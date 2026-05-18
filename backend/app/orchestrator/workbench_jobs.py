"""Workbench job dispatch + result handling.

Currently one job kind is wired: `import` — parse a resume and *incrementally
merge* it into the CV library. The agent does semantic dedup: it gets the
existing library (with entry IDs) plus the new resume text, and decides for
each item whether it's new or matches something already stored. The
orchestrator then applies only the new pieces, with exact-match safety nets
on top of the model's judgement.

Tailor (W2) and improve (W3) will reuse this dispatch path — same
WorkbenchRequestMsg / WorkbenchResultMsg, different prompt + result handler.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    CoverLetter,
    CVBullet,
    CVEntry,
    CVEntryKind,
    CVProfile,
    CVSkill,
    CVSummary,
    ResumeApplication,
    TailoredResume,
    WorkbenchJob,
    WorkbenchJobStatus,
)
from app.orchestrator.protocol import WorkbenchRequestMsg
from app.orchestrator.registry import LiveWorker, registry

log = structlog.get_logger()

# Pools to try, in order, for a Workbench inference job. Import is structured
# text work — the docs pool is the natural home, but fall through so a job
# still runs on a single-pool fleet.
_IMPORT_POOLS = ("docs", "researcher", "planner", "coder")


# ── worker selection ───────────────────────────────────────────────


async def _pick_worker(preferred_pools: tuple[str, ...]) -> LiveWorker | None:
    for pool in preferred_pools:
        workers = await registry.by_pool(pool)
        if not workers:
            continue
        idle = [w for w in workers if w.current_task_id is None]
        return (idle or workers)[0]
    return None


# ── library serialization (what the model dedups against) ──────────


async def serialize_library(session: AsyncSession) -> str:
    """Render the existing CV library as text the model can compare against.
    Entry IDs are included so the model can reference them when it decides a
    new resume entry matches one already stored."""
    parts: list[str] = []

    profile = (await session.execute(select(CVProfile))).scalars().first()
    parts.append("### Profile")
    if profile and (profile.full_name or profile.email):
        parts.append(f"name: {profile.full_name or '(none)'}")
        if profile.headline:
            parts.append(f"headline: {profile.headline}")
        if profile.location:
            parts.append(f"location: {profile.location}")
        if profile.email:
            parts.append(f"email: {profile.email}")
        if profile.phone:
            parts.append(f"phone: {profile.phone}")
        for k, v in (profile.links or {}).items():
            parts.append(f"{k}: {v}")
    else:
        parts.append("(no profile set yet)")

    summaries = list((await session.execute(
        select(CVSummary).order_by(CVSummary.created_at)
    )).scalars().all())
    parts.append("\n### Summaries")
    if summaries:
        for s in summaries:
            parts.append(f"- [{s.label}] {s.text}")
    else:
        parts.append("(none)")

    entries = list((await session.execute(
        select(CVEntry).order_by(CVEntry.kind, CVEntry.created_at)
    )).scalars().all())
    parts.append("\n### Entries")
    if entries:
        for e in entries:
            dates = " ".join(filter(None, [e.start_date, "–" if (e.start_date or e.end_date) else None, e.end_date]))
            header = (
                f"[{e.kind.value}] id={e.id} | {e.organization}"
                f"{' — ' + e.title if e.title else ''}"
                f"{' | ' + e.location if e.location else ''}"
                f"{' | ' + dates if dates.strip() else ''}"
            )
            parts.append(header)
            if e.description:
                parts.append(f"  description: {e.description}")
            for b in e.bullets:
                parts.append(f"  - {b.text}")
    else:
        parts.append("(none)")

    skills = list((await session.execute(
        select(CVSkill).order_by(CVSkill.category, CVSkill.name)
    )).scalars().all())
    parts.append("\n### Skills")
    if skills:
        for s in skills:
            parts.append(f"- [{s.category}] {s.name}")
    else:
        parts.append("(none)")

    return "\n".join(parts)


# ── prompt construction ────────────────────────────────────────────


_IMPORT_SYSTEM = """\
You parse resumes and merge them into an existing CV library WITHOUT creating
duplicates. You will be given the current library (entries carry an `id=`) and
the raw text of a newly-uploaded resume.

Extract every CV item from the new resume text, then for each one decide
whether it's already represented in the library:

- ENTRIES (jobs / projects / education): if the new entry is the same role at
  the same organization as an existing one — even if the wording differs —
  set `matches_existing_id` to that entry's id. Otherwise set it to null.
- BULLETS: under each entry, mark `is_new: true` only for bullets whose
  content is NOT already present in the library (for matched entries, compare
  against that existing entry's bullets; for new entries, all bullets are new).
- SKILLS / SUMMARIES: mark `is_new: true` only if not already present.

Be conservative: when in doubt about whether something is a duplicate, mark it
new — the human reviews the result and it's easier to delete than to recover.

Output ONLY a single JSON object in a ```json code block, no prose around it:

```json
{
  "profile": {
    "full_name": "", "headline": "", "location": "",
    "email": "", "phone": "",
    "links": {"linkedin": "", "github": "", "website": ""}
  },
  "summaries": [
    {"label": "default", "text": "...", "is_new": true}
  ],
  "entries": [
    {
      "kind": "experience",
      "organization": "...", "title": "...", "location": "...",
      "url": "", "start_date": "...", "end_date": "...", "description": "",
      "matches_existing_id": null,
      "bullets": [
        {"text": "...", "tags": ["python", "ci-cd"], "is_new": true}
      ]
    }
  ],
  "skills": [
    {"category": "Web Development", "name": "Spring Boot", "is_new": true}
  ]
}
```

`kind` must be one of: experience, project, education. Leave string fields as
"" when the resume doesn't supply them. `tags` are short lowercase keywords
for later job-description matching — infer 2–5 per bullet.
"""


def build_import_messages(library_text: str, resume_text: str) -> list[dict[str, str]]:
    user = (
        f"## Existing CV library\n{library_text}\n\n"
        f"## Newly uploaded resume (raw text)\n{resume_text}\n\n"
        f"## Task\nExtract and merge per the rules. Output only the JSON object."
    )
    return [
        {"role": "system", "content": _IMPORT_SYSTEM},
        {"role": "user", "content": user},
    ]


# ── dispatch ───────────────────────────────────────────────────────


async def dispatch_import(job_id: uuid.UUID) -> str | None:
    """Build the import prompt and send it to a worker. Returns None on
    success, or an error string the caller should record on the job."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            return "job not found"
        resume_text = (job.input or {}).get("resume_text", "")
        if not resume_text.strip():
            return "no resume text on the job"
        library_text = await serialize_library(session)

    worker = await _pick_worker(_IMPORT_POOLS)
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to run the import."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id,
        kind="import",
        model=None,  # worker default — import is not model-sensitive
        messages=build_import_messages(library_text, resume_text),
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("workbench.dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if job:
            job.status = WorkbenchJobStatus.RUNNING
            job.worker_name = worker.name
            await session.commit()
    log.info("workbench.import_dispatched", job=str(job_id), worker=worker.name)
    return None


# ── result handling ────────────────────────────────────────────────


def _extract_json_object(text: str) -> dict | None:
    """Pull the JSON object out of a model response — fenced or bare."""
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    # Also try the largest {...} span as a fallback.
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


async def _apply_import_merge(session: AsyncSession, parsed: dict) -> dict[str, Any]:
    """Apply the model's parsed import to the library, incrementally.

    Returns a counts dict for the result summary. Exact-match safety nets sit
    on top of the model's is_new / matches_existing_id judgement.
    """
    counts = {
        "profile_updated": False,
        "summaries_added": 0,
        "entries_added": 0,
        "entries_matched": 0,
        "bullets_added": 0,
        "skills_added": 0,
    }

    # ── profile: fill only empty fields, never overwrite existing data ──
    p_in = parsed.get("profile") or {}
    if isinstance(p_in, dict) and any(p_in.get(k) for k in ("full_name", "email")):
        profile = (await session.execute(select(CVProfile))).scalars().first()
        if profile is None:
            profile = CVProfile()
            session.add(profile)
        for field in ("full_name", "headline", "location", "email", "phone"):
            if not getattr(profile, field, None) and p_in.get(field):
                setattr(profile, field, str(p_in[field]).strip())
                counts["profile_updated"] = True
        links = dict(profile.links or {})
        for k, v in (p_in.get("links") or {}).items():
            if v and not links.get(k):
                links[k] = str(v).strip()
                counts["profile_updated"] = True
        profile.links = links

    # ── summaries ──
    existing_summaries = {
        s.text.strip().lower()
        for s in (await session.execute(select(CVSummary))).scalars().all()
    }
    for s in parsed.get("summaries") or []:
        if not isinstance(s, dict) or not s.get("is_new"):
            continue
        text = str(s.get("text", "")).strip()
        if not text or text.lower() in existing_summaries:
            continue
        session.add(CVSummary(label=str(s.get("label") or "imported")[:128], text=text))
        existing_summaries.add(text.lower())
        counts["summaries_added"] += 1

    # ── entries + bullets ──
    for e in parsed.get("entries") or []:
        if not isinstance(e, dict):
            continue
        try:
            kind = CVEntryKind(str(e.get("kind", "")).strip())
        except ValueError:
            continue

        match_id_raw = e.get("matches_existing_id")
        target_entry: CVEntry | None = None
        if match_id_raw:
            try:
                target_entry = await session.get(CVEntry, uuid.UUID(str(match_id_raw)))
            except (ValueError, TypeError):
                target_entry = None

        if target_entry is not None:
            # Matched an existing entry — attach only genuinely-new bullets.
            counts["entries_matched"] += 1
            existing_texts = {b.text.strip().lower() for b in target_entry.bullets}
            for b in e.get("bullets") or []:
                if not isinstance(b, dict) or not b.get("is_new"):
                    continue
                btext = str(b.get("text", "")).strip()
                if not btext or btext.lower() in existing_texts:
                    continue
                session.add(CVBullet(
                    entry_id=target_entry.id,
                    text=btext,
                    tags=[str(t).strip().lower() for t in (b.get("tags") or []) if str(t).strip()],
                ))
                existing_texts.add(btext.lower())
                counts["bullets_added"] += 1
        else:
            # Brand-new entry — create it and all its bullets.
            entry = CVEntry(
                kind=kind,
                organization=str(e.get("organization", "")).strip() or "(unknown)",
                title=str(e.get("title") or "").strip() or None,
                location=str(e.get("location") or "").strip() or None,
                url=str(e.get("url") or "").strip() or None,
                start_date=str(e.get("start_date") or "").strip() or None,
                end_date=str(e.get("end_date") or "").strip() or None,
                description=str(e.get("description") or "").strip() or None,
            )
            session.add(entry)
            await session.flush()
            counts["entries_added"] += 1
            for b in e.get("bullets") or []:
                if not isinstance(b, dict):
                    continue
                btext = str(b.get("text", "")).strip()
                if not btext:
                    continue
                session.add(CVBullet(
                    entry_id=entry.id,
                    text=btext,
                    tags=[str(t).strip().lower() for t in (b.get("tags") or []) if str(t).strip()],
                ))
                counts["bullets_added"] += 1

    # ── skills (exact category+name dedup safety net) ──
    existing_skills = {
        (s.category.strip().lower(), s.name.strip().lower())
        for s in (await session.execute(select(CVSkill))).scalars().all()
    }
    for s in parsed.get("skills") or []:
        if not isinstance(s, dict) or not s.get("is_new"):
            continue
        cat = str(s.get("category", "")).strip()
        name = str(s.get("name", "")).strip()
        if not cat or not name:
            continue
        if (cat.lower(), name.lower()) in existing_skills:
            continue
        session.add(CVSkill(category=cat, name=name))
        existing_skills.add((cat.lower(), name.lower()))
        counts["skills_added"] += 1

    return counts


async def handle_import_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Worker finished an import job — parse + apply, or record the failure."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            log.warning("workbench.import_result_no_job", job=str(job_id))
            return

        if not success:
            job.status = WorkbenchJobStatus.ERROR
            job.error = error or "worker reported failure"
            await session.commit()
            return

        parsed = _extract_json_object(content)
        if parsed is None:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "could not parse a JSON object from the model response"
            job.result = {"raw": content[:8000]}
            await session.commit()
            return

        try:
            async with session.begin_nested():
                counts = await _apply_import_merge(session, parsed)
            job.status = WorkbenchJobStatus.DONE
            job.result = {"counts": counts, "raw": parsed}
            job.error = None
        except Exception as exc:  # noqa: BLE001 — surface any merge failure to the user
            log.exception("workbench.import_merge_failed", job=str(job_id))
            job.status = WorkbenchJobStatus.ERROR
            job.error = f"merge failed: {exc}"
            job.result = {"raw": parsed}
        await session.commit()
    log.info("workbench.import_result_applied", job=str(job_id), status=job.status.value)


# ════════════════════════ tailoring (W2) ════════════════════════════
#
# A tailor job reads the WHOLE CV library (every entry/bullet/skill carries an
# id) plus a job description, and returns a structured *selection*: which
# bullets and skills to include, plus a freshly-written summary. The model may
# lightly rephrase chosen bullets to mirror the JD's terminology — constrained
# to stay truthful. The orchestrator validates every id against the library,
# applies the rephrases, and assembles the Markdown deterministically so
# formatting is identical across versions.


async def serialize_library_with_ids(session: AsyncSession) -> str:
    """Full CV library serialized for tailoring — entries, bullets, and skills
    all carry their database ids so the model can reference them precisely."""
    parts: list[str] = []

    profile = (await session.execute(select(CVProfile))).scalars().first()
    parts.append("### Profile")
    if profile and (profile.full_name or profile.email):
        parts.append(f"name: {profile.full_name or '(none)'}")
        for label, val in (
            ("headline", profile.headline), ("location", profile.location),
            ("email", profile.email), ("phone", profile.phone),
        ):
            if val:
                parts.append(f"{label}: {val}")
    else:
        parts.append("(no profile set)")

    summaries = list((await session.execute(
        select(CVSummary).order_by(CVSummary.created_at)
    )).scalars().all())
    parts.append("\n### Summary variants (reference — write a fresh tailored summary, don't copy verbatim)")
    if summaries:
        for s in summaries:
            parts.append(f"- [{s.label}] {s.text}")
    else:
        parts.append("(none — write a summary from scratch based on the entries)")

    entries = list((await session.execute(
        select(CVEntry).where(CVEntry.active.is_(True)).order_by(CVEntry.kind, CVEntry.created_at)
    )).scalars().all())
    parts.append("\n### Entries")
    for e in entries:
        dates = " – ".join(filter(None, [e.start_date, e.end_date])) if (e.start_date or e.end_date) else ""
        parts.append(
            f"[{e.kind.value}] entry_id={e.id} | {e.organization}"
            f"{' — ' + e.title if e.title else ''}"
            f"{' | ' + e.location if e.location else ''}"
            f"{' | ' + dates if dates else ''}"
        )
        if e.description:
            parts.append(f"  description: {e.description}")
        for b in e.bullets:
            if not b.active:
                continue
            tags = ",".join(b.tags or [])
            parts.append(f"  bullet_id={b.id} tags=[{tags}] :: {b.text}")
    if not entries:
        parts.append("(none)")

    skills = list((await session.execute(
        select(CVSkill).order_by(CVSkill.category, CVSkill.name)
    )).scalars().all())
    parts.append("\n### Skills")
    if skills:
        for s in skills:
            parts.append(f"skill_id={s.id} | [{s.category}] {s.name}")
    else:
        parts.append("(none)")

    return "\n".join(parts)


_TAILOR_SYSTEM = """\
You tailor a resume to a specific job description by SELECTING the best-fit
items from a CV library. You do not invent experience. You may lightly
rephrase chosen bullet points to mirror the job description's terminology —
but every rephrase must stay strictly truthful to the original bullet's
meaning. Never add accomplishments, numbers, or technologies that aren't in
the source bullet.

You will be given the full CV library (every entry, bullet, and skill carries
an id) and a job description. Produce a JSON selection:

- Write a fresh `summary` paragraph tailored to this job (2–4 sentences).
- Pick the `skill_ids` most relevant to the job — favor what the JD asks for.
- For each relevant entry, list the `bullet_id`s worth including, in priority
  order. For a bullet you want to reword, set `rephrased` to the new text;
  otherwise set it to null and the original is used verbatim.
- Include only entries and bullets that strengthen the application for THIS
  job. It's fine to drop weak entries entirely.
- Give a short `rationale` explaining the overall choices.

Output ONLY a single JSON object in a ```json code block:

```json
{
  "summary": "tailored summary paragraph...",
  "skill_ids": ["<uuid>", "<uuid>"],
  "entries": [
    {
      "entry_id": "<uuid>",
      "bullets": [
        {"bullet_id": "<uuid>", "rephrased": "reworded text or null"}
      ]
    }
  ],
  "rationale": "1-3 sentences on why these items fit this job"
}
```

Every id MUST come from the library below — do not invent ids.
"""


def build_tailor_messages(library_text: str, job_description: str) -> list[dict[str, str]]:
    user = (
        f"## CV library\n{library_text}\n\n"
        f"## Job description\n{job_description}\n\n"
        f"## Task\nProduce the JSON selection per the rules. Output only the JSON object."
    )
    return [
        {"role": "system", "content": _TAILOR_SYSTEM},
        {"role": "user", "content": user},
    ]


async def dispatch_tailor(job_id: uuid.UUID) -> str | None:
    """Build the tailor prompt and send it to a worker. Returns None on
    success, or an error string the caller should record on the job."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            return "job not found"
        application_id = (job.input or {}).get("application_id")
        if not application_id:
            return "job has no application_id"
        app = await session.get(ResumeApplication, uuid.UUID(str(application_id)))
        if not app:
            return "application not found"
        if not (app.job_description or "").strip():
            return "the application has no job description"
        library_text = await serialize_library_with_ids(session)
        job_description = app.job_description

    worker = await _pick_worker(_IMPORT_POOLS)
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to run the tailoring job."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id,
        kind="tailor",
        model=None,
        messages=build_tailor_messages(library_text, job_description),
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("workbench.tailor_dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if job:
            job.status = WorkbenchJobStatus.RUNNING
            job.worker_name = worker.name
            await session.commit()
    log.info("workbench.tailor_dispatched", job=str(job_id), worker=worker.name)
    return None


async def _assemble_resume(
    session: AsyncSession, selection: dict
) -> tuple[str, dict]:
    """Validate the model's selection against the library and assemble the
    tailored resume as Markdown. Returns (markdown, validated_selection).

    Any id the model invented or that points at a missing/cross-entry record
    is silently dropped — the resume is built only from real library items.
    """
    profile = (await session.execute(select(CVProfile))).scalars().first()

    # ── header ──
    lines: list[str] = []
    if profile and profile.full_name:
        lines.append(f"# {profile.full_name}")
        contact = " · ".join(filter(None, [
            profile.location, profile.email, profile.phone,
            *(profile.links or {}).values(),
        ]))
        if contact:
            lines.append(contact)
        lines.append("")

    # ── summary ──
    summary = str(selection.get("summary") or "").strip()
    if summary:
        lines.append("## Summary")
        lines.append(summary)
        lines.append("")

    # ── skills (validated, grouped by category) ──
    skill_ids: list[str] = [str(s) for s in (selection.get("skill_ids") or [])]
    chosen_skills: list[CVSkill] = []
    for sid in skill_ids:
        try:
            sk = await session.get(CVSkill, uuid.UUID(sid))
        except (ValueError, TypeError):
            sk = None
        if sk:
            chosen_skills.append(sk)
    if chosen_skills:
        lines.append("## Skills")
        by_cat: dict[str, list[str]] = {}
        for sk in chosen_skills:
            by_cat.setdefault(sk.category, []).append(sk.name)
        for cat, names in by_cat.items():
            lines.append(f"- **{cat}**: {', '.join(names)}")
        lines.append("")

    # ── entries, grouped by kind ──
    validated_entries: list[dict] = []
    entries_by_kind: dict[CVEntryKind, list[tuple[CVEntry, list[str]]]] = {
        CVEntryKind.EXPERIENCE: [], CVEntryKind.PROJECT: [], CVEntryKind.EDUCATION: [],
    }
    for e_sel in selection.get("entries") or []:
        if not isinstance(e_sel, dict):
            continue
        try:
            entry = await session.get(CVEntry, uuid.UUID(str(e_sel.get("entry_id"))))
        except (ValueError, TypeError):
            entry = None
        if entry is None:
            continue
        # Map this entry's real bullets so we can validate + apply rephrases.
        bullet_by_id = {str(b.id): b for b in entry.bullets}
        rendered_bullets: list[str] = []
        validated_bullets: list[dict] = []
        for b_sel in e_sel.get("bullets") or []:
            if not isinstance(b_sel, dict):
                continue
            real = bullet_by_id.get(str(b_sel.get("bullet_id")))
            if real is None:
                continue  # invented or cross-entry id — drop it
            rephrased = b_sel.get("rephrased")
            text = str(rephrased).strip() if rephrased else real.text
            rendered_bullets.append(text)
            validated_bullets.append({
                "bullet_id": str(real.id),
                "rephrased": text if rephrased else None,
            })
        if rendered_bullets:
            entries_by_kind[entry.kind].append((entry, rendered_bullets))
            validated_entries.append({
                "entry_id": str(entry.id), "bullets": validated_bullets,
            })

    _SECTION_TITLES = {
        CVEntryKind.EXPERIENCE: "Experience",
        CVEntryKind.PROJECT: "Projects",
        CVEntryKind.EDUCATION: "Education",
    }
    for kind, title in _SECTION_TITLES.items():
        group = entries_by_kind[kind]
        if not group:
            continue
        lines.append(f"## {title}")
        for entry, bullets in group:
            heading = entry.organization
            if entry.title:
                heading += f" — {entry.title}"
            lines.append(f"### {heading}")
            meta = " · ".join(filter(None, [
                entry.location,
                " – ".join(filter(None, [entry.start_date, entry.end_date])) or None,
                entry.url,
            ]))
            if meta:
                lines.append(f"*{meta}*")
            if entry.description:
                lines.append(entry.description)
            for b in bullets:
                lines.append(f"- {b}")
            lines.append("")

    markdown = "\n".join(lines).strip() + "\n"
    validated = {
        "summary": summary,
        "skill_ids": [str(s.id) for s in chosen_skills],
        "entries": validated_entries,
        "rationale": str(selection.get("rationale") or "").strip(),
    }
    return markdown, validated


# ════════════════════════ cover letter (W4) ═════════════════════════
#
# Drafts a cover letter for an application. The model receives the candidate's
# profile, the job description, and (if available) the highlights from the
# most recent TailoredResume so the letter mirrors what's on the resume. Like
# every other Workbench job, it rides the workbench_request / workbench_result
# protocol — non-streaming, one inference, structured result handler.


_COVER_LETTER_SYSTEM = """\
You draft cover letters. Output ONE cover letter for the job described below.

Style:
  - 3-4 paragraphs, roughly 250-350 words total.
  - Open with a brief hook tied to the role / company.
  - Middle paragraphs map specific experience (from the candidate's resume
    highlights below) to what the job description asks for. Stay strictly
    truthful — do not invent results, technologies, or scope.
  - Close with a clear, polite call-to-action.
  - Address "Dear Hiring Manager," unless the JD names someone.
  - Sign off with the candidate's name (no contact block — the PDF template
    adds the header).
  - Output ONLY the letter body. Markdown for paragraph breaks (blank lines).
  - No prose around the letter, no preface, no JSON, no code fences.
"""


def _highlights_from_tailored(tailored: TailoredResume | None) -> list[str]:
    """Pull the (already-rephrased) bullet texts out of the latest tailored
    resume's selection so the cover letter can echo what's actually on the
    resume the user will send. Returns the bullets as plain strings."""
    if not tailored or not tailored.selection:
        return []
    out: list[str] = []
    for e in tailored.selection.get("entries") or []:
        if not isinstance(e, dict):
            continue
        for b in e.get("bullets") or []:
            if not isinstance(b, dict):
                continue
            rephrased = b.get("rephrased")
            if rephrased and str(rephrased).strip():
                out.append(str(rephrased).strip())
    return out


def build_cover_letter_messages(
    full_name: str,
    headline: str | None,
    summary_text: str | None,
    highlights: list[str],
    job_description: str,
    company: str | None,
    role_title: str | None,
) -> list[dict[str, str]]:
    candidate_block = f"Name: {full_name or '(unspecified)'}"
    if headline:
        candidate_block += f"\nHeadline: {headline}"
    if summary_text:
        candidate_block += f"\nSummary: {summary_text}"

    target_block = ""
    if company or role_title:
        target_block = (
            f"Company: {company or '(unspecified)'}\n"
            f"Role: {role_title or '(unspecified)'}\n"
        )

    if highlights:
        h_block = "\n".join(f"- {h}" for h in highlights)
    else:
        h_block = "(no tailored highlights — derive from the JD and candidate profile)"

    user = (
        f"## Candidate\n{candidate_block}\n\n"
        f"## Target\n{target_block}\n"
        f"## Resume highlights to echo (truthful only)\n{h_block}\n\n"
        f"## Job description\n{job_description}\n\n"
        f"## Task\nWrite the cover letter per the rules. Output only the body."
    )
    return [
        {"role": "system", "content": _COVER_LETTER_SYSTEM},
        {"role": "user", "content": user},
    ]


async def dispatch_cover_letter(job_id: uuid.UUID) -> str | None:
    """Build the cover-letter prompt for an application and send it to a worker."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            return "job not found"
        application_id = (job.input or {}).get("application_id")
        if not application_id:
            return "job has no application_id"
        try:
            app = await session.get(ResumeApplication, uuid.UUID(str(application_id)))
        except (ValueError, TypeError):
            app = None
        if not app:
            return "application not found"
        if not (app.job_description or "").strip():
            return "the application has no job description"

        profile = (await session.execute(select(CVProfile))).scalars().first()
        full_name = profile.full_name if profile else ""
        headline = profile.headline if profile else None

        # Use a default-labelled summary variant if one exists, else any.
        summary_row = (await session.execute(
            select(CVSummary).order_by(CVSummary.created_at).limit(1)
        )).scalars().first()
        summary_text = summary_row.text if summary_row else None

        # Latest TailoredResume version for this app — gives the model a list
        # of already-tailored bullets to echo. Optional: works fine without.
        latest = (await session.execute(
            select(TailoredResume)
            .where(TailoredResume.application_id == app.id)
            .order_by(TailoredResume.version.desc())
            .limit(1)
        )).scalars().first()
        highlights = _highlights_from_tailored(latest)

        messages = build_cover_letter_messages(
            full_name=full_name, headline=headline, summary_text=summary_text,
            highlights=highlights, job_description=app.job_description,
            company=app.company, role_title=app.role_title,
        )

    worker = await _pick_worker(_IMPORT_POOLS)
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to draft the cover letter."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id, kind="cover_letter", model=None, messages=messages,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("workbench.cover_letter_dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if job:
            job.status = WorkbenchJobStatus.RUNNING
            job.worker_name = worker.name
            await session.commit()
    log.info("workbench.cover_letter_dispatched", job=str(job_id), worker=worker.name)
    return None


async def handle_cover_letter_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Persist the drafted letter as a new CoverLetter version on the application."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            log.warning("workbench.cover_letter_result_no_job", job=str(job_id))
            return
        if not success:
            job.status = WorkbenchJobStatus.ERROR
            job.error = error or "worker reported failure"
            await session.commit()
            return

        application_id = (job.input or {}).get("application_id")
        try:
            app = await session.get(ResumeApplication, uuid.UUID(str(application_id)))
        except (ValueError, TypeError):
            app = None
        if app is None:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "application no longer exists"
            await session.commit()
            return

        body = (content or "").strip()
        # Strip a single accidental fence wrapper that some models add.
        if body.startswith("```") and body.endswith("```"):
            body = "\n".join(body.split("\n")[1:-1]).strip()
        if not body:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "the model returned an empty letter"
            await session.commit()
            return

        existing = (await session.execute(
            select(CoverLetter).where(CoverLetter.application_id == app.id)
        )).scalars().all()
        next_version = (max((c.version for c in existing), default=0)) + 1
        letter = CoverLetter(
            application_id=app.id, version=next_version, body=body,
        )
        session.add(letter)
        await session.flush()
        job.status = WorkbenchJobStatus.DONE
        job.error = None
        job.result = {"cover_letter_id": str(letter.id), "version": next_version}
        await session.commit()
    log.info("workbench.cover_letter_result_applied", job=str(job_id))


# ════════════════════════ improve (W3) ═════════════════════════════
#
# A single-bullet rewrite. The model gets one bullet (plus a little context —
# what entry it belongs to and the bullet's tags) and proposes a strengthened
# version. The result is stored on the job for human review; nothing in the
# library is mutated until the user explicitly clicks Apply on the review
# screen. Truthfulness rule is the same as tailoring: no invented numbers,
# technologies, or accomplishments — only stronger wording of what's there.


_IMPROVE_SYSTEM = """\
You improve resume bullet points. The user will give you ONE bullet plus a
little context. Output ONE strengthened version of that bullet.

Rules — non-negotiable:
- Stay strictly truthful. Do NOT add new numbers, technologies, results,
  scope, or claims that aren't already in the source bullet. If the source
  doesn't say "increased X by 40%", you can't either.
- Use a strong action verb to lead.
- Be concrete where the source is concrete; do not invent specificity.
- Keep resume tense (past for past roles, present-tense fine for ongoing).
- Keep it tight — one sentence, ideally under 25 words.
- Output ONLY the improved bullet text. No quotes around it, no prose
  before or after, no list marker, no leading dash.
"""


def build_improve_messages(
    bullet_text: str, entry_label: str, tags: list[str]
) -> list[dict[str, str]]:
    tag_str = ", ".join(tags) if tags else "(none)"
    user = (
        f"## Context\nThis bullet is on the entry: {entry_label}\n"
        f"Tags: {tag_str}\n\n"
        f"## Original bullet\n{bullet_text}\n\n"
        f"## Task\nProduce one improved version of the bullet, per the rules."
    )
    return [
        {"role": "system", "content": _IMPROVE_SYSTEM},
        {"role": "user", "content": user},
    ]


async def dispatch_improve(job_id: uuid.UUID) -> str | None:
    """Build the improve prompt and send it to a worker."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            return "job not found"
        bullet_id = (job.input or {}).get("bullet_id")
        if not bullet_id:
            return "job has no bullet_id"
        try:
            bullet = await session.get(CVBullet, uuid.UUID(str(bullet_id)))
        except (ValueError, TypeError):
            bullet = None
        if not bullet:
            return "bullet not found"
        entry = await session.get(CVEntry, bullet.entry_id)
        entry_label = entry.organization if entry else "(unknown)"
        if entry and entry.title:
            entry_label += f" — {entry.title}"
        messages = build_improve_messages(bullet.text, entry_label, list(bullet.tags or []))

    worker = await _pick_worker(_IMPORT_POOLS)
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to run the improvement."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id,
        kind="improve",
        model=None,
        messages=messages,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("workbench.improve_dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if job:
            job.status = WorkbenchJobStatus.RUNNING
            job.worker_name = worker.name
            await session.commit()
    log.info("workbench.improve_dispatched", job=str(job_id), worker=worker.name)
    return None


async def handle_improve_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Store the model's proposal on the job. Application to the bullet is a
    separate user-driven step (the review screen's Apply button)."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            log.warning("workbench.improve_result_no_job", job=str(job_id))
            return
        if not success:
            job.status = WorkbenchJobStatus.ERROR
            job.error = error or "worker reported failure"
            await session.commit()
            return
        proposed = (content or "").strip()
        # The model occasionally wraps its single line in quotes or adds a leading
        # bullet marker — strip a few common decorations defensively.
        for prefix in ("- ", "* ", "• "):
            if proposed.startswith(prefix):
                proposed = proposed[len(prefix):].strip()
                break
        if (proposed.startswith('"') and proposed.endswith('"')) or (
            proposed.startswith("'") and proposed.endswith("'")
        ):
            proposed = proposed[1:-1].strip()
        if not proposed:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "the model returned an empty proposal"
            await session.commit()
            return
        job.status = WorkbenchJobStatus.DONE
        job.error = None
        job.result = {"proposed": proposed}
        await session.commit()
    log.info("workbench.improve_result_applied", job=str(job_id))


async def handle_tailor_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Worker finished a tailor job — parse the selection, validate it against
    the library, assemble the Markdown, and store it as a new TailoredResume
    version on the application."""
    async with SessionLocal() as session:
        job = await session.get(WorkbenchJob, job_id)
        if not job:
            log.warning("workbench.tailor_result_no_job", job=str(job_id))
            return

        if not success:
            job.status = WorkbenchJobStatus.ERROR
            job.error = error or "worker reported failure"
            await session.commit()
            return

        application_id = (job.input or {}).get("application_id")
        try:
            app = await session.get(ResumeApplication, uuid.UUID(str(application_id)))
        except (ValueError, TypeError):
            app = None
        if app is None:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "application no longer exists"
            await session.commit()
            return

        parsed = _extract_json_object(content)
        if parsed is None:
            job.status = WorkbenchJobStatus.ERROR
            job.error = "could not parse a JSON selection from the model response"
            job.result = {"raw": content[:8000]}
            await session.commit()
            return

        try:
            markdown, validated = await _assemble_resume(session, parsed)
            existing = (await session.execute(
                select(TailoredResume).where(TailoredResume.application_id == app.id)
            )).scalars().all()
            next_version = (max((t.version for t in existing), default=0)) + 1
            tailored = TailoredResume(
                application_id=app.id,
                version=next_version,
                selection=validated,
                rendered=markdown,
                rationale=validated.get("rationale") or None,
            )
            session.add(tailored)
            await session.flush()
            job.status = WorkbenchJobStatus.DONE
            job.error = None
            job.result = {"tailored_resume_id": str(tailored.id), "version": next_version}
        except Exception as exc:  # noqa: BLE001
            log.exception("workbench.tailor_assemble_failed", job=str(job_id))
            job.status = WorkbenchJobStatus.ERROR
            job.error = f"assembly failed: {exc}"
            job.result = {"raw": parsed}
        await session.commit()
    log.info("workbench.tailor_result_applied", job=str(job_id), status=job.status.value)

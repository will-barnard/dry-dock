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
    CVBullet,
    CVEntry,
    CVEntryKind,
    CVProfile,
    CVSkill,
    CVSummary,
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

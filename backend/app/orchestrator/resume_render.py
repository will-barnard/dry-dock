"""Render Workbench artifacts as print-quality PDFs.

The PDF pipeline:
  TailoredResume.selection (validated, stable structured pick)
    → HTML via the resume_print.html Jinja template
    → PDF via WeasyPrint

We render from the *structured selection*, not the stored Markdown blob, so
formatting is deterministic and the model's truthful rephrases come through
exactly as captured. CoverLetter rendering is much simpler — its `body` is
already prose, we just frame it with the profile header.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CoverLetter,
    CVBullet,
    CVEntry,
    CVEntryKind,
    CVProfile,
    CVSkill,
    TailoredResume,
)

log = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

# ── data shapes the templates render against ───────────────────────


def _format_dates(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


async def _profile_dict(session: AsyncSession) -> dict:
    profile = (await session.execute(select(CVProfile))).scalars().first()
    if not profile:
        return {"full_name": "", "contacts": [], "links": []}
    contacts = [c for c in (profile.location, profile.email, profile.phone) if c]
    links: list[tuple[str, str]] = []
    for key, url in (profile.links or {}).items():
        if not url:
            continue
        # Use the canonical link label rather than the URL itself for the
        # printed contact line; matches the reference resume's style.
        labels = {"linkedin": "LinkedIn", "github": "GitHub"}
        label = labels.get(key.lower(), key)
        links.append((label, url))
    return {
        "full_name": profile.full_name,
        "headline": profile.headline,
        "contacts": contacts,
        "links": links,
    }


async def _resume_data(session: AsyncSession, tailored: TailoredResume) -> dict:
    """Pull all the data the template needs from the stored selection.

    We re-query entries and skills by id so the layout has access to the
    organization / title / dates / location / url fields, but use the
    rephrased bullet text from the selection when present so the model's
    truthful rephrases survive.
    """
    selection = tailored.selection or {}

    # Skills, grouped by category preserving the source CVSkill.category text.
    skill_ids = [str(s) for s in (selection.get("skill_ids") or [])]
    skills: list[CVSkill] = []
    for sid in skill_ids:
        try:
            sk = await session.get(CVSkill, uuid.UUID(sid))
        except (ValueError, TypeError):
            sk = None
        if sk:
            skills.append(sk)
    skills_by_category: dict[str, list[str]] = {}
    for sk in skills:
        skills_by_category.setdefault(sk.category, []).append(sk.name)
    skills_grouped = [(cat, names) for cat, names in skills_by_category.items()]

    # Entries grouped by kind.
    sections: dict[CVEntryKind, list[dict]] = {
        CVEntryKind.EXPERIENCE: [],
        CVEntryKind.PROJECT: [],
        CVEntryKind.EDUCATION: [],
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
        bullet_by_id = {str(b.id): b for b in entry.bullets}
        rendered_bullets: list[str] = []
        for b_sel in e_sel.get("bullets") or []:
            if not isinstance(b_sel, dict):
                continue
            real = bullet_by_id.get(str(b_sel.get("bullet_id")))
            if real is None:
                continue
            rephrased = b_sel.get("rephrased")
            rendered_bullets.append(
                str(rephrased).strip() if rephrased else real.text
            )
        # Education rows are allowed to print with zero bullets (just degree
        # + school + dates). Experience / project rows without bullets are
        # noise and get dropped.
        if not rendered_bullets and entry.kind != CVEntryKind.EDUCATION:
            continue
        sections[entry.kind].append({
            "organization": entry.organization,
            "title": entry.title,
            "location": entry.location,
            "url": entry.url,
            "dates": _format_dates(entry.start_date, entry.end_date),
            "description": entry.description,
            "bullets": rendered_bullets,
        })

    return {
        "profile": await _profile_dict(session),
        "summary": str(selection.get("summary") or "").strip(),
        "skills": skills_grouped,
        "experience": sections[CVEntryKind.EXPERIENCE],
        "projects": sections[CVEntryKind.PROJECT],
        "education": sections[CVEntryKind.EDUCATION],
    }


# ── public API ──────────────────────────────────────────────────────


async def render_resume_html(session: AsyncSession, tailored: TailoredResume) -> str:
    data = await _resume_data(session, tailored)
    template = _env.get_template("resume_print.html")
    return template.render(**data)


async def render_resume_pdf(session: AsyncSession, tailored: TailoredResume) -> bytes:
    """Render a TailoredResume to PDF bytes via WeasyPrint."""
    html = await render_resume_html(session, tailored)
    # Imported here so the rest of the module loads cleanly even on a host
    # that doesn't have the WeasyPrint native deps (useful for tests).
    from weasyprint import HTML
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()


async def render_cover_letter_html(
    session: AsyncSession, letter: CoverLetter
) -> str:
    profile = await _profile_dict(session)
    template = _env.get_template("cover_letter_print.html")
    return template.render(profile=profile, body=letter.body)


async def render_cover_letter_pdf(
    session: AsyncSession, letter: CoverLetter
) -> bytes:
    html = await render_cover_letter_html(session, letter)
    from weasyprint import HTML
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()

"""Workbench module — CV library editor (W1).

The CV library is a structured, granular store of resume content:
  CVProfile   — one header/contact row
  CVSummary   — summary-paragraph variants
  CVEntry     — jobs / projects / education (containers)
  CVBullet    — reusable line items under an entry
  CVSkill     — individual skills, grouped by category

This file is all CRUD: small form handlers, each redirecting back to
/workbench. The tailoring workflow (W2) and agent-powered import (W1.5) build
on top of these tables.
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import (
    CVBullet,
    CVEntry,
    CVEntryKind,
    CVProfile,
    CVSkill,
    CVSummary,
    ResumeApplication,
    TailoredResume,
    User,
    WorkbenchJob,
    WorkbenchJobKind,
    WorkbenchJobStatus,
)
from app.orchestrator.workbench_jobs import dispatch_import, dispatch_improve, dispatch_tailor

router = APIRouter(tags=["workbench"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _back() -> RedirectResponse:
    return RedirectResponse("/workbench", status_code=303)


def _parse_tags(raw: str) -> list[str]:
    """Comma- or newline-separated tag string → clean list."""
    parts = raw.replace("\n", ",").split(",")
    return [p.strip().lower() for p in parts if p.strip()]


# ── library view ───────────────────────────────────────────────────


@router.get("/workbench", response_class=HTMLResponse, response_model=None)
async def workbench_home(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    profile = (await session.execute(select(CVProfile))).scalars().first()
    summaries = list((await session.execute(
        select(CVSummary).order_by(CVSummary.sort_order, CVSummary.created_at)
    )).scalars().all())
    entries = list((await session.execute(
        select(CVEntry).order_by(CVEntry.sort_order, CVEntry.created_at)
    )).scalars().all())
    # Bullets eager-load via the relationship's order_by; touch them so the
    # template can iterate without a lazy-load surprise.
    for e in entries:
        _ = e.bullets
    skills = list((await session.execute(
        select(CVSkill).order_by(CVSkill.category, CVSkill.sort_order, CVSkill.name)
    )).scalars().all())

    by_kind = {
        "experience": [e for e in entries if e.kind == CVEntryKind.EXPERIENCE],
        "project": [e for e in entries if e.kind == CVEntryKind.PROJECT],
        "education": [e for e in entries if e.kind == CVEntryKind.EDUCATION],
    }
    # Group skills by category for display.
    skills_by_category: dict[str, list[CVSkill]] = {}
    for s in skills:
        skills_by_category.setdefault(s.category, []).append(s)

    recent_imports = list((await session.execute(
        select(WorkbenchJob)
        .where(WorkbenchJob.kind == WorkbenchJobKind.IMPORT)
        .order_by(desc(WorkbenchJob.created_at))
        .limit(5)
    )).scalars().all())

    applications = list((await session.execute(
        select(ResumeApplication).order_by(desc(ResumeApplication.created_at))
    )).scalars().all())

    return templates.TemplateResponse(
        request,
        "workbench.html",
        {
            "user": user,
            "profile": profile,
            "summaries": summaries,
            "entries_by_kind": by_kind,
            "skills_by_category": skills_by_category,
            "entry_kinds": [k.value for k in CVEntryKind],
            "recent_imports": recent_imports,
            "applications": applications,
        },
    )


# ── agent-powered import ───────────────────────────────────────────


def _extract_pdf_text(data: bytes) -> str:
    """Pull text out of an uploaded PDF. pypdf is pure-Python; good enough for
    text-based resumes (it won't OCR a scanned image, but resumes are text)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(pages).strip()


@router.post("/workbench/import", response_class=HTMLResponse, response_model=None)
async def import_resume(
    request: Request,
    resume_pdf: UploadFile | None = File(None),
    resume_text: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Accept a resume — as a PDF upload or pasted text — extract the text,
    and dispatch an incremental-merge import job to a worker."""
    text = resume_text.strip()
    if resume_pdf is not None and resume_pdf.filename:
        raw = await resume_pdf.read()
        if raw:
            try:
                extracted = _extract_pdf_text(raw)
            except Exception as exc:
                raise HTTPException(400, f"could not read the PDF: {exc}")
            if extracted:
                # Prefer the PDF; if both were supplied, the PDF wins.
                text = extracted
    if not text:
        raise HTTPException(400, "provide a resume PDF or paste resume text")

    job = WorkbenchJob(
        kind=WorkbenchJobKind.IMPORT,
        status=WorkbenchJobStatus.PENDING,
        input={"resume_text": text[:60000]},  # generous cap; resumes are small
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # dispatch_import manages its own sessions; on failure it returns an error
    # string we record on the job so the status page can show it.
    err = await dispatch_import(job.id)
    if err:
        async with session.begin():
            j = await session.get(WorkbenchJob, job.id)
            if j:
                j.status = WorkbenchJobStatus.ERROR
                j.error = err

    return RedirectResponse(f"/workbench/imports/{job.id}", status_code=303)


@router.get("/workbench/imports/{job_id}", response_class=HTMLResponse, response_model=None)
async def import_status(
    request: Request,
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    job = await session.get(WorkbenchJob, job_id)
    if not job:
        raise HTTPException(404, "import job not found")
    return templates.TemplateResponse(
        request,
        "workbench_import.html",
        {"user": user, "job": job},
    )


# ── applications + tailoring (W2) ──────────────────────────────────


async def _latest_tailor_job(
    session: AsyncSession, application_id: uuid.UUID
) -> WorkbenchJob | None:
    """Find the most recent tailor job whose input references this application.

    Uses JSON ->> for the lookup since the application_id lives in the job's
    `input` blob. Postgres-only — fine, we already require PG.
    """
    result = await session.execute(
        select(WorkbenchJob)
        .where(
            WorkbenchJob.kind == WorkbenchJobKind.TAILOR,
            WorkbenchJob.input["application_id"].astext == str(application_id),
        )
        .order_by(desc(WorkbenchJob.created_at))
        .limit(1)
    )
    return result.scalars().first()


@router.post("/workbench/applications", response_class=HTMLResponse, response_model=None)
async def create_application(
    request: Request,
    company: str = Form(""),
    role_title: str = Form(""),
    job_description: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    text = job_description.strip()
    if not text:
        raise HTTPException(400, "job description is required")
    app = ResumeApplication(
        company=company.strip(),
        role_title=role_title.strip(),
        job_description=text,
    )
    session.add(app)
    await session.commit()
    await session.refresh(app)
    return RedirectResponse(f"/workbench/applications/{app.id}", status_code=303)


@router.get("/workbench/applications/{application_id}", response_class=HTMLResponse, response_model=None)
async def application_detail(
    request: Request,
    application_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    app = await session.get(ResumeApplication, application_id)
    if not app:
        raise HTTPException(404, "application not found")
    versions = list((await session.execute(
        select(TailoredResume)
        .where(TailoredResume.application_id == application_id)
        .order_by(desc(TailoredResume.version))
    )).scalars().all())
    latest_job = await _latest_tailor_job(session, application_id)
    return templates.TemplateResponse(
        request,
        "workbench_application.html",
        {
            "user": user,
            "application": app,
            "versions": versions,
            "latest_job": latest_job,
        },
    )


@router.post("/workbench/applications/{application_id}/tailor", response_class=HTMLResponse, response_model=None)
async def tailor_application(
    request: Request,
    application_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    app = await session.get(ResumeApplication, application_id)
    if not app:
        raise HTTPException(404, "application not found")

    job = WorkbenchJob(
        kind=WorkbenchJobKind.TAILOR,
        status=WorkbenchJobStatus.PENDING,
        input={"application_id": str(application_id)},
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    err = await dispatch_tailor(job.id)
    if err:
        async with session.begin():
            j = await session.get(WorkbenchJob, job.id)
            if j:
                j.status = WorkbenchJobStatus.ERROR
                j.error = err

    return RedirectResponse(f"/workbench/applications/{application_id}", status_code=303)


@router.post("/workbench/applications/{application_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_application(
    application_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    app = await session.get(ResumeApplication, application_id)
    if app:
        await session.delete(app)  # cascades to TailoredResume rows
        await session.commit()
    return RedirectResponse("/workbench", status_code=303)


# ── bullet improvement (W3) ────────────────────────────────────────


@router.post("/workbench/bullets/{bullet_id}/improve", response_class=HTMLResponse, response_model=None)
async def improve_bullet(
    bullet_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    bullet = await session.get(CVBullet, bullet_id)
    if not bullet:
        raise HTTPException(404, "bullet not found")

    job = WorkbenchJob(
        kind=WorkbenchJobKind.IMPROVE,
        status=WorkbenchJobStatus.PENDING,
        input={"bullet_id": str(bullet_id)},
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    err = await dispatch_improve(job.id)
    if err:
        async with session.begin():
            j = await session.get(WorkbenchJob, job.id)
            if j:
                j.status = WorkbenchJobStatus.ERROR
                j.error = err

    return RedirectResponse(f"/workbench/improvements/{job.id}", status_code=303)


@router.get("/workbench/improvements/{job_id}", response_class=HTMLResponse, response_model=None)
async def improve_review(
    request: Request,
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    job = await session.get(WorkbenchJob, job_id)
    if not job or job.kind != WorkbenchJobKind.IMPROVE:
        raise HTTPException(404, "improvement job not found")
    bullet_id_raw = (job.input or {}).get("bullet_id")
    bullet = None
    entry = None
    if bullet_id_raw:
        try:
            bullet = await session.get(CVBullet, uuid.UUID(str(bullet_id_raw)))
        except (ValueError, TypeError):
            bullet = None
    if bullet:
        entry = await session.get(CVEntry, bullet.entry_id)
    return templates.TemplateResponse(
        request,
        "workbench_improve.html",
        {"user": user, "job": job, "bullet": bullet, "entry": entry},
    )


@router.post("/workbench/improvements/{job_id}/apply", response_class=HTMLResponse, response_model=None)
async def improve_apply(
    job_id: uuid.UUID,
    text: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Accept the (possibly edited) proposed bullet text and write it back."""
    job = await session.get(WorkbenchJob, job_id)
    if not job or job.kind != WorkbenchJobKind.IMPROVE:
        raise HTTPException(404, "improvement job not found")
    bullet_id_raw = (job.input or {}).get("bullet_id")
    if not bullet_id_raw:
        raise HTTPException(400, "job has no bullet_id")
    try:
        bullet = await session.get(CVBullet, uuid.UUID(str(bullet_id_raw)))
    except (ValueError, TypeError):
        bullet = None
    if not bullet:
        raise HTTPException(404, "bullet no longer exists")

    new_text = text.strip()
    if not new_text:
        raise HTTPException(400, "the proposed text is empty")
    bullet.text = new_text
    # Stamp the job with what was actually applied so the audit trail captures
    # the user's edits, not just the model's proposal.
    job.result = {**(job.result or {}), "applied": new_text}
    await session.commit()
    return RedirectResponse("/workbench", status_code=303)


@router.post("/workbench/improvements/{job_id}/dismiss", response_class=HTMLResponse, response_model=None)
async def improve_dismiss(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    job = await session.get(WorkbenchJob, job_id)
    if job:
        job.result = {**(job.result or {}), "dismissed": True}
        await session.commit()
    return RedirectResponse("/workbench", status_code=303)


# ── profile ────────────────────────────────────────────────────────


@router.post("/workbench/profile", response_class=HTMLResponse, response_model=None)
async def upsert_profile(
    request: Request,
    full_name: str = Form(""),
    headline: str = Form(""),
    location: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin: str = Form(""),
    github: str = Form(""),
    website: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    profile = (await session.execute(select(CVProfile))).scalars().first()
    if profile is None:
        profile = CVProfile()
        session.add(profile)
    profile.full_name = full_name.strip()
    profile.headline = headline.strip() or None
    profile.location = location.strip() or None
    profile.email = email.strip() or None
    profile.phone = phone.strip() or None
    profile.links = {
        k: v.strip()
        for k, v in (("linkedin", linkedin), ("github", github), ("website", website))
        if v.strip()
    }
    await session.commit()
    return _back()


# ── summaries ──────────────────────────────────────────────────────


@router.post("/workbench/summaries", response_class=HTMLResponse, response_model=None)
async def add_summary(
    request: Request,
    label: str = Form("default"),
    text: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if text.strip():
        session.add(CVSummary(label=label.strip() or "default", text=text.strip()))
        await session.commit()
    return _back()


@router.post("/workbench/summaries/{summary_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_summary(
    summary_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    obj = await session.get(CVSummary, summary_id)
    if obj:
        await session.delete(obj)
        await session.commit()
    return _back()


# ── entries ────────────────────────────────────────────────────────


@router.post("/workbench/entries", response_class=HTMLResponse, response_model=None)
async def add_entry(
    request: Request,
    kind: str = Form(...),
    organization: str = Form(...),
    title: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    description: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    try:
        entry_kind = CVEntryKind(kind)
    except ValueError:
        raise HTTPException(400, f"unknown entry kind: {kind}")
    session.add(CVEntry(
        kind=entry_kind,
        organization=organization.strip(),
        title=title.strip() or None,
        location=location.strip() or None,
        url=url.strip() or None,
        start_date=start_date.strip() or None,
        end_date=end_date.strip() or None,
        description=description.strip() or None,
    ))
    await session.commit()
    return _back()


@router.post("/workbench/entries/{entry_id}", response_class=HTMLResponse, response_model=None)
async def edit_entry(
    entry_id: uuid.UUID,
    organization: str = Form(...),
    title: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    description: str = Form(""),
    active: bool = Form(False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    entry = await session.get(CVEntry, entry_id)
    if not entry:
        raise HTTPException(404, "entry not found")
    entry.organization = organization.strip()
    entry.title = title.strip() or None
    entry.location = location.strip() or None
    entry.url = url.strip() or None
    entry.start_date = start_date.strip() or None
    entry.end_date = end_date.strip() or None
    entry.description = description.strip() or None
    entry.active = active
    await session.commit()
    return _back()


@router.post("/workbench/entries/{entry_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_entry(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    entry = await session.get(CVEntry, entry_id)
    if entry:
        await session.delete(entry)  # cascades to bullets
        await session.commit()
    return _back()


# ── bullets ────────────────────────────────────────────────────────


@router.post("/workbench/entries/{entry_id}/bullets", response_class=HTMLResponse, response_model=None)
async def add_bullet(
    entry_id: uuid.UUID,
    text: str = Form(...),
    tags: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    entry = await session.get(CVEntry, entry_id)
    if not entry:
        raise HTTPException(404, "entry not found")
    if text.strip():
        session.add(CVBullet(
            entry_id=entry_id,
            text=text.strip(),
            tags=_parse_tags(tags),
        ))
        await session.commit()
    return _back()


@router.post("/workbench/bullets/{bullet_id}", response_class=HTMLResponse, response_model=None)
async def edit_bullet(
    bullet_id: uuid.UUID,
    text: str = Form(...),
    tags: str = Form(""),
    active: bool = Form(False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    bullet = await session.get(CVBullet, bullet_id)
    if not bullet:
        raise HTTPException(404, "bullet not found")
    bullet.text = text.strip()
    bullet.tags = _parse_tags(tags)
    bullet.active = active
    await session.commit()
    return _back()


@router.post("/workbench/bullets/{bullet_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_bullet(
    bullet_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    bullet = await session.get(CVBullet, bullet_id)
    if bullet:
        await session.delete(bullet)
        await session.commit()
    return _back()


# ── skills ─────────────────────────────────────────────────────────


@router.post("/workbench/skills", response_class=HTMLResponse, response_model=None)
async def add_skill(
    category: str = Form(...),
    name: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if category.strip() and name.strip():
        session.add(CVSkill(category=category.strip(), name=name.strip()))
        await session.commit()
    return _back()


@router.post("/workbench/skills/{skill_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_skill(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    skill = await session.get(CVSkill, skill_id)
    if skill:
        await session.delete(skill)
        await session.commit()
    return _back()

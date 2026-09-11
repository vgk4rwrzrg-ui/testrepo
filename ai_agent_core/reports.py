"""Chapter-by-chapter report generation ("write bit by bit").

Why: a big report generated in ONE model call overflows the context window
and arrives half-finished. This engine splits the work:

1. **Outline pass** (tiny): produce chapter titles + one-line briefs.
2. **One chapter per step**: each chapter prompt contains only the report
   request, the outline, and short SUMMARIES of previously written chapters
   -- never their full text. Full text lives in the database.
3. **Assembly on download**: chapters are concatenated into the final
   markdown document.

The pipeline is *poll-driven*: every call to :func:`step` performs exactly
one bounded unit of work (outline, or one chapter). The chat widget keeps
calling the step endpoint and shows each progress note ("Writing chapter
2/6: Findings…"), so it also works on Kubernetes with multiple workers --
no Celery, threads or websockets required.

Plugging in a real LLM::

    # settings.py
    AI_AGENT_REPORT_WRITER = "myproject.ai.report_writer"

    def report_writer(mode, user, job, context):
        if mode == "outline":
            # context: {"request": str, "suggested_chapters": int}
            return [{"title": "...", "brief": "..."}, ...]
        if mode == "chapter":
            # context: {"request", "report_title", "outline": [...],
            #           "chapter_title", "chapter_brief", "chapter_index",
            #           "total_chapters", "previous_summaries": [...]}
            return {"content": "...markdown...", "summary": "..."}

Without the setting, a built-in writer composes each chapter from guarded
table-search results, so the pipeline works end-to-end out of the box.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.utils.module_loading import import_string

from .models import ReportChapter, ReportJob

logger = logging.getLogger(__name__)

SUMMARY_WORDS = 60          # words of each chapter carried forward
DEFAULT_CHAPTERS = 6
MAX_CHAPTERS = 20

REPORT_TRIGGER = re.compile(
    r"^\s*(/report\b|(please\s+)?(write|generate|create|make|build)\s+"
    r"(me\s+)?(a\s+|an\s+)?(big\s+|full\s+|large\s+|detailed\s+|huge\s+"
    r"|comprehensive\s+)?report)", re.IGNORECASE)


def is_report_request(message: str) -> bool:
    return bool(REPORT_TRIGGER.match(message or ""))


def _requested_chapters(text: str) -> int:
    m = re.search(r"(\d{1,2})\s+chapters?", text, re.IGNORECASE)
    if m:
        return max(2, min(MAX_CHAPTERS, int(m.group(1))))
    return DEFAULT_CHAPTERS


def _title_from_request(text: str) -> str:
    t = re.sub(REPORT_TRIGGER, "", text).strip(" :,.-")
    t = re.sub(r"^(on|about|covering|for)\s+", "", t, flags=re.IGNORECASE)
    return (t[:120].strip().title() or "Generated Report")


def _summarize(content: str, words: int = SUMMARY_WORDS) -> str:
    tokens = content.split()
    return " ".join(tokens[:words]) + ("…" if len(tokens) > words else "")


def get_writer():
    path = getattr(settings, "AI_AGENT_REPORT_WRITER", None)
    if path:
        return import_string(path)
    return _builtin_writer


# ---------------------------------------------------------------------------
# Built-in writer (no external LLM needed; uses guarded table search)
# ---------------------------------------------------------------------------

_BASE_OUTLINE = [
    ("Executive Summary", "High-level overview of scope and key takeaways."),
    ("Introduction & Scope", "What was requested and which data was used."),
    ("Data Overview", "Tables and records relevant to the request."),
    ("Detailed Findings", "Record-level findings from the searched tables."),
    ("Analysis", "Patterns, counts and notable observations."),
    ("Recommendations", "Suggested next actions based on the findings."),
    ("Conclusion", "Summary of the report and closing remarks."),
]


def _builtin_writer(mode: str, user, job: ReportJob,
                    context: Dict[str, Any]):
    if mode == "outline":
        n = context.get("suggested_chapters", DEFAULT_CHAPTERS)
        picked = _BASE_OUTLINE[:max(2, min(n, len(_BASE_OUTLINE)))]
        return [{"title": t, "brief": b} for t, b in picked]

    # mode == "chapter"
    from .search import run_search
    topic = f"{context['chapter_title']} {context['request']}"
    results = run_search(user, topic)

    lines: List[str] = []
    lines.append(context.get("chapter_brief") or "")
    prev = context.get("previous_summaries") or []
    if prev:
        lines.append("")
        lines.append(f"_Building on the previous {len(prev)} "
                     f"chapter{'s' if len(prev) != 1 else ''}._")
    if results:
        for block in results:
            lines.append("")
            lines.append(f"**Data from `{block['table']}`:**")
            for row in block["rows"]:
                pairs = ", ".join(f"{k}: {v}" for k, v in row.items())
                lines.append(f"- {pairs}")
        total = sum(len(b["rows"]) for b in results)
        lines.append("")
        lines.append(f"In total, {total} relevant record"
                     f"{'s were' if total != 1 else ' was'} identified for "
                     "this chapter.")
    else:
        lines.append("")
        lines.append("No table records matched this chapter's topic; this "
                     "section is based on the report request alone.")
    content = "\n".join(lines).strip()
    return {"content": content, "summary": _summarize(content)}


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def create_job(user, request_text: str, session_key: str = "",
               conversation=None) -> ReportJob:
    return ReportJob.objects.create(
        user=user if getattr(user, "pk", None) else None,
        session_key=session_key or "",
        conversation=conversation,
        title=_title_from_request(request_text),
        request_text=request_text,
        progress_note="Report queued — building the outline next.",
    )


def step(job: ReportJob, user=None) -> Dict[str, Any]:
    """Perform exactly ONE unit of work and return a progress payload."""
    user = user if getattr(user, "is_authenticated", False) else job.user
    writer = get_writer()
    try:
        if job.status == ReportJob.Status.PENDING:
            return _step_outline(job, user, writer)
        if job.status == ReportJob.Status.WRITING:
            return _step_chapter(job, user, writer)
    except Exception as exc:  # noqa: BLE001 -- job must record any failure
        logger.exception("Report job %s failed", job.pk)
        job.status = ReportJob.Status.FAILED
        job.error = str(exc)[:2000]
        job.progress_note = "Report failed — see error details."
        job.save(update_fields=["status", "error", "progress_note",
                                "updated_at"])
    return _progress(job)


def _step_outline(job, user, writer) -> Dict[str, Any]:
    outline = writer("outline", user, job, {
        "request": job.request_text,
        "suggested_chapters": _requested_chapters(job.request_text),
    })
    outline = list(outline)[:MAX_CHAPTERS]
    if not outline:
        raise ValueError("Writer returned an empty outline.")
    for i, ch in enumerate(outline, start=1):
        ReportChapter.objects.create(
            job=job, index=i, title=str(ch.get("title") or f"Chapter {i}"),
            brief=str(ch.get("brief") or ""))
    job.total_chapters = len(outline)
    job.status = ReportJob.Status.WRITING
    job.progress_note = (f"Outline ready — {len(outline)} chapters planned. "
                         f"Writing chapter 1/{len(outline)}: "
                         f"{outline[0].get('title', '')}…")
    job.save(update_fields=["total_chapters", "status", "progress_note",
                            "updated_at"])
    return _progress(job)


def _step_chapter(job, user, writer) -> Dict[str, Any]:
    chapter = job.chapters.filter(
        status=ReportChapter.Status.PENDING).order_by("index").first()
    if chapter is None:
        return _finish(job)

    # Atomic claim so two overlapping polls never write the same chapter.
    claimed = ReportChapter.objects.filter(
        pk=chapter.pk, status=ReportChapter.Status.PENDING,
    ).update(status=ReportChapter.Status.WRITING)
    if not claimed:
        return _progress(job)

    previous = list(job.chapters.filter(
        status=ReportChapter.Status.DONE).order_by("index"))
    result = writer("chapter", user, job, {
        "request": job.request_text,
        "report_title": job.title,
        "outline": [{"index": c.index, "title": c.title, "brief": c.brief}
                    for c in job.chapters.all()],
        "chapter_title": chapter.title,
        "chapter_brief": chapter.brief,
        "chapter_index": chapter.index,
        "total_chapters": job.total_chapters,
        # THE fix for context overflow: summaries only, never full text.
        "previous_summaries": [
            {"index": c.index, "title": c.title, "summary": c.summary}
            for c in previous],
    })
    if isinstance(result, str):
        result = {"content": result, "summary": _summarize(result)}

    chapter.content = result.get("content") or ""
    chapter.summary = result.get("summary") or _summarize(chapter.content)
    chapter.word_count = len(chapter.content.split())
    chapter.status = ReportChapter.Status.DONE
    chapter.save()

    job.chapters_done = job.chapters.filter(
        status=ReportChapter.Status.DONE).count()
    nxt = job.chapters.filter(
        status=ReportChapter.Status.PENDING).order_by("index").first()
    if nxt is None:
        return _finish(job)
    job.progress_note = (
        f"Finished chapter {chapter.index}/{job.total_chapters}: "
        f"{chapter.title}  ({chapter.word_count} words). "
        f"Writing chapter {nxt.index}/{job.total_chapters}: {nxt.title}…")
    job.save(update_fields=["chapters_done", "progress_note", "updated_at"])
    return _progress(job)


def _finish(job) -> Dict[str, Any]:
    job.chapters_done = job.chapters.filter(
        status=ReportChapter.Status.DONE).count()
    job.status = ReportJob.Status.DONE
    total_words = sum(job.chapters.values_list("word_count", flat=True))
    job.progress_note = (f"Report complete — {job.total_chapters} chapters, "
                         f"{total_words} words. Ready to download.")
    job.save(update_fields=["chapters_done", "status", "progress_note",
                            "updated_at"])
    return _progress(job)


def _progress(job) -> Dict[str, Any]:
    return {
        "job_id": job.pk,
        "status": job.status,
        "note": job.progress_note,
        "chapters_done": job.chapters_done,
        "total_chapters": job.total_chapters,
        "done": job.status == ReportJob.Status.DONE,
        "failed": job.status == ReportJob.Status.FAILED,
        "error": job.error or None,
    }


def assemble_markdown(job: ReportJob) -> str:
    """Concatenate all chapters into the final markdown document."""
    parts = [f"# {job.title}", ""]
    parts.append(f"_Requested: {job.request_text}_")
    parts.append("")
    parts.append("## Table of Contents")
    for c in job.chapters.all():
        parts.append(f"{c.index}. {c.title}")
    for c in job.chapters.all():
        parts.append("")
        parts.append(f"## Chapter {c.index}: {c.title}")
        parts.append("")
        parts.append(c.content or "_(chapter not written)_")
    return "\n".join(parts)

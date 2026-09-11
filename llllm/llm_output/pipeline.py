"""
Two-phase pipeline that wraps AROUND your existing LLM router/orchestrator.

    prep = prepare(user_message, output_type)     # phase 1, BEFORE the LLM
    raw  = <your orchestrator: RAG + router + AI_tool loop, unchanged>
    result = finalize(raw, prep, bubble_template) # phase 2, AFTER the loop

prepare() does not touch the user message; it produces an instruction block
you append to the system prompt / RAG context so the LLM keeps using its
tools normally and only formats its FINAL message.

finalize() turns that final message into either a downloadable file payload
or chat-bubble HTML.
"""
import logging
from dataclasses import dataclass

from django.conf import settings

from .prompts import detect_output_type, format_instructions
from .builders import (BUILDERS, build_chat, parse_llm_json,
                       render_sources_html, _describe_source)

logger = logging.getLogger(__name__)


@dataclass
class PreparedRequest:
    output_type: str        # "excel" | "word" | "pdf" | "chat"
    instructions: str       # append this to your system prompt / RAG context
    user_message: str       # unchanged -- forward this to the orchestrator


def prepare(user_message: str, output_type: str | None = None) -> PreparedRequest:
    """Phase 1: run before your router/orchestrator sees the message."""
    output_type = output_type or detect_output_type(user_message)
    if output_type not in ("excel", "word", "pdf", "chat"):
        output_type = "chat"
    return PreparedRequest(
        output_type=output_type,
        instructions=format_instructions(output_type),
        user_message=user_message,
    )



def _download_url(rel_path: str) -> str:
    """Build the download link for a generated file.

    If settings.LLM_OUTPUT_DOWNLOAD_URL_NAME is set, reverse() that URL
    pattern with the file's path relative to MEDIA_ROOT (e.g.
    "llm_files/abc123_report.docx") so an authenticated download view can
    serve it. Otherwise fall back to direct MEDIA_URL serving.
    """
    url_name = getattr(settings, "LLM_OUTPUT_DOWNLOAD_URL_NAME", None)
    if url_name:
        from django.urls import reverse
        return reverse(url_name, kwargs={"filename": rel_path})
    return settings.MEDIA_URL + rel_path



def _append_sources(data: dict, output_type: str,
                    sources: list | None) -> None:
    """Deterministically append the orchestrator's audit trail to a
    generated document, so provenance can't be hallucinated."""
    if not sources:
        return
    lines = [_describe_source(s) for s in sources]
    if output_type == "excel":
        data.setdefault("sheets", []).append({
            "name": "Data sources",
            "title": "Database queries used to build this workbook",
            "headers": ["#", "Query"],
            "rows": [[i + 1, line] for i, line in enumerate(lines)],
        })
    else:
        data.setdefault("blocks", []).extend([
            {"type": "page_break"},
            {"type": "heading", "level": 2, "text": "Data sources"},
            {"type": "paragraph",
             "text": "This document was generated from the following "
                     "database queries:"},
            {"type": "numbered", "items": lines},
        ])


def finalize(raw_llm_output: str, prep: PreparedRequest,
             bubble_template: str | None = None,
             sources: list | None = None) -> dict:
    """Phase 2: run on the LLM's FINAL message, after the tool loop is done.

    Returns a JSON-serialisable payload for the frontend:
      chat: {"type": "chat", "html": "..."}
      file: {"type": "word", "download_url": "...", "filename": "...",
             "html": "<bubble with attachment link>"}
    On malformed output it degrades to a chat rendering with a "warning" key.
    """
    if prep.output_type == "chat":
        return {"type": "chat",
                "html": build_chat(raw_llm_output, bubble_template,
                                   sources)}

    try:
        data = parse_llm_json(raw_llm_output)
        _append_sources(data, prep.output_type, sources)
        rel_path = BUILDERS[prep.output_type](data)
    except Exception as exc:   # builder errors (openpyxl/docx/reportlab) too
        logger.exception("File build failed for %s", prep.output_type)
        from .builders import build_fail_bubble
        return {
            "type": "chat",
            "html": build_fail_bubble(f"{prep.output_type} file",
                                      f"{exc}\n\n{raw_llm_output}",
                                      bubble_template),
            "warning": f"Could not build {prep.output_type} file: {exc}",
        }

    filename = rel_path.rsplit("/", 1)[-1]
    download_url = _download_url(rel_path)
    return {
        "type": prep.output_type,
        "download_url": download_url,
        "filename": filename,
        # ready-made bubble so the frontend can inject it like any message
        "html": build_chat_attachment(download_url, filename,
                                      prep.output_type, bubble_template,
                                      sources),
    }


_TYPE_ICONS = {"word": "\U0001F4C4", "pdf": "\U0001F4D5",
               "excel": "\U0001F4CA"}


def build_chat_attachment(download_url: str, filename: str,
                          output_type: str,
                          bubble_template: str | None = None,
                          sources: list | None = None) -> str:
    """A chat bubble containing a download link for the generated file."""
    from django.utils.html import escape
    icon = _TYPE_ICONS.get(output_type, "\U0001F4CE")
    url_base = getattr(settings, 'BASE_URL', '')
    URL = f"{url_base}/FieldEngSite/download/llm_files/{escape(filename)}/"
    inner = (
        f'<div class="llm-attachment">'
        f'<span class="llm-attachment-icon">{icon}</span> '
        f'<a href="{URL}" download>{escape(filename)}</a>'
        f'</div>'
    )
    if sources:
        inner += render_sources_html(sources)
    from .builders import DEFAULT_BUBBLE
    template = bubble_template or DEFAULT_BUBBLE
    if "{{content}}" in template:
        return template.replace("{{content}}", inner)
    if "</div>" in template:
        return template.rstrip().replace("</div>", inner + "</div>", 1)
    return template + inner

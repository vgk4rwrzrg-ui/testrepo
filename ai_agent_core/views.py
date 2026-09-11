"""Operational endpoints: liveness probe + double-proxy diagnostics."""
import os

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.urls import get_script_prefix, reverse

from .conf import get_config_dir, get_default_profile_name, list_profiles
from .utils import get_client_ip


def healthz(request):
    """K8s liveness/readiness probe target (no auth, no DB)."""
    return JsonResponse({"status": "ok", "app": "ai_agent_core"})


@staff_member_required
def proxy_debug(request):
    """What did the double proxy actually deliver? Staff-only."""
    return JsonResponse({
        "script_prefix": get_script_prefix(),
        "script_name": request.META.get("SCRIPT_NAME", ""),
        "path": request.path,
        "path_info": request.path_info,
        "scheme": request.scheme,
        "is_secure": request.is_secure(),
        "host": request.get_host(),
        "client_ip": get_client_ip(request),
        "remote_addr": request.META.get("REMOTE_ADDR", ""),
        "x_forwarded_for": request.META.get("HTTP_X_FORWARDED_FOR", ""),
        "x_forwarded_proto": request.META.get("HTTP_X_FORWARDED_PROTO", ""),
        "x_forwarded_host": request.META.get("HTTP_X_FORWARDED_HOST", ""),
        "x_forwarded_prefix": request.META.get("HTTP_X_FORWARDED_PREFIX", ""),
        "jupyterhub_service_prefix":
            os.environ.get("JUPYTERHUB_SERVICE_PREFIX", ""),
        "config_dir": str(get_config_dir()),
        "active_profile": get_default_profile_name(),
        "available_profiles": list_profiles(),
    })


# ---------------------------------------------------------------------------
# Bot chat API + widget demo page
# ---------------------------------------------------------------------------
import json

from django.shortcuts import render
from django.views.decorators.http import require_POST

from .models import BotChatMessage, BotConversation, BotProfile
from .search import answer


@require_POST
def bot_chat(request):
    """Chat endpoint used by the floating widget (CSRF-protected POST).

    Body: {"message": str, "conversation_id": int|null}
    Returns: {"reply": str, "results": [...], "conversation_id": int,
              "bot": {"name": ...}}
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "Empty message."}, status=400)
    if len(message) > 2000:
        return JsonResponse({"error": "Message too long."}, status=400)

    bot = BotProfile.get_default()
    user = request.user if request.user.is_authenticated else None

    from .reports import create_job, is_report_request
    wants_report = is_report_request(message)

    conversation = None
    conv_id = payload.get("conversation_id")
    if conv_id:
        conversation = BotConversation.objects.filter(pk=conv_id).first()
        if conversation and conversation.user and conversation.user != user:
            conversation = None  # never continue someone else's thread
    if conversation is None:
        if not request.session.session_key:
            request.session.save()
        conversation = BotConversation.objects.create(
            user=user,
            session_key=request.session.session_key or "",
            bot_name=getattr(bot, "name", "Assistant"))

    history = [{"role": m.role, "content": m.content}
               for m in conversation.messages.all()[:20]]
    BotChatMessage.objects.create(conversation=conversation,
                                  role="user", content=message)

    if wants_report:
        job = create_job(request.user, message,
                         session_key=request.session.session_key or "",
                         conversation=conversation)
        reply = (f"Starting your report: “{job.title}”. I write big reports "
                 "bit by bit — outline first, then one chapter at a time — "
                 "so nothing gets cut off. I will post progress here.")
        BotChatMessage.objects.create(conversation=conversation, role="bot",
                                      content=reply)
        return JsonResponse({
            "reply": reply,
            "results": [],
            "conversation_id": conversation.pk,
            "report_job": job.pk,
            "bot": {"name": getattr(bot, "name", "Assistant")},
        })

    result = answer(request.user, message, bot, history)
    BotChatMessage.objects.create(conversation=conversation, role="bot",
                                  content=result["reply"],
                                  results=result["results"])
    return JsonResponse({
        "reply": result["reply"],
        "results": result["results"] if (bot is None or bot.show_result_cards)
                   else [],
        "conversation_id": conversation.pk,
        "bot": {"name": getattr(bot, "name", "Assistant")},
    })


def widget_demo(request):
    """Standalone page rendering the floating bot widget (for smoke tests)."""
    return render(request, "ai_agent_core/widget_demo.html")


# ---------------------------------------------------------------------------
# Chaptered report endpoints (poll-driven: each step call = one chapter)
# ---------------------------------------------------------------------------
from django.http import Http404, HttpResponse

from .models import ReportJob
from .reports import assemble_markdown, step


def _get_owned_job(request, job_id) -> ReportJob:
    job = ReportJob.objects.filter(pk=job_id).first()
    if job is None or not job.owned_by(request):
        raise Http404("Report not found.")
    return job


@require_POST
def report_step(request, job_id: int):
    """Advance the report by ONE unit of work (outline or one chapter).

    The widget polls this endpoint until ``done``; each response carries the
    human-readable progress note ("Writing chapter 2/6: Findings…").
    """
    job = _get_owned_job(request, job_id)
    payload = step(job, user=request.user)
    if payload["done"]:
        payload["download_url"] = request.build_absolute_uri(
            reverse("ai_agent_core:report_download", args=[job.pk]))
        if job.conversation_id:
            BotChatMessage.objects.create(
                conversation_id=job.conversation_id, role="bot",
                content=job.progress_note)
    return JsonResponse(payload)


def report_status(request, job_id: int):
    """Read-only progress (no work performed)."""
    job = _get_owned_job(request, job_id)
    from .reports import _progress
    return JsonResponse(_progress(job))


def report_download(request, job_id: int):
    """The assembled report as a markdown download."""
    job = _get_owned_job(request, job_id)
    if job.status != ReportJob.Status.DONE:
        return JsonResponse({"error": "Report is not finished yet.",
                             "status": job.status}, status=409)
    md = assemble_markdown(job)
    slug = "".join(c if c.isalnum() or c in "-_ " else ""
                   for c in job.title).strip().replace(" ", "_") or "report"
    resp = HttpResponse(md, content_type="text/markdown; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slug}.md"'
    return resp

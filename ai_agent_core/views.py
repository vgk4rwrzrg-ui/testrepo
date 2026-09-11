"""Operational endpoints: liveness probe + double-proxy diagnostics."""
import os

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.urls import get_script_prefix

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

"""Middleware for JupyterHub's double-proxy topology.

Request flow::

    Client -> Ingress (Nginx/Traefik) -> JupyterHub CHP -> user pod -> Django

Two proxies rewrite the request before Django sees it:

* the ingress terminates TLS and sets ``X-Forwarded-Proto/Host/For``;
* the configurable-http-proxy routes ``/user/<username>/...`` into the pod,
  usually WITHOUT stripping the prefix; ``JUPYTERHUB_SERVICE_PREFIX`` in the
  pod environment names that prefix (jupyter-server-proxy also sends
  ``X-Forwarded-Prefix``).

``JupyterHubProxyMiddleware`` makes both hops transparent:

1. resolves the active prefix (settings > X-Forwarded-Prefix >
   JUPYTERHUB_SERVICE_PREFIX env),
2. moves it from ``PATH_INFO`` into ``SCRIPT_NAME`` and sets Django's global
   script prefix, so ``reverse()``, ``{% url %}``, redirects, the admin and
   ``LOGIN_URL`` all emit fully prefixed URLs,
3. resolves the true client IP from ``X-Forwarded-For`` using an explicit
   trusted-hop count (never blindly trusting the first entry, which a client
   can spoof).

Place it FIRST in ``MIDDLEWARE``.  Scheme/host trust is handled by Django
itself -- see ``ai_agent_core.security.apply_proxy_security``.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.urls import set_script_prefix

logger = logging.getLogger(__name__)


class JupyterHubProxyMiddleware:
    """Normalise prefix routing + client identity behind the double proxy."""

    def __init__(self, get_response):
        self.get_response = get_response
        # Explicit setting wins; JUPYTERHUB_SERVICE_PREFIX is injected into
        # every single-user pod by KubeSpawner.
        self.forced_prefix = (
            getattr(settings, "AI_AGENT_FORCED_PREFIX", None)
            or os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "")
        ).rstrip("/")
        self.trust_prefix_header = getattr(
            settings, "AI_AGENT_TRUST_PREFIX_HEADER", True)
        # Ingress + CHP = 2 trusted hops by default.
        self.trusted_proxies = int(getattr(
            settings, "AI_AGENT_TRUSTED_PROXY_COUNT", 2))

    # -- prefix ------------------------------------------------------------
    def _resolve_prefix(self, request) -> str:
        if self.trust_prefix_header:
            header = request.META.get("HTTP_X_FORWARDED_PREFIX", "").strip()
            if header:
                return "/" + header.strip("/")
        if self.forced_prefix:
            return "/" + self.forced_prefix.strip("/")
        # Last resort: infer /user/<name> or /services/<name> from the path.
        path = request.META.get("PATH_INFO", "") or request.path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("user", "services"):
            return f"/{parts[0]}/{parts[1]}"
        return ""

    def _apply_prefix(self, request, prefix: str) -> None:
        script = prefix + "/"
        path_info = request.META.get("PATH_INFO", request.path)
        if path_info == prefix or path_info.startswith(script):
            # CHP forwarded the full external path: strip the prefix.
            new_path = path_info[len(prefix):] or "/"
        else:
            # Prefix already stripped upstream: keep the path as-is.
            new_path = path_info
        request.META["SCRIPT_NAME"] = prefix
        request.META["PATH_INFO"] = new_path
        request.path_info = new_path
        request.path = prefix + new_path
        set_script_prefix(script)

    # -- client IP ----------------------------------------------------------
    def _resolve_client_ip(self, request) -> str:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if not xff:
            return request.META.get("REMOTE_ADDR", "")
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        # Each trusted proxy appended one hop; the client is N-from-the-end.
        if len(hops) >= self.trusted_proxies:
            return hops[-self.trusted_proxies]
        return hops[0]

    def __call__(self, request):
        prefix = self._resolve_prefix(request)
        if prefix:
            self._apply_prefix(request, prefix)
        else:
            set_script_prefix("/")
        request.META["AI_AGENT_CLIENT_IP"] = self._resolve_client_ip(request)
        return self.get_response(request)

"""One-call proxy security hardening for the project settings module.

Usage (bottom of ``settings.py``)::

    from ai_agent_core.security import apply_proxy_security
    apply_proxy_security(globals())

Everything is environment-driven, so the same image runs in every cluster:

===============================  ==============================================
Env var                          Effect
===============================  ==============================================
AI_AGENT_CSRF_TRUSTED_ORIGINS    comma list -> CSRF_TRUSTED_ORIGINS
AI_AGENT_ALLOWED_HOSTS           comma list -> ALLOWED_HOSTS
AI_AGENT_BEHIND_TLS_PROXY        "0" disables the secure-cookie block (dev)
===============================  ==============================================
"""
from __future__ import annotations

import os
from typing import Any, Dict


def _csv_env(name: str) -> list:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def apply_proxy_security(settings_globals: Dict[str, Any]) -> None:
    s = settings_globals

    # --- Trust the forwarded protocol set by the OUTER proxy (TLS edge). ---
    # Both proxies must be configured to overwrite (never append blindly)
    # X-Forwarded-Proto so a client cannot inject it.
    s.setdefault("SECURE_PROXY_SSL_HEADER", ("HTTP_X_FORWARDED_PROTO", "https"))

    # --- Build absolute URLs from the public host, not the pod host. -------
    s.setdefault("USE_X_FORWARDED_HOST", True)
    s.setdefault("USE_X_FORWARDED_PORT", True)

    # --- Host / origin allowlists from the environment. ---------------------
    hosts = _csv_env("AI_AGENT_ALLOWED_HOSTS")
    if hosts:
        s["ALLOWED_HOSTS"] = hosts
    origins = _csv_env("AI_AGENT_CSRF_TRUSTED_ORIGINS")
    if origins:
        s["CSRF_TRUSTED_ORIGINS"] = origins

    if os.environ.get("AI_AGENT_BEHIND_TLS_PROXY", "1") != "0":
        s.setdefault("SESSION_COOKIE_SECURE", True)
        s.setdefault("CSRF_COOKIE_SECURE", True)

    # --- Per-user cookie scoping under the JupyterHub prefix. ---------------
    # Prevents session/CSRF cookie collisions between /user/alice/ and
    # /user/bob/ on the shared public host.
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "").rstrip("/")
    if prefix:
        s.setdefault("SESSION_COOKIE_PATH", prefix + "/")
        s.setdefault("CSRF_COOKIE_PATH", prefix + "/")
        s.setdefault("FORCE_SCRIPT_NAME", None)  # middleware handles prefixing
        s.setdefault("LOGIN_URL", f"{prefix}/admin/login/")

    # Referer checks against the public origin when HTTPS terminates upstream.
    s.setdefault("SECURE_REFERRER_POLICY", "same-origin")

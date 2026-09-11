"""Deployment system checks: ``python manage.py check --deploy`` friendly."""
import os

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.compatibility)
def config_dir_check(app_configs, **kwargs):
    from .conf import get_config_dir, yaml_available

    errors = []
    cfg_dir = get_config_dir()
    if not cfg_dir.is_dir():
        errors.append(Warning(
            f"AI_AGENT_CONFIG_DIR '{cfg_dir}' does not exist or is not a "
            "directory. Agent profiles cannot be loaded until the ConfigMap/"
            "PVC is mounted at this path.",
            hint="Set AI_AGENT_CONFIG_DIR in settings or the environment to "
                 "the mount point of your K8s ConfigMap / PVC.",
            id="ai_agent_core.W001",
        ))
    if not yaml_available():
        errors.append(Warning(
            "PyYAML is not installed; only JSON agent profiles will load.",
            hint="pip install PyYAML to enable .yaml/.yml profiles.",
            id="ai_agent_core.W002",
        ))
    return errors


@register(Tags.security)
def proxy_security_check(app_configs, **kwargs):
    errors = []
    if not getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        errors.append(Warning(
            "SECURE_PROXY_SSL_HEADER is not set. Behind the double proxy "
            "Django will treat forwarded HTTPS traffic as plain HTTP, which "
            "breaks secure cookies and CSRF origin checks.",
            hint="SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')",
            id="ai_agent_core.W003",
        ))
    if not getattr(settings, "USE_X_FORWARDED_HOST", False):
        errors.append(Warning(
            "USE_X_FORWARDED_HOST is False. Absolute URLs will be built from "
            "the pod-internal host instead of the public ingress host.",
            hint="USE_X_FORWARDED_HOST = True (and forward X-Forwarded-Host "
                 "from the outer proxy).",
            id="ai_agent_core.W004",
        ))
    if not getattr(settings, "CSRF_TRUSTED_ORIGINS", None):
        errors.append(Warning(
            "CSRF_TRUSTED_ORIGINS is empty; POSTs arriving through the "
            "ingress will fail Django's Origin check.",
            hint="Set CSRF_TRUSTED_ORIGINS (or env AI_AGENT_CSRF_TRUSTED_ORIGINS "
                 "with ai_agent_core.security.apply_proxy_security).",
            id="ai_agent_core.W005",
        ))
    return errors

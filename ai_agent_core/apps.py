"""AppConfig for ai_agent_core."""
from django.apps import AppConfig


class AiAgentCoreConfig(AppConfig):
    """Pluggable, enterprise-ready AI agent core.

    Consolidates the legacy standalone modules (read-only table search
    toolset, agent engine settings) into one reusable Django app that is
    Kubernetes-aware and safe behind JupyterHub's double-proxy topology.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "ai_agent_core"
    verbose_name = "AI Agent Core"

    def ready(self):
        # Register deployment system checks (config dir, YAML support,
        # proxy security settings).  Imported here so Django is fully
        # loaded first.
        from . import checks  # noqa: F401

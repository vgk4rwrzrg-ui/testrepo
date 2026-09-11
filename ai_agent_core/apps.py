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

        # Invalidate the dynamic table registry whenever an admin edits
        # registrations (models are loaded now, so imports are safe).
        from django.db.models.signals import (m2m_changed, post_delete,
                                              post_save)
        from . import registry
        from .models import SearchableField, SearchableTable
        for model in (SearchableTable, SearchableField):
            post_save.connect(registry.mark_dirty, sender=model)
            post_delete.connect(registry.mark_dirty, sender=model)
        m2m_changed.connect(registry.mark_dirty,
                            sender=SearchableField.groups.through)

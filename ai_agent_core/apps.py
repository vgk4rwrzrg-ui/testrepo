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
                                              post_save, post_migrate)
        from . import registry
        from .models import SearchableField, SearchableTable

        for model in (SearchableTable, SearchableField):
            post_save.connect(registry.mark_dirty, sender=model)
            post_delete.connect(registry.mark_dirty, sender=model)
        m2m_changed.connect(registry.mark_dirty,
                            sender=SearchableField.groups.through)

        # Auto-populate searchable tables after migrations are applied.
        post_migrate.connect(self.auto_register_tables)

    def auto_register_tables(self, **kwargs):
        """Automatically register all project models as searchable."""
        from django.apps import apps as django_apps
        from .models import SearchableTable, SearchableField, TableAccessPolicy
        # Blacklist of apps that should not be auto-registered
        blacklist = {"auth", "admin", "contenttypes", "sessions", "messages", "staticfiles", "ai_agent_core"}

        for model in django_apps.get_models():
            app_label = model._meta.app_label
            if app_label in blacklist:
                continue

            table, created = SearchableTable.objects.get_or_create(
                app_label=app_label,
                model_name=model.__name__,
                defaults={
                    "enabled": True,
                    "description": f"Auto-registered model {app_label}.{model.__name__}"
                }
            )

            # Ensure there is a baseline 'allow-all' policy for this table.
            # This grants READ access to all authenticated users by default.
            TableAccessPolicy.objects.get_or_create(
                app_label=app_label,
                model_name=model.__name__,
                defaults={
                    "applies_to_all_authenticated": True,
                    "access_level": TableAccessPolicy.AccessLevel.READ,
                    "priority": 0,
                    "notes": "Default auto-generated allow-all policy."
                }
            )

            # If the table was just created, or we want to ensure fields are present,
            # populate the searchable fields.
            for field in model._meta.get_fields():
                # Only register actual model fields (not reverse relations, etc.)
                # and avoid fields that are usually not useful for search (like ManyToMany through fields)
                if not field.concrete or field.auto_created:
                    continue
                
                # We can add a filter here for specific field types if needed, 
                # but generally, concrete fields are good candidates.
                SearchableField.objects.get_or_create(
                    table=table,
                    field_name=field.name,
                    defaults={
                        "description": f"Auto-registered field {field.name} of {app_label}.{model.__name__}"
                    }
                )

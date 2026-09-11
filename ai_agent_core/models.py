"""Database-backed table access control, hooked into Django Groups."""
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models


class TableAccessPolicy(models.Model):
    """Maps an allowlisted table to the Django Groups permitted to search it.

    Policies are evaluated highest ``priority`` first; the first policy whose
    groups intersect the requesting user's groups wins.  A DENY policy at a
    high priority therefore overrides broader ALLOW policies below it.

    The (app_label, model_name) pair must exist in the legacy engine's
    ``ALLOWED_MODELS`` allowlist -- policies can only narrow the legacy
    surface, never widen it, preserving the source repository's data
    integrity constraints.
    """

    class AccessLevel(models.TextChoices):
        READ = "read", "Read (fetch + aggregate)"
        AGGREGATE_ONLY = "aggregate_only", "Aggregate only (no row-level reads)"
        DENY = "deny", "Deny"

    app_label = models.CharField(
        max_length=100,
        help_text="Django app label of the target table (e.g. 'Req_tracker').",
    )
    model_name = models.CharField(
        max_length=100,
        help_text="Model class name exactly as in the legacy allowlist "
                  "(e.g. 'Requistion').",
    )
    groups = models.ManyToManyField(
        Group,
        blank=True,
        related_name="table_access_policies",
        help_text="Groups this policy applies to. Leave empty and tick "
                  "'applies to all authenticated' for a global policy.",
    )
    applies_to_all_authenticated = models.BooleanField(
        default=False,
        help_text="If set, the policy matches every authenticated user "
                  "regardless of group membership.",
    )
    access_level = models.CharField(
        max_length=20,
        choices=AccessLevel.choices,
        default=AccessLevel.READ,
    )
    allowed_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional JSON list narrowing the legacy field allowlist "
                  "for this audience (empty = every legacy-allowed field). "
                  "Must be a subset of the legacy allowlist.",
    )
    max_rows_per_call = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional per-call row cap for this audience (further "
                  "clamped by the legacy MAX_LIMIT).",
    )
    priority = models.IntegerField(
        default=0,
        help_text="Higher priority policies are evaluated first.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "table access policy"
        verbose_name_plural = "table access policies"
        ordering = ["-priority", "app_label", "model_name"]
        indexes = [
            models.Index(fields=["app_label", "model_name", "is_active"]),
        ]

    def __str__(self):
        return (f"[{self.get_access_level_display()}] "
                f"{self.app_label}.{self.model_name} (prio {self.priority})")

    @property
    def model_path(self) -> str:
        return f"{self.app_label}.{self.model_name}"

    def clean(self):
        from .legacy.ai_tools import ALLOWED_MODELS

        errors = {}
        allowed = ALLOWED_MODELS.get(self.app_label, {})
        if self.app_label not in ALLOWED_MODELS:
            errors["app_label"] = (
                f"'{self.app_label}' is not in the legacy allowlist. "
                f"Valid app labels: {', '.join(sorted(ALLOWED_MODELS))}."
            )
        elif self.model_name not in allowed:
            errors["model_name"] = (
                f"'{self.model_name}' is not an allowlisted model of "
                f"'{self.app_label}'. Valid: {', '.join(sorted(allowed))}."
            )
        elif self.allowed_fields:
            if not isinstance(self.allowed_fields, list):
                errors["allowed_fields"] = "Must be a JSON list of field names."
            else:
                legal = set(allowed[self.model_name])
                bad = [f for f in self.allowed_fields if f not in legal]
                if bad:
                    errors["allowed_fields"] = (
                        f"Not in the legacy field allowlist: {', '.join(bad)}. "
                        f"Legal fields: {', '.join(sorted(legal))}."
                    )
        if errors:
            raise ValidationError(errors)


class TableAccessAudit(models.Model):
    """Immutable audit trail of every guarded table-search decision."""

    class Action(models.TextChoices):
        FETCH = "fetch", "Fetch rows"
        AGGREGATE = "aggregate", "Aggregate"
        DESCRIBE = "describe", "Describe model"
        LIST = "list", "List models"

    user = models.ForeignKey(
        "auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="table_access_audits",
    )
    username = models.CharField(max_length=150, blank=True)
    model_path = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    was_allowed = models.BooleanField()
    policy = models.ForeignKey(
        TableAccessPolicy, null=True, blank=True, on_delete=models.SET_NULL,
    )
    reason = models.CharField(max_length=255, blank=True)
    query = models.JSONField(default=dict, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        verdict = "ALLOW" if self.was_allowed else "DENY"
        return f"{verdict} {self.username} {self.action} {self.model_path}"

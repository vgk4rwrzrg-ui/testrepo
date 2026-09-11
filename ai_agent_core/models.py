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


# ===========================================================================
# Bot identity, dynamic table registration, and chat persistence
# ===========================================================================

class BotProfile(models.Model):
    """Admin-configurable identity + chat-window behaviour of the bot."""

    class WindowMode(models.TextChoices):
        RIGHT = "right", "Docked right"
        LEFT = "left", "Docked left"
        POPUP = "popup", "Centered popup"

    name = models.CharField(max_length=80, default="Assistant")
    avatar_emoji = models.CharField(
        max_length=8, blank=True, default="",
        help_text="Optional emoji launcher. Leave BLANK to use the built-in "
                  "animated robot SVG (recommended). Ignored if an image URL "
                  "is set.")
    avatar_image_url = models.URLField(
        blank=True,
        help_text="Optional image URL for the launcher/avatar.")
    greeting = models.CharField(
        max_length=255,
        default="Hi! Ask me anything about your data.",
        help_text="First message shown when the chat opens.")
    personality = models.TextField(
        blank=True,
        help_text="Free-text persona description; passed to the LLM handler "
                  "and merged over the file-based profile.")
    system_prompt = models.TextField(
        blank=True,
        help_text="Optional system prompt override for the LLM handler.")
    window_mode = models.CharField(
        max_length=10, choices=WindowMode.choices, default=WindowMode.RIGHT,
        help_text="Where the chat window appears: docked left, docked right, "
                  "or a centered popup.")

    class ThemeMode(models.TextChoices):
        AUTO = "auto", "Auto (follow site / OS dark mode)"
        LIGHT = "light", "Always light"
        DARK = "dark", "Always dark"

    theme_mode = models.CharField(
        max_length=10, choices=ThemeMode.choices, default=ThemeMode.AUTO,
        help_text="Auto detects the host page's dark/light mode "
                  "(data-theme / data-bs-theme / 'dark' class on <html>/<body>) "
                  "and falls back to the OS prefers-color-scheme; or force "
                  "one theme.")
    primary_color = models.CharField(
        max_length=7, default="#2563eb",
        help_text="Hex accent color for the widget.")
    placeholder_text = models.CharField(
        max_length=120, default="Type a question…")
    show_result_cards = models.BooleanField(
        default=True,
        help_text="Render matching rows as cards under the bot reply.")
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="The default profile rendered by {% ai_bot_widget %}.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_window_mode_display()})"

    @classmethod
    def get_default(cls):
        return (cls.objects.filter(is_active=True)
                .order_by("-is_default", "id").first())


class SearchableTable(models.Model):
    """Dynamically registers a project model as searchable by the bot.

    Complements the static legacy allowlist: rows here are merged into the
    engine's allowlist at runtime (see ``ai_agent_core.registry``), so any
    Django model in the project can be opened to the bot from the admin --
    no code change, no redeploy.
    """
    app_label = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    description = models.CharField(
        max_length=255, blank=True,
        help_text="Shown to the LLM/agent as table context.")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("app_label", "model_name")]
        ordering = ["app_label", "model_name"]

    def __str__(self):
        return f"{self.app_label}.{self.model_name}"

    def clean(self):
        from django.apps import apps as django_apps
        try:
            django_apps.get_model(self.app_label, self.model_name)
        except LookupError:
            raise ValidationError(
                f"No installed model '{self.app_label}.{self.model_name}'.")


class SearchableField(models.Model):
    """A field opened for searching, optionally restricted to Groups.

    Leave ``groups`` empty to expose the field to every user who can access
    the table; add groups to open the field ONLY to those groups
    ("certain fields for certain users").
    """
    table = models.ForeignKey(
        SearchableTable, on_delete=models.CASCADE, related_name="fields")
    field_name = models.CharField(max_length=100)
    groups = models.ManyToManyField(
        Group, blank=True, related_name="visible_search_fields",
        help_text="Empty = visible to everyone with table access; otherwise "
                  "only these groups see this field.")
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = [("table", "field_name")]
        ordering = ["table", "field_name"]

    def __str__(self):
        return f"{self.table}.{self.field_name}"

    def clean(self):
        from django.apps import apps as django_apps
        from .legacy.ai_tools import DENIED_FIELDS
        if self.field_name.lower() in DENIED_FIELDS:
            raise ValidationError(
                {"field_name": f"'{self.field_name}' is security-denied and "
                               "can never be exposed."})
        try:
            model = django_apps.get_model(self.table.app_label,
                                          self.table.model_name)
        except LookupError:
            return  # table.clean() reports this
        try:
            model._meta.get_field(self.field_name)
        except Exception:
            raise ValidationError(
                {"field_name": f"'{self.field_name}' is not a field of "
                               f"{self.table}."})


class BotConversation(models.Model):
    """One chat thread between a user (or anonymous session) and the bot."""
    user = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="bot_conversations")
    session_key = models.CharField(max_length=64, blank=True, db_index=True)
    bot_name = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        who = self.user.username if self.user else f"anon:{self.session_key[:8]}"
        return f"Conversation #{self.pk} with {who}"


class BotChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        BOT = "bot", "Bot"

    conversation = models.ForeignKey(
        BotConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    results = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"


# ===========================================================================
# Chaptered report generation (big reports written bit by bit)
# ===========================================================================

class ReportJob(models.Model):
    """A large report generated chapter-by-chapter so no single LLM call
    ever holds the whole document in context.

    Pipeline (one unit of work per ``step()``):
      PENDING -> outline pass (small) -> WRITING -> one chapter per step,
      each prompt containing only the outline + short SUMMARIES of prior
      chapters -> DONE (assembled on download).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending (outline not started)"
        WRITING = "writing", "Writing chapters"
        DONE = "done", "Complete"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="report_jobs")
    session_key = models.CharField(max_length=64, blank=True, db_index=True)
    conversation = models.ForeignKey(
        "BotConversation", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="report_jobs")
    title = models.CharField(max_length=200, blank=True)
    request_text = models.TextField(
        help_text="The user's original report request.")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    total_chapters = models.PositiveIntegerField(default=0)
    chapters_done = models.PositiveIntegerField(default=0)
    progress_note = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report #{self.pk}: {self.title or self.request_text[:40]}"

    def owned_by(self, request) -> bool:
        if getattr(request.user, "is_superuser", False):
            return True
        if self.user_id:
            return request.user.is_authenticated and \
                request.user.pk == self.user_id
        return bool(self.session_key) and \
            request.session.session_key == self.session_key


class ReportChapter(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        WRITING = "writing", "Writing"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    job = models.ForeignKey(ReportJob, on_delete=models.CASCADE,
                            related_name="chapters")
    index = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    brief = models.TextField(
        blank=True, help_text="One-line outline brief for this chapter.")
    content = models.TextField(blank=True)
    summary = models.TextField(
        blank=True,
        help_text="Short summary carried into later chapters' prompts "
                  "instead of the full text (keeps context small).")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    word_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["index"]
        unique_together = [("job", "index")]

    def __str__(self):
        return f"Ch.{self.index} {self.title} [{self.status}]"

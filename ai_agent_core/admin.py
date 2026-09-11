"""Django Admin backend: map table access policies to Groups on the fly."""
from django import forms
from django.contrib import admin, messages

from .legacy.ai_tools import ALLOWED_MODELS
from .models import TableAccessAudit, TableAccessPolicy


class TableAccessPolicyForm(forms.ModelForm):
    """Dropdowns constrained to the legacy allowlist -- admins can only
    narrow the legacy surface, never widen it."""

    app_label = forms.ChoiceField(
        choices=lambda: [(a, a) for a in sorted(ALLOWED_MODELS)])
    model_name = forms.ChoiceField(
        choices=lambda: sorted(
            {(m, f"{a}.{m}") for a, ms in ALLOWED_MODELS.items() for m in ms},
            key=lambda c: c[1],
        ))

    class Meta:
        model = TableAccessPolicy
        fields = "__all__"


@admin.register(TableAccessPolicy)
class TableAccessPolicyAdmin(admin.ModelAdmin):
    form = TableAccessPolicyForm
    list_display = ("model_path", "access_level", "group_list", "priority",
                    "applies_to_all_authenticated", "is_active", "updated_at")
    list_filter = ("access_level", "is_active", "app_label",
                   "applies_to_all_authenticated", "groups")
    search_fields = ("app_label", "model_name", "notes", "groups__name")
    filter_horizontal = ("groups",)
    ordering = ("-priority", "app_label", "model_name")
    actions = ("activate_policies", "deactivate_policies")
    fieldsets = (
        ("Target table (legacy allowlist)", {
            "fields": ("app_label", "model_name"),
        }),
        ("Audience", {
            "fields": ("groups", "applies_to_all_authenticated"),
        }),
        ("Grant", {
            "fields": ("access_level", "allowed_fields", "max_rows_per_call",
                       "priority", "is_active"),
        }),
        ("Bookkeeping", {
            "classes": ("collapse",),
            "fields": ("notes", "created_at", "updated_at"),
        }),
    )
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="groups")
    def group_list(self, obj):
        names = [g.name for g in obj.groups.all()]
        if obj.applies_to_all_authenticated:
            names.append("(all authenticated)")
        return ", ".join(names) or "—"

    @admin.action(description="Activate selected policies")
    def activate_policies(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"{n} policies activated.", messages.SUCCESS)

    @admin.action(description="Deactivate selected policies")
    def deactivate_policies(self, request, queryset):
        n = queryset.update(is_active=False)
        self.message_user(request, f"{n} policies deactivated.", messages.WARNING)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")


@admin.register(TableAccessAudit)
class TableAccessAuditAdmin(admin.ModelAdmin):
    """Read-only audit trail."""
    list_display = ("created_at", "username", "action", "model_path",
                    "was_allowed", "reason")
    list_filter = ("was_allowed", "action", "created_at")
    search_fields = ("username", "model_path", "reason")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in TableAccessAudit._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Retention cleanup is a management concern, not a UI button.
        return request.user.is_superuser

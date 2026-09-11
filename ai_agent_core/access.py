"""Group-based access decisions layered over the legacy allowlist.

The legacy engine (``ai_agent_core.legacy.ai_tools``) already enforces its
own allowlist/denylist/pagination integrity constraints -- untouched.  This
module answers ONE additional question before the legacy engine ever runs:

    "Is this *user*, by virtue of their Django Groups, permitted to search
     this table, and with which field subset / row cap?"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from django.conf import settings

from . import registry
from .legacy.ai_tools import ALLOWED_MODELS, resolve_model_path
from .models import TableAccessAudit, TableAccessPolicy

logger = logging.getLogger(__name__)


@dataclass
class AccessDecision:
    allowed: bool
    model_path: str = ""
    access_level: str = ""
    fields: List[str] = field(default_factory=list)
    max_rows: Optional[int] = None
    policy: Optional[TableAccessPolicy] = None
    reason: str = ""


def _default_access() -> str:
    """Site-wide fallback when no policy matches: 'deny' (default) or 'read'."""
    return getattr(settings, "AI_AGENT_DEFAULT_TABLE_ACCESS", "deny")


def _superuser_bypass() -> bool:
    return getattr(settings, "AI_AGENT_SUPERUSER_BYPASS", True)


def check_access(user, model_path: str, action: str = "fetch") -> AccessDecision:
    """Resolve the effective policy for ``user`` on ``model_path``.

    ``action`` is 'fetch' or 'aggregate'.  Raises ``ValueError`` for an
    invalid/non-allowlisted model path (same semantics as the legacy engine).
    """
    registry.sync_registry()  # merge admin-registered tables (lazy, TTL)
    app_label, model_name = resolve_model_path(model_path)  # legacy validation
    resolved = f"{app_label}.{model_name}"
    # Allowlisted fields, narrowed to those this user's groups may see.
    legacy_fields = registry.visible_fields(
        user, app_label, model_name,
        ALLOWED_MODELS[app_label][model_name])

    if user is None or not getattr(user, "is_authenticated", False):
        return AccessDecision(False, resolved,
                              reason="Anonymous access is not permitted.")

    if getattr(user, "is_superuser", False) and _superuser_bypass():
        return AccessDecision(True, resolved, "read", legacy_fields,
                              reason="superuser bypass")

    user_group_ids = set(user.groups.values_list("id", flat=True))
    policies = (
        TableAccessPolicy.objects
        .filter(app_label=app_label, model_name=model_name, is_active=True)
        .prefetch_related("groups")
        .order_by("-priority", "id")
    )

    for policy in policies:
        policy_group_ids = {g.id for g in policy.groups.all()}
        matches = policy.applies_to_all_authenticated or (
            policy_group_ids & user_group_ids
        )
        if not matches:
            continue

        if policy.access_level == TableAccessPolicy.AccessLevel.DENY:
            return AccessDecision(False, resolved, policy=policy,
                                  reason=f"Denied by policy #{policy.pk}.")

        if (policy.access_level == TableAccessPolicy.AccessLevel.AGGREGATE_ONLY
                and action == "fetch"):
            return AccessDecision(
                False, resolved, policy=policy,
                reason=f"Policy #{policy.pk} allows aggregation only; "
                       "row-level fetch is not permitted.")

        eff = ([f for f in policy.allowed_fields if f in legacy_fields]
               if policy.allowed_fields else legacy_fields)
        return AccessDecision(True, resolved, policy.access_level, eff,
                              max_rows=policy.max_rows_per_call,
                              policy=policy,
                              reason=f"Allowed by policy #{policy.pk}.")

    if _default_access() == "read":
        return AccessDecision(True, resolved, "read", legacy_fields,
                              reason="site default: read")
    return AccessDecision(False, resolved,
                          reason="No policy grants your groups access to "
                                 f"{resolved}.")


def accessible_models(user) -> Dict[str, List[str]]:
    """Subset of the legacy catalogue this user may see (for list_models)."""
    registry.sync_registry()
    out: Dict[str, List[str]] = {}
    for app_label, model_map in ALLOWED_MODELS.items():
        for model_name in model_map:
            try:
                decision = check_access(user, f"{app_label}.{model_name}",
                                        action="aggregate")
            except ValueError:  # pragma: no cover
                continue
            if decision.allowed:
                out[f"{app_label}.{model_name}"] = decision.fields
    return out


def record_audit(user, action: str, decision: Optional[AccessDecision],
                 model_path: str = "", query: Optional[Dict[str, Any]] = None,
                 row_count: Optional[int] = None) -> None:
    """Persist an audit row (best-effort; auditing never breaks a request)."""
    if not getattr(settings, "AI_AGENT_AUDIT_ENABLED", True):
        return
    try:
        TableAccessAudit.objects.create(
            user=user if getattr(user, "pk", None) else None,
            username=getattr(user, "username", "") or "anonymous",
            model_path=(decision.model_path if decision else model_path),
            action=action,
            was_allowed=bool(decision and decision.allowed),
            policy=decision.policy if decision else None,
            reason=(decision.reason if decision else "")[:255],
            query=query or {},
            row_count=row_count,
        )
    except Exception:  # pragma: no cover
        logger.exception("Failed to write TableAccessAudit row")

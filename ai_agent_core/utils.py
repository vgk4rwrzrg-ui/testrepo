"""Guarded, group-aware wrappers around the legacy table-search engine.

The legacy functions in :mod:`ai_agent_core.legacy.ai_tools` are called
verbatim -- allowlist, denylist, alias resolution, lookup validation,
pagination clamps, and the uniform result envelope are all preserved exactly
as in the source repository.  These wrappers add, in order:

1. a Django-Group policy check (:func:`ai_agent_core.access.check_access`),
2. per-policy field narrowing and row caps,
3. an immutable audit record.

Expose ONLY these ``guarded_*`` functions to the agent's tool registry.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .access import accessible_models, check_access, record_audit
from .legacy import ai_tools as legacy

# Re-export the legacy envelope helpers so callers keep one import surface.
_ok = legacy._ok
_err = legacy._err

PERMISSION_DENIED = "PERMISSION_DENIED"


def _denied(decision) -> Dict[str, Any]:
    return _err(PERMISSION_DENIED, decision.reason,
                "Ask an administrator to map your Django group to this "
                "table in the AI Agent Core admin (Table access policies).")


def _first_segment_ok(decision, keys) -> Optional[str]:
    """When a policy narrows fields, forbid filters/order_by on hidden ones."""
    from .legacy.ai_tools import resolve_field
    model_name = decision.model_path.split(".")[1]
    allowed = set(decision.fields)
    for key in keys:
        first = resolve_field(model_name, key.lstrip("-").split("__")[0])
        if first not in allowed:
            return first
    return None


def guarded_fetch_data(user,
                       model_path: str,
                       filters: Optional[Dict[str, Any]] = None,
                       exclude: Optional[Dict[str, Any]] = None,
                       limit: int = legacy.DEFAULT_LIMIT,
                       offset: int = 0,
                       order_by: Optional[str] = None,
                       include_m2m: bool = False) -> Dict[str, Any]:
    """Group-guarded :func:`legacy.fetch_data` (identical envelope)."""
    try:
        decision = check_access(user, model_path, action="fetch")
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Use guarded_list_models() to see valid paths.")

    query = {"filters": filters or {}, "exclude": exclude or {},
             "limit": limit, "offset": offset, "order_by": order_by}
    if not decision.allowed:
        record_audit(user, "fetch", decision, query=query)
        return _denied(decision)

    hidden = _first_segment_ok(
        decision,
        list((filters or {}).keys()) + list((exclude or {}).keys())
        + ([order_by] if order_by else []),
    )
    if hidden is not None:
        record_audit(user, "fetch", decision, query=query)
        return _err(PERMISSION_DENIED,
                    f"Field '{hidden}' is hidden from your groups by policy.",
                    "Filter/order only on fields returned by "
                    "guarded_describe_model().")

    if decision.max_rows:
        limit = min(int(limit), decision.max_rows)

    result = legacy.fetch_data(decision.model_path, filters=filters,
                               exclude=exclude, user=user, limit=limit,
                               offset=offset, order_by=order_by,
                               include_m2m=include_m2m)

    # Post-filter rows to the policy's field subset (legacy output untouched
    # when the policy imposes no narrowing).
    if result.get("ok") and decision.fields:
        allowed = set(decision.fields)
        rows = result["data"].get("rows", [])
        result["data"]["rows"] = [
            {k: v for k, v in row.items() if k in allowed} for row in rows
        ]

    record_audit(user, "fetch", decision, query=query,
                 row_count=(len(result["data"]["rows"])
                            if result.get("ok") else None))
    return result


def guarded_aggregate_data(user,
                           model_path: str,
                           func: str,
                           field: str,
                           filters: Optional[Dict[str, Any]] = None,
                           group_by: Optional[str] = None,
                           limit: int = legacy.DEFAULT_LIMIT) -> Dict[str, Any]:
    """Group-guarded :func:`legacy.aggregate_data` (identical envelope)."""
    try:
        decision = check_access(user, model_path, action="aggregate")
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Use guarded_list_models() to see valid paths.")

    query = {"func": func, "field": field, "filters": filters or {},
             "group_by": group_by, "limit": limit}
    if not decision.allowed:
        record_audit(user, "aggregate", decision, query=query)
        return _denied(decision)

    hidden = _first_segment_ok(
        decision,
        [field] + list((filters or {}).keys())
        + ([group_by] if group_by else []),
    )
    if hidden is not None:
        record_audit(user, "aggregate", decision, query=query)
        return _err(PERMISSION_DENIED,
                    f"Field '{hidden}' is hidden from your groups by policy.",
                    "Aggregate only on fields returned by "
                    "guarded_describe_model().")

    result = legacy.aggregate_data(decision.model_path, func, field,
                                   filters=filters, user=user,
                                   group_by=group_by, limit=limit)
    # Audit the calculation AND its computed outcome, so the log shows
    # exactly what formula ran and what value the AI was given.
    query["ok"] = bool(result.get("ok"))
    if result.get("ok"):
        query["result"] = result.get("data")   # already JSON-safe (legacy)
    else:
        query["error"] = (result.get("error") or {}).get("code")
    record_audit(user, "aggregate", decision, query=query,
                 row_count=(len(result["data"])
                            if result.get("ok")
                            and isinstance(result.get("data"), list)
                            else None))
    return result


def guarded_list_models(user) -> Dict[str, Any]:
    """Like ``legacy.list_models`` but filtered to the user's group grants."""
    data = accessible_models(user)
    record_audit(user, "list", None,
                 query={"visible_models": sorted(data.keys())})
    return _ok(data)


def guarded_describe_model(user, model_path: str) -> Dict[str, Any]:
    """Group-guarded ``legacy.describe_model``; fields narrowed by policy."""
    try:
        decision = check_access(user, model_path, action="aggregate")
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Use guarded_list_models() to see valid paths.")
    if not decision.allowed:
        record_audit(user, "describe", decision)
        return _denied(decision)

    result = legacy.describe_model(decision.model_path)
    if result.get("ok") and decision.fields:
        allowed = set(decision.fields)
        result["data"]["fields"] = [
            f for f in result["data"]["fields"] if f["name"] in allowed
        ]
    record_audit(user, "describe", decision)
    return result


# ---------------------------------------------------------------------------
# Proxy-aware URL helpers
# ---------------------------------------------------------------------------

def build_prefixed_url(request, path: str) -> str:
    """Absolute URL that survives the JupyterHub double proxy.

    Combines the forwarded scheme/host (via Django's SECURE_PROXY_SSL_HEADER /
    USE_X_FORWARDED_HOST) with the script prefix resolved by
    ``JupyterHubProxyMiddleware``.
    """
    from django.urls import get_script_prefix
    prefix = get_script_prefix().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return request.build_absolute_uri(prefix + path)


def get_client_ip(request) -> str:
    """Real client IP as resolved by the middleware (or REMOTE_ADDR)."""
    return request.META.get("AI_AGENT_CLIENT_IP",
                            request.META.get("REMOTE_ADDR", ""))

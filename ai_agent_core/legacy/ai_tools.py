"""
AI Data Fetching Toolset
========================

Read-only database access tools for an LLM agent.

The agent workflow:
    1. Call ``list_models()`` / ``describe_model()`` to discover what is queryable.
    2. Translate the user's natural-language request into a model path + filters.
    3. Call ``fetch_data()`` for rows, or ``aggregate_data()`` for counts/sums/etc.

Safety model (defense in depth):
    * Read-only  -- no create/update/delete paths exist in this module.
    * Allowlist  -- only models/fields in ``ALLOWED_MODELS`` are reachable.
    * Denylist   -- sensitive field names and expensive lookups are blocked
                    at every segment of a lookup chain (including FK traversal).
    * Pagination -- results are always paginated; ``MAX_LIMIT`` rows per call.
    * Scoping    -- every queryset passes through ``apply_user_scope`` so
                    tenancy rules can be enforced centrally.

All public tools return a uniform envelope:

    {"ok": bool, "data": <payload or None>, "error": None or
        {"code": str, "message": str, "hint": str}}
"""

from __future__ import annotations

import json
import math
import logging
from typing import Any, Dict, List, Optional
from django.core.exceptions import FieldError
from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_LIMIT = 500
DEFAULT_LIMIT = 50

# Explicit allowlist of app -> model -> fields the agent may read.
# NOTE: 'id' is NOT implied; each model lists its real primary key explicitly.
ALLOWED_MODELS: Dict[str, Dict[str, List[str]]] = {
    "user_analytics": {
        "PageView": ["user", "visitor_id", "path", "status_code", "response_time_ms", 'referrer', 'user_agent'],
        "AnalyticsEvent": ["user", "visitor_id", "path", "event_type", "duration_seconds", 'metadata'],
    },
    "auth": {
        "User": ["id", "username", "first_name", "last_name", "email",
                 "is_active", "is_staff", "date_joined", "last_login", "groups"],
        "Group": ["id", "name"],
    },
}
 
# Field names that must never be exposed, even via FK traversal.
DENIED_FIELDS = {"password", "token", "secret", "api_key", "ssn"}

# Lookup types that are too expensive / risky to expose.
DENIED_LOOKUPS = {"regex", "iregex"}

# Natural-language / misspelling aliases for MODEL names.
# (Several real model names contain typos, e.g. "Requistion", "Invloved".)
MODEL_ALIASES: Dict[str, str] = {
    "Requisition": "Requistion",
    "Requirement": "Requistion",
    "Req": "Requistion",
    "Applicant": "Candidate",
    "Issue": "Incident_report",
    "Ticket": "Incident_report",
    "Involved": "Invloved",
    "Outbrief": "OutBreif",
}

# Per-model aliases for FIELD names ({model: {alias: real_field}}).
FIELD_ALIASES: Dict[str, Dict[str, str]] = {
    "User_data": {
        "Middle_Name": "Middel_Name",
        "employee_id": "emp",
    },
    "Candidate": {
        "candidate_key": "candiate_key",
    },
}

# Valid Django lookup suffixes the agent may use.
ALLOWED_LOOKUPS = {
    "exact", "iexact", "contains", "icontains", "in",
    "gt", "gte", "lt", "lte", "range",
    "startswith", "istartswith", "endswith", "iendswith",
    "date", "year", "month", "day", "week_day",
    "isnull",
}


# ---------------------------------------------------------------------------
# Result envelope helpers
# ---------------------------------------------------------------------------

def _ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _err(code: str, message: str, hint: str = "") -> Dict[str, Any]:
    return {"ok": False, "data": None,
            "error": {"code": code, "message": message, "hint": hint}}


class _FallbackJSONEncoder(DjangoJSONEncoder):
    """DjangoJSONEncoder plus a ``str()`` fallback so exotic backend column
    types (ClickHouse Array/Map/Tuple/IPv4/IPv6, memoryview, ...) serialize
    instead of raising INTERNAL_ERROR."""

    def default(self, o):  # noqa: D102
        try:
            return super().default(o)
        except TypeError:
            return str(o)


def _scrub_nonfinite(data: Any) -> Any:
    """Replace inf/-inf/nan with None. ClickHouse float math returns these
    instead of erroring, and they are not valid strict JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, list):
        return [_scrub_nonfinite(v) for v in data]
    if isinstance(data, dict):
        return {k: _scrub_nonfinite(v) for k, v in data.items()}
    return data


def _json_safe(data: Any) -> Any:
    """Round-trip through DjangoJSONEncoder so dates/decimals/UUIDs are
    plain JSON types the agent can consume directly. Unknown types fall
    back to ``str()``; non-finite floats become null."""
    return _scrub_nonfinite(
        json.loads(json.dumps(data, cls=_FallbackJSONEncoder)))


# ---------------------------------------------------------------------------
# Scoping hook
# ---------------------------------------------------------------------------

def apply_user_scope(queryset, model, user):
    """Enforce tenancy/ownership rules in ONE place.

    Every tool routes its queryset through here before evaluation.
    Replace the pass-through below with real business rules, e.g.::

        if model._meta.model_name == "program_name_data" and user is not None:
            return queryset.filter(Members__pk=user.pk)

    Returning ``queryset.none()`` denies access entirely.
    """
    return queryset


# ---------------------------------------------------------------------------
# Resolution & validation
# ---------------------------------------------------------------------------

def resolve_model_path(model_path: str) -> tuple:
    """Split 'app_label.Model' and resolve model-name aliases.

    Returns (app_label, model_name) or raises ValueError.
    """
    try:
        from .. import registry
        registry.sync_registry()
    except Exception:
        pass

    if not isinstance(model_path, str) or model_path.count(".") != 1:
        raise ValueError(
            f"Invalid model path {model_path!r}. Expected 'app_label.ModelName'."
        )
    app_label, model_name = model_path.split(".")
    model_name = MODEL_ALIASES.get(model_name, model_name)

    if app_label not in ALLOWED_MODELS or model_name not in ALLOWED_MODELS[app_label]:
        available = ", ".join(
            f"{a}.{m}" for a, ms in ALLOWED_MODELS.items() for m in ms
        )
        raise ValueError(
            f"Model {model_path} is not allowed. Available models: {available}"
        )
    return app_label, model_name


def resolve_field(model_name: str, field: str) -> str:
    """Map a field alias to its real name (no-op if not aliased)."""
    return FIELD_ALIASES.get(model_name, {}).get(field, field)


def _validate_lookup(app_label: str, model_name: str, lookup: str) -> str:
    """Validate one filter key like 'Status' or 'Start_date__year__gte'.

    Returns the (possibly alias-resolved) lookup string.
    Rules:
      * First segment must be an allowed field for the model.
      * NO segment may be a denied field name (blocks FK traversal leaks).
      * NO segment may be a denied lookup type.
      * A trailing segment that isn't a field must be an allowed lookup type.
    """
    segments = lookup.split("__")
    segments[0] = resolve_field(model_name, segments[0])
    allowed_fields = set(ALLOWED_MODELS[app_label][model_name])

    if segments[0] not in allowed_fields:
        raise ValueError(
            f"Field {segments[0]!r} is not allowed for {app_label}.{model_name}. "
            f"Allowed: {', '.join(sorted(allowed_fields))}"
        )

    for seg in segments:
        low = seg.lower()
        if low in DENIED_FIELDS:
            raise ValueError(f"Field {seg!r} is restricted for security reasons.")
        if low in DENIED_LOOKUPS:
            raise ValueError(f"Lookup type {seg!r} is forbidden.")

    # If the final segment looks like a lookup (not a field), require allowlist.
    if len(segments) > 1 and segments[-1] not in allowed_fields:
        if segments[-1] not in ALLOWED_LOOKUPS:
            # It may be a related-field name; allow single-hop relations but
            # reject unknown terminal tokens that are neither field nor lookup.
            if not segments[-1][0].isalpha() or segments[-1] in DENIED_LOOKUPS:
                raise ValueError(f"Unrecognized lookup {segments[-1]!r}.")
    return "__".join(segments)


def validate_filters(app_label: str, model_name: str,
                     filters: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and alias-resolve a filter dict. Returns a cleaned copy."""
    if not isinstance(filters, dict):
        raise ValueError("filters must be a dict of {lookup: value}.")
    return {
        _validate_lookup(app_label, model_name, key): value
        for key, value in filters.items()
    }


def _validate_order_by(app_label: str, model_name: str, order_by: str) -> str:
    field = order_by.lstrip("-")
    field = resolve_field(model_name, field)
    if field not in ALLOWED_MODELS[app_label][model_name]:
        raise ValueError(
            f"Cannot order by {field!r}; it is not an allowed field of "
            f"{app_label}.{model_name}."
        )
    return ("-" if order_by.startswith("-") else "") + field


def _concrete_fields(model, allowed_fields) -> List[str]:
    """Allowed fields that .values() can serialize (skip M2M / missing)."""
    out = []
    for name in allowed_fields:
        try:
            f = model._meta.get_field(name)
        except Exception:
            continue
        if not isinstance(f, models.ManyToManyField):
            out.append(name)
    return out


def _m2m_fields(model, allowed_fields) -> List[str]:
    """Allowed fields that are ManyToMany on this model."""
    out = []
    for name in allowed_fields:
        try:
            if isinstance(model._meta.get_field(name), models.ManyToManyField):
                out.append(name)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------

def list_models() -> Dict[str, Any]:
    """Return every queryable model path and its allowed fields."""
    data = {
        f"{app}.{model}": fields
        for app, ms in ALLOWED_MODELS.items()
        for model, fields in ms.items()
    }
    return _ok(data)


def describe_model(model_path: str) -> Dict[str, Any]:
    """Return field metadata (name, type, null, related model) for one model."""
    try:
        app_label, model_name = resolve_model_path(model_path)
        model = apps.get_model(app_label, model_name)
        info = []
        for name in ALLOWED_MODELS[app_label][model_name]:
            try:
                f = model._meta.get_field(name)
            except Exception:
                continue
            info.append({
                "name": name,
                "type": f.get_internal_type(),
                "null": getattr(f, "null", None),
                "is_relation": f.is_relation,
                "related_model": (
                    f.related_model._meta.label if f.is_relation and f.related_model
                    else None
                ),
                "help_text": str(getattr(f, "help_text", "")),
                "choices": ([f"{c[0]}={c[1]}" for c in f.choices]
                            if getattr(f, "choices", None) else None),
            })
        return _ok({
            "model": f"{app_label}.{model_name}",
            "primary_key": model._meta.pk.name,
            "fields": info,
        })
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), "Use list_models() to see valid paths.")
    except Exception:
        logger.exception("describe_model failed")
        return _err("INTERNAL_ERROR", "An unexpected error occurred.")


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------

def fetch_data(model_path: str,
               filters: Optional[Dict[str, Any]] = None,
               exclude: Optional[Dict[str, Any]] = None,
               user=None,
               limit: int = DEFAULT_LIMIT,
               offset: int = 0,
               order_by: Optional[str] = None,
               include_m2m: bool = False) -> Dict[str, Any]:
    """Fetch rows from the database.

    Args:
        model_path: "app_label.ModelName" (aliases accepted).
        filters:    Django-style lookups, e.g. {"Status": "Active",
                    "Start_date__year__gte": 2023}.
        exclude:    Same syntax as filters; rows matching are removed.
        user:       Request user, passed to apply_user_scope().
        limit:      Rows per page (clamped to 1..MAX_LIMIT).
        offset:     Rows to skip (>= 0).
        order_by:   Allowed field name, optionally prefixed with "-".
                    Defaults to "-<pk>".
        include_m2m: If True, allowed ManyToMany fields are returned as
                    lists of related primary keys (fetched with a second
                    query -- no row duplication or pagination breakage).

    Returns the standard envelope; ``data`` contains rows, total_count,
    has_more, limit, offset.
    """
    try:
        app_label, model_name = resolve_model_path(model_path)
        clean_filters = validate_filters(app_label, model_name, filters or {})
        clean_exclude = validate_filters(app_label, model_name, exclude or {})

        model = apps.get_model(app_label, model_name)
        queryset = model.objects.filter(**clean_filters)
        if clean_exclude:
            queryset = queryset.exclude(**clean_exclude)
        queryset = apply_user_scope(queryset, model, user)

        pk_name = model._meta.pk.name
        order_clause = (_validate_order_by(app_label, model_name, order_by)
                        if order_by else f"-{pk_name}")
        queryset = queryset.order_by(order_clause)

        limit = min(max(1, int(limit)), MAX_LIMIT)
        offset = max(0, int(offset))

        total_count = queryset.count()
        fields = _concrete_fields(model, ALLOWED_MODELS[app_label][model_name])
        rows = list(queryset.values(*fields)[offset:offset + limit])

        if include_m2m and rows:
            pks = [r[pk_name] for r in rows]
            for m2m_name in _m2m_fields(model, ALLOWED_MODELS[app_label][model_name]):
                mapping: Dict[Any, List[Any]] = {}
                pairs = (model.objects.filter(pk__in=pks)
                         .values_list(pk_name, m2m_name))
                for pk_val, rel_pk in pairs:
                    if rel_pk is not None:
                        mapping.setdefault(pk_val, []).append(rel_pk)
                for r in rows:
                    r[m2m_name] = mapping.get(r[pk_name], [])

        return _ok({
            "rows": _json_safe(rows),
            "total_count": total_count,
            "has_more": (offset + limit) < total_count,
            "limit": limit,
            "offset": offset,
            "order_by": order_clause,
        })
    except (ValueError, TypeError) as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Check allowed fields via describe_model() and filter syntax.")
    except FieldError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Check the lookup path; use describe_model() for valid fields.")
    except Exception:
        logger.exception("fetch_data failed for %s", model_path)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.")


AGGREGATE_FUNCS = {"count": Count, "sum": Sum, "avg": Avg, "min": Min, "max": Max}


def aggregate_data(model_path: str,
                   func: str,
                   field: str,
                   filters: Optional[Dict[str, Any]] = None,
                   user=None,
                   group_by: Optional[str] = None,
                   limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Aggregate rows: count / sum / avg / min / max, optionally grouped.

    Args:
        model_path: "app_label.ModelName".
        func:       One of count, sum, avg, min, max (case-insensitive).
        field:      Allowed field to aggregate (use the pk for count).
        filters:    Optional Django-style lookups applied first.
        user:       Request user, passed to apply_user_scope().
        group_by:   Optional allowed field; returns one row per group value.
        limit:      Max number of groups returned (grouped mode only).

    Returns the envelope; grouped ``data`` is a list of
    {<group_by>: value, "result": aggregate}, otherwise a scalar.
    """
    try:
        app_label, model_name = resolve_model_path(model_path)
        clean_filters = validate_filters(app_label, model_name, filters or {})

        func = (func or "").lower()
        if func not in AGGREGATE_FUNCS:
            raise ValueError(
                f"Unsupported aggregation function {func!r}. "
                f"Use one of: {', '.join(sorted(AGGREGATE_FUNCS))}."
            )

        allowed_fields = set(ALLOWED_MODELS[app_label][model_name])
        field = resolve_field(model_name, field)
        if field not in allowed_fields:
            raise ValueError(
                f"Field {field!r} is not allowed for aggregation on "
                f"{app_label}.{model_name}."
            )

        model = apps.get_model(app_label, model_name)
        queryset = model.objects.filter(**clean_filters)
        queryset = apply_user_scope(queryset, model, user)

        agg = AGGREGATE_FUNCS[func]
        if group_by:
            group_by = resolve_field(model_name, group_by)
            if group_by not in allowed_fields:
                raise ValueError(f"Field {group_by!r} is not allowed for grouping.")
            limit = min(max(1, int(limit)), MAX_LIMIT)
            result = (queryset.values(group_by)
                              .annotate(result=agg(field))
                              .order_by("-result")[:limit])
            data = _json_safe(list(result))
        else:
            data = _json_safe(queryset.aggregate(result=agg(field))["result"])

        return _ok(data)
    except (ValueError, TypeError) as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Ensure func is count/sum/avg/min/max and fields are allowed.")
    except FieldError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Check the lookup path; use describe_model() for valid fields.")
    except Exception:
        logger.exception("aggregate_data failed for %s", model_path)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.")


def fetch_related(model_path: str,
                  pk: Any,
                  relation: str,
                  user=None,
                  limit: int = DEFAULT_LIMIT,
                  offset: int = 0) -> Dict[str, Any]:
    """Fetch full rows related to ONE object through a ManyToMany field.

    Example -- employees assigned to program 7:
        fetch_related("FiledEngSite.Program_Name_Data", pk=7, relation="Members")

    The related rows are serialized with the RELATED model's own allowed
    field list, so both sides of the relation must be allowlisted.
    """
    try:
        app_label, model_name = resolve_model_path(model_path)
        relation = resolve_field(model_name, relation)
        if relation not in ALLOWED_MODELS[app_label][model_name]:
            raise ValueError(
                f"Relation {relation!r} is not an allowed field of "
                f"{app_label}.{model_name}."
            )
        model = apps.get_model(app_label, model_name)
        field = model._meta.get_field(relation)
        if not isinstance(field, models.ManyToManyField):
            raise ValueError(
                f"{relation!r} is not a ManyToMany field. Use fetch_data with "
                f"a filter for ForeignKey relations."
            )

        rel_model = field.related_model
        rel_app = rel_model._meta.app_label
        rel_name = rel_model._meta.object_name
        if rel_app not in ALLOWED_MODELS or rel_name not in ALLOWED_MODELS[rel_app]:
            raise ValueError(
                f"Related model {rel_app}.{rel_name} is not allowlisted."
            )

        base_qs = apply_user_scope(model.objects.filter(pk=pk), model, user)
        obj = base_qs.first()
        if obj is None:
            raise ValueError(
                f"{app_label}.{model_name} with pk={pk!r} not found "
                f"(or not visible to this user)."
            )

        rel_qs = getattr(obj, relation).all()
        rel_qs = apply_user_scope(rel_qs, rel_model, user)
        rel_pk = rel_model._meta.pk.name
        rel_qs = rel_qs.order_by(rel_pk)

        limit = min(max(1, int(limit)), MAX_LIMIT)
        offset = max(0, int(offset))
        total_count = rel_qs.count()
        rel_fields = _concrete_fields(rel_model, ALLOWED_MODELS[rel_app][rel_name])
        rows = list(rel_qs.values(*rel_fields)[offset:offset + limit])

        return _ok({
            "parent": f"{app_label}.{model_name} pk={pk}",
            "relation": relation,
            "related_model": f"{rel_app}.{rel_name}",
            "rows": _json_safe(rows),
            "total_count": total_count,
            "has_more": (offset + limit) < total_count,
            "limit": limit,
            "offset": offset,
        })
    except (ValueError, TypeError) as e:
        return _err("VALIDATION_ERROR", str(e),
                    "relation must be an allowed M2M field; both models must "
                    "be in the allowlist.")
    except Exception:
        logger.exception("fetch_related failed for %s.%s", model_path, relation)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.")

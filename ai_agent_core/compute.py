"""Audited, in-database formulas over allowlisted fields.

Answers "take field A, divide by field B, multiply by field A" WITHOUT the
LLM doing arithmetic in its head:

* The expression is parsed with Python's ``ast`` and only these node types
  are accepted: numbers, field names, ``+ - * /``, unary minus, parentheses.
  No calls, attributes, subscripts, comparisons or names outside the
  policy-narrowed field allowlist -> no injection surface.
* It is compiled to Django ``F()`` expressions and executed by the database
  (exact arithmetic over ALL matching rows, not a 500-row sample the model
  eyeballs). Division is wrapped in ``NULLIF(denominator, 0)`` so divide-
  by-zero yields NULL instead of an error or a hallucinated number.
* Cross-table formulas, two ways:
    1. **Related tables** (FK / M2M path): use ``Relation__field`` operands,
       e.g. ``Hours / Requistion__Budget``. Each hop is validated against the
       legacy allowlist/denylist and the *related* table's Group policy.
    2. **Unrelated tables**: pass ``scalars`` -- named aggregate values from
       other tables (each computed through the audited aggregate tool) that
       become constants in the expression, e.g.
       ``expression="Salary / avg_salary"`` with
       ``scalars={"avg_salary": {"model": "FiledEngSite.User_data",
                                 "func": "avg", "field": "Salary"}}``.
* Every call writes an audit row whose ``query`` JSON holds the expression,
  the fields it touched, filters, scalars used, and the computed result.
"""
from __future__ import annotations

import ast
import dataclasses
import logging
from typing import Any, Dict, List, Optional, Tuple

from django.apps import apps as django_apps
from django.core.exceptions import FieldError
from django.db import models
from django.db.models import (Avg, Count, ExpressionWrapper, F, FloatField,
                              Max, Min, Sum, Value)
from django.db.models.functions import Cast, NullIf

from .access import check_access, record_audit
from .legacy import ai_tools as legacy

logger = logging.getLogger(__name__)

_ok, _err = legacy._ok, legacy._err
PERMISSION_DENIED = "PERMISSION_DENIED"
AGG = {"sum": Sum, "avg": Avg, "min": Min, "max": Max, "count": Count}
MAX_EXPRESSION_LEN = 300
MAX_OPERANDS = 12


class FormulaError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Expression validation
# ---------------------------------------------------------------------------

_ALLOWED_BIN = (ast.Add, ast.Sub, ast.Mult, ast.Div)


def parse_expression(expression: str) -> Tuple[ast.AST, List[str]]:
    """Parse + whitelist-check. Returns (tree, operand names)."""
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("expression must be a non-empty string.")
    if len(expression) > MAX_EXPRESSION_LEN:
        raise FormulaError("expression too long.")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Cannot parse expression: {exc.msg}") from exc

    names: List[str] = []

    def walk(node):
        if isinstance(node, ast.Expression):
            walk(node.body)
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BIN):
                raise FormulaError("Only + - * / are allowed.")
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise FormulaError("Only unary minus/plus are allowed.")
            walk(node.operand)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or \
                    not isinstance(node.value, (int, float)):
                raise FormulaError("Only numeric literals are allowed.")
        elif isinstance(node, ast.Name):
            if node.id not in names:
                names.append(node.id)
            if len(names) > MAX_OPERANDS:
                raise FormulaError(f"At most {MAX_OPERANDS} operands.")
        else:
            raise FormulaError(
                f"Disallowed syntax: {type(node).__name__}. Use fields, "
                "numbers, + - * / and parentheses only.")

    walk(tree)
    return tree, names


# ---------------------------------------------------------------------------
# Operand resolution (same table, related table, or scalar)
# ---------------------------------------------------------------------------

def _resolve_field_path(user, app_label: str, model_name: str,
                        decision_fields: List[str], path: str) -> str:
    """Validate ``field`` or ``Rel__field`` against allowlists + policies.

    Returns the ORM lookup string. Raises FormulaError / PermissionError.
    """
    path = legacy.resolve_field(model_name, path)
    segments = path.split("__")
    if segments[0] not in decision_fields:
        raise PermissionError(
            f"Field '{segments[0]}' is not available to you on "
            f"{app_label}.{model_name}.")
    # Legacy denylist / lookup validation at every segment.
    legacy._validate_lookup(app_label, model_name, path)

    model = django_apps.get_model(app_label, model_name)
    for i, seg in enumerate(segments):
        try:
            field = model._meta.get_field(seg)
        except Exception as exc:
            raise FormulaError(f"Unknown field '{seg}' in '{path}'.") from exc
        last = i == len(segments) - 1
        if field.is_relation and field.related_model is not None and not last:
            rel = field.related_model
            rel_label = f"{rel._meta.app_label}.{rel._meta.object_name}"
            # The RELATED table needs its own policy grant for this user,
            # and the next segment must be visible under that grant.
            rel_decision = check_access(user, rel_label, action="aggregate")
            if not rel_decision.allowed:
                raise PermissionError(
                    f"Cross-table access to {rel_label} denied: "
                    f"{rel_decision.reason}")
            nxt = legacy.resolve_field(rel._meta.object_name, segments[i + 1])
            if nxt not in rel_decision.fields:
                raise PermissionError(
                    f"Field '{nxt}' on {rel_label} is not available to you.")
            model = rel
        elif last and field.is_relation:
            raise FormulaError(
                f"'{path}' ends on a relation; add the numeric field, "
                f"e.g. '{path}__<field>'.")
    return path


def _resolve_scalars(user, scalars: Optional[Dict[str, Dict[str, Any]]]
                     ) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Compute named constants from OTHER tables via the audited aggregate."""
    from .utils import guarded_aggregate_data
    values: Dict[str, float] = {}
    trail: List[Dict[str, Any]] = []
    for name, spec in (scalars or {}).items():
        if not isinstance(spec, dict) or "model" not in spec:
            raise FormulaError(
                f"scalar '{name}' must be {{model, func, field, filters?}}.")
        res = guarded_aggregate_data(
            user, spec["model"], spec.get("func", "sum"), spec["field"],
            filters=spec.get("filters"))
        if not res["ok"]:
            raise PermissionError(
                f"scalar '{name}' ({spec['model']}): "
                f"{res['error']['message']}")
        val = res["data"]
        if isinstance(val, list):
            raise FormulaError(
                f"scalar '{name}' must not be grouped (single value only).")
        values[name] = float(val) if val is not None else None
        trail.append({"name": name, **spec, "value": values[name]})
    return values, trail


# ---------------------------------------------------------------------------
# Compile to ORM expression
# ---------------------------------------------------------------------------

def _compile(tree: ast.AST, lookups: Dict[str, str],
             scalars: Dict[str, float]):
    flt = FloatField()

    def build(node):
        if isinstance(node, ast.Expression):
            return build(node.body)
        if isinstance(node, ast.Constant):
            return Value(float(node.value), output_field=flt)
        if isinstance(node, ast.Name):
            if node.id in lookups:
                return Cast(F(lookups[node.id]), output_field=flt)
            if node.id in scalars:
                return Value(scalars[node.id], output_field=flt)
            raise FormulaError(f"Unknown operand '{node.id}'.")
        if isinstance(node, ast.UnaryOp):
            inner = build(node.operand)
            if isinstance(node.op, ast.USub):
                return ExpressionWrapper(Value(-1.0, output_field=flt) * inner,
                                         output_field=flt)
            return inner
        if isinstance(node, ast.BinOp):
            left, right = build(node.left), build(node.right)
            if isinstance(node.op, ast.Add):
                expr = left + right
            elif isinstance(node.op, ast.Sub):
                expr = left - right
            elif isinstance(node.op, ast.Mult):
                expr = left * right
            else:  # Div: NULL on zero denominator instead of an error.
                # Renders as standard NULLIF(x, y), which ClickHouse accepts
                # (case-insensitive alias of nullIf) -- verified by the
                # ClickHouseCompatTests SQL assertion. Any residual inf/nan
                # from float math is scrubbed to null in _json_safe.
                expr = left / NullIf(right, Value(0.0, output_field=flt),
                                     output_field=flt)
            return ExpressionWrapper(expr, output_field=flt)
        raise FormulaError("Unsupported node.")

    return build(tree)


# ---------------------------------------------------------------------------
# The guarded tool
# ---------------------------------------------------------------------------

def guarded_compute_data(user,
                         model_path: str,
                         expression: str,
                         filters: Optional[Dict[str, Any]] = None,
                         aggregate: Optional[str] = None,
                         group_by: Optional[str] = None,
                         scalars: Optional[Dict[str, Dict[str, Any]]] = None,
                         limit: int = legacy.DEFAULT_LIMIT,
                         offset: int = 0,
                         order_by: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate an arithmetic formula over rows of ``model_path`` in the DB.

    Args:
        expression: e.g. ``"A / B * A"`` -- operands are allowed fields,
            ``Relation__field`` paths, or names defined in ``scalars``.
        filters:    Django-style lookups applied first (legacy-validated).
        aggregate:  None -> per-row values; or sum/avg/min/max/count of the
            computed value (optionally ``group_by`` an allowed field).
        scalars:    {name: {"model", "func", "field", "filters"?}} constants
            from other tables (each an audited aggregate call).
    Returns the standard envelope. Rows: ``[{pk, <operands…>, result}]``.
    """
    query: Dict[str, Any] = {"expression": expression, "filters": filters or {},
                             "aggregate": aggregate, "group_by": group_by,
                             "scalars": scalars or {}, "limit": limit,
                             "offset": offset}
    try:
        decision = check_access(user, model_path, action="aggregate")
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e),
                    "Use guarded_list_models() to see valid paths.")
    if not decision.allowed:
        record_audit(user, "compute", decision, query=query)
        return _err(PERMISSION_DENIED, decision.reason,
                    "Ask an administrator to map your group to this table.")

    app_label, model_name = decision.model_path.split(".")
    try:
        tree, names = parse_expression(expression)
        scalar_values, scalar_trail = _resolve_scalars(user, scalars)
        lookups: Dict[str, str] = {}
        for name in names:
            if name in scalar_values:
                continue
            lookups[name] = _resolve_field_path(
                user, app_label, model_name, decision.fields, name)
        if not lookups and aggregate is None:
            raise FormulaError(
                "Expression uses no table fields; add a field operand or "
                "set aggregate to compute a single value.")
        query["fields"] = sorted(lookups.values())
        query["scalar_values"] = scalar_trail

        clean_filters = legacy.validate_filters(app_label, model_name,
                                                filters or {})
        hidden = [k for k in clean_filters
                  if k.split("__")[0] not in decision.fields]
        if hidden:
            raise PermissionError(
                f"Filter on hidden field '{hidden[0]}' is not permitted.")
        if group_by:
            group_by = legacy.resolve_field(model_name, group_by)
            if group_by not in decision.fields:
                raise PermissionError(
                    f"group_by field '{group_by}' is not available to you.")

        model = django_apps.get_model(app_label, model_name)
        qs = model.objects.filter(**clean_filters)
        qs = legacy.apply_user_scope(qs, model, user)
        computed = _compile(tree, lookups, scalar_values)
        qs = qs.annotate(_result=computed)

        if aggregate:
            agg = (aggregate or "").lower()
            if agg not in AGG:
                raise FormulaError(
                    f"aggregate must be one of {sorted(AGG)}.")
            if group_by:
                limit = min(max(1, int(limit)), legacy.MAX_LIMIT)
                rows = list(qs.values(group_by)
                            .annotate(result=AGG[agg]("_result"))
                            .order_by(group_by)[:limit])
                data = legacy._json_safe([{group_by: r[group_by],
                                           "result": r["result"]}
                                          for r in rows])
            else:
                data = legacy._json_safe(
                    qs.aggregate(result=AGG[agg]("_result"))["result"])
            payload = {"expression": expression, "aggregate": agg,
                       "group_by": group_by, "result": data,
                       "scalars": scalar_trail}
            query.update(ok=True, result=data)
            record_audit(user, "compute", decision, query=query,
                         row_count=len(data) if isinstance(data, list)
                         else None)
            return _ok(payload)

        # per-row mode
        pk_name = model._meta.pk.name
        if order_by:
            fld = order_by.lstrip("-")
            if fld not in ("result", *decision.fields):
                raise PermissionError(
                    f"Cannot order by '{fld}'.")
            order_clause = ("-" if order_by.startswith("-") else "") + (
                "_result" if fld == "result" else fld)
        else:
            order_clause = f"-{pk_name}"
        limit = min(max(1, int(limit)), legacy.MAX_LIMIT)
        offset = max(0, int(offset))
        qs = qs.order_by(order_clause)
        total = qs.count()
        value_fields = [pk_name] + sorted(set(lookups.values()) - {pk_name})
        rows = list(qs.values(*value_fields, "_result")[offset:offset + limit])
        for r in rows:
            r["result"] = r.pop("_result")
        data = {"expression": expression,
                "rows": legacy._json_safe(rows),
                "total_count": total,
                "has_more": (offset + limit) < total,
                "limit": limit, "offset": offset,
                "scalars": scalar_trail,
                "null_means": "division by zero or missing value"}
        query.update(ok=True, total_count=total)
        record_audit(user, "compute", decision, query=query,
                     row_count=len(rows))
        return _ok(data)

    except PermissionError as e:
        query.update(ok=False, error=PERMISSION_DENIED)
        # Field/related-table denial: log the verdict as DENIED, not the
        # table-level allow.
        record_audit(user, "compute",
                     dataclasses.replace(decision, allowed=False,
                                         reason=str(e)[:255]),
                     query=query)
        return _err(PERMISSION_DENIED, str(e),
                    "Only fields visible to your groups may be used in "
                    "formulas (on every table the formula touches).")
    except (FormulaError, ValueError, TypeError, FieldError) as e:
        query.update(ok=False, error="VALIDATION_ERROR")
        record_audit(user, "compute", decision, query=query)
        return _err("VALIDATION_ERROR", str(e),
                    "Use guarded_describe_model() for field names; only "
                    "numbers, fields, + - * / and parentheses are allowed.")
    except Exception:
        logger.exception("compute failed for %s", model_path)
        query.update(ok=False, error="INTERNAL_ERROR")
        record_audit(user, "compute", decision, query=query)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.")

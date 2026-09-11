"""Drop-in tool bridge for YOUR existing LLM router / RAG pipeline.

Mirrors the interface of the legacy ``llm/tool_def.py`` (``TOOL_SCHEMAS``,
``execute_tool(name, arguments, user)``, ``field_legend``) so any client --
Little Larry, your own app's icon, a notebook, another service -- can feed
the LLM through the same policy-checked, audited tools. Swap the import::

    # before
    from llm.tool_def import TOOL_SCHEMAS, execute_tool, field_legend
    # after
    from ai_agent_core.integrations import TOOL_SCHEMAS, execute_tool, field_legend

Every tool call is Group-policy-checked, field-narrowed and written to the
Table access audit log, regardless of which UI or router made the call.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from .compute import guarded_compute_data
from .utils import (guarded_aggregate_data, guarded_describe_model,
                    guarded_fetch_data, guarded_fetch_related,
                    guarded_list_models)

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_models",
        "description": "List every database table you may query and its "
                       "visible fields. Call this FIRST.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "describe_model",
        "description": "Field metadata for one table (types, relations, "
                       "help text, choices). Call before filtering.",
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"}}, "required": ["model_path"]}}},
    {"type": "function", "function": {
        "name": "fetch_data",
        "description": "Fetch rows using Django-style filters, e.g. "
                       "{'Status__iexact': 'Open'}. Paginated.",
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"},
            "filters": {"type": "object"}, "exclude": {"type": "object"},
            "limit": {"type": "integer"}, "offset": {"type": "integer"},
            "order_by": {"type": "string"},
            "include_m2m": {"type": "boolean"}},
            "required": ["model_path"]}}},
    {"type": "function", "function": {
        "name": "aggregate_data",
        "description": "count/sum/avg/min/max of ONE field, optionally "
                       "grouped.",
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"},
            "func": {"type": "string",
                     "enum": ["count", "sum", "avg", "min", "max"]},
            "field": {"type": "string"}, "filters": {"type": "object"},
            "group_by": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["model_path", "func", "field"]}}},
    {"type": "function", "function": {
        "name": "fetch_related",
        "description": "Full rows related to ONE object through a "
                       "ManyToMany field (membership questions).",
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"}, "pk": {},
            "relation": {"type": "string"},
            "limit": {"type": "integer"}, "offset": {"type": "integer"}},
            "required": ["model_path", "pk", "relation"]}}},
    {"type": "function", "function": {
        "name": "compute_data",
        "description": (
            "ALWAYS use this for any formula/arithmetic across fields "
            "(e.g. 'A / B * A'); never calculate in your head. Runs in the "
            "database over all matching rows; division by zero yields null. "
            "Operands: visible fields, 'Relation__field' paths for related "
            "tables, or names from 'scalars' (aggregates of OTHER tables). "
            "Set 'aggregate' (sum/avg/min/max/count) for a single value, "
            "optionally 'group_by'."),
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"},
            "expression": {"type": "string"},
            "filters": {"type": "object"},
            "aggregate": {"type": "string",
                          "enum": ["sum", "avg", "min", "max", "count"]},
            "group_by": {"type": "string"},
            "scalars": {"type": "object", "description":
                        "{name: {model, func, field, filters?}}"},
            "limit": {"type": "integer"}, "offset": {"type": "integer"},
            "order_by": {"type": "string"}},
            "required": ["model_path", "expression"]}}},
]

_DISPATCH: Dict[str, Callable[[Dict[str, Any], Any], Dict[str, Any]]] = {
    "list_models": lambda a, u: guarded_list_models(u),
    "describe_model": lambda a, u: guarded_describe_model(u, **a),
    "fetch_data": lambda a, u: guarded_fetch_data(u, **a),
    "aggregate_data": lambda a, u: guarded_aggregate_data(u, **a),
    "fetch_related": lambda a, u: guarded_fetch_related(u, **a),
    "compute_data": lambda a, u: guarded_compute_data(u, **a),
}

TOOL_NAMES = list(_DISPATCH)


def execute_tool(name: str, arguments, user=None) -> str:
    """Execute one tool call; ALWAYS returns a JSON string (never raises).

    ``arguments`` may be a JSON string (OpenAI-style) or a dict.
    """
    try:
        args = (json.loads(arguments or "{}") if isinstance(arguments, str)
                else dict(arguments or {}))
        fn = _DISPATCH.get(name)
        if not fn:
            return json.dumps({"ok": False, "data": None, "error": {
                "code": "UNKNOWN_TOOL",
                "message": f"No tool named {name!r}. Available: {TOOL_NAMES}",
                "hint": ""}})
        return json.dumps(fn(args, user), default=str)
    except TypeError as e:   # bad/missing arguments
        return json.dumps({"ok": False, "data": None, "error": {
            "code": "VALIDATION_ERROR", "message": str(e), "hint": ""}})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "data": None, "error": {
            "code": "TOOL_ERROR", "message": str(e), "hint": ""}})


def field_legend(model_path: str, user=None) -> str:
    """Compact field-meaning block (policy-narrowed for ``user``)."""
    try:
        res = guarded_describe_model(user, model_path)
        if not res.get("ok"):
            return ""
        d = res["data"]
        lines = [f"FIELD MEANINGS for {d['model']} (pk: {d['primary_key']}):"]
        for f in d["fields"]:
            bits = [f"- {f['name']} ({f['type']})"]
            if f.get("related_model"):
                bits.append(f"-> {f['related_model']}")
            if f.get("help_text"):
                bits.append(f": {f['help_text']}")
            if f.get("choices"):
                bits.append(f"[values: {', '.join(f['choices'][:12])}]")
            lines.append(" ".join(bits))
        return "\n".join(lines)
    except Exception:
        return ""


def rag_context(user, question: str, max_tables: int = 6) -> Dict[str, Any]:
    """Build a RAG context block for your own router: retrieved rows +
    sources trail + model info, all through the audited search path."""
    from .search import ai_model_info, run_search
    searched: List[Dict[str, Any]] = []
    results = run_search(user, question, searched=searched)
    return {"results": results[:max_tables],
            "sources": searched,
            "model": ai_model_info(),
            "tools": TOOL_NAMES}

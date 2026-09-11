"""Tool schemas + dispatcher bridging the LLM to ai_tools."""
import json
import ai_tools  

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "List every queryable database model and its allowed fields. Call this FIRST to discover what data exists.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_model",
            "description": "Get field metadata for one model: types, relations, primary key, "
                            "what each field MEANS (help text), and allowed choice values. "
                            "Call before filtering on fields you're unsure about.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_path": {"type": "string", "description": "e.g. 'FiledEngSite.User_data'"},
                },
                "required": ["model_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_data",
            "description": "Fetch rows from a model using Django-style filters, e.g. {'Last_Name__iexact': 'Duncan'}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_path": {"type": "string"},
                    "filters": {"type": "object"},
                    "exclude": {"type": "object"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                    "order_by": {"type": "string"},
                    "include_m2m": {"type": "boolean", "description": "Return M2M fields as lists of related PKs"},
                },
                "required": ["model_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_data",
            "description": "Count/sum/avg/min/max over a model, optionally grouped by a field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_path": {"type": "string"},
                    "func": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                    "field": {"type": "string"},
                    "filters": {"type": "object"},
                    "group_by": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["model_path", "func", "field"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_related",
            "description": (
                "Fetch full rows related to ONE object through a ManyToMany field. "
                "Use for membership questions, e.g. 'who is in program X': first "
                "fetch_data on FiledEngSite.Program_Name_Data (try Program_Abr__iexact, "
                "then Program_Name__icontains) to get the program id, then call this "
                "with relation='Members' to get full employee rows. Paginated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_path": {"type": "string", "description": "Parent model, e.g. 'FiledEngSite.Program_Name_Data'"},
                    "pk": {"description": "Primary key of the parent object"},
                    "relation": {"type": "string", "description": "M2M field name, e.g. 'Members'"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "required": ["model_path", "pk", "relation"],
            },
        },
    },
]   

_DISPATCH = {
    "list_models": lambda args, user: ai_tools.list_models(),
    "describe_model": lambda args, user: ai_tools.describe_model(args["model_path"]),
    "fetch_data": lambda args, user: ai_tools.fetch_data(user=user, **args),
    "aggregate_data": lambda args, user: ai_tools.aggregate_data(user=user, **args),
    "fetch_related": lambda args, user: ai_tools.fetch_related(user=user, **args),
}
def field_legend(model_path: str) -> str:
    """Compact field-meaning block from describe_model. '' on any failure."""
    try:
        res = ai_tools.describe_model(model_path)
        if not res.get("ok"):
            return ""
        d = res["data"]
        lines = [f"FIELD MEANINGS for {d['model']} (pk: {d['primary_key']}):"]
        for f in d["fields"]:
            bits = [f"- {f['name']} ({f['type']})"]
            if f["related_model"]:
                bits.append(f"-> {f['related_model']}")
            if f["help_text"]:
                bits.append(f": {f['help_text']}")
            if f.get("choices"):
                bits.append(f"[values: {', '.join(f['choices'][:12])}]")
            lines.append(" ".join(bits))
        return "\n".join(lines)
    except Exception:
        return ""
      
def execute_tool(name: str, arguments: str, user=None) -> str:
    """Execute one tool call; ALWAYS returns a JSON string (never raises)."""
    try:
        args = json.loads(arguments or "{}")
        fn = _DISPATCH.get(name)
        if not fn:
            return json.dumps({"ok": False, "error": {"code": "UNKNOWN_TOOL",
                "message": f"No tool named {name!r}. Available: {list(_DISPATCH)}"}})
        return json.dumps(fn(args, user))
    except Exception as e:
        return json.dumps({"ok": False, "error": {"code": "TOOL_ERROR", "message": str(e)}})
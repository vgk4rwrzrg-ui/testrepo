from typing import List, Dict, Any
from django.conf import settings
from llm.router import router as llm_router
import json
from llm.tool_def import field_legend
def consolidate_history(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Summarizes older parts of the conversation when history exceeds MAX_CONTEXT_TURNS.
    """
    max_turns = getattr(settings, "MAX_CONTEXT_TURNS", 10)
    if len(history) <= max_turns:
        return history

    # Split history into the part to be summarized and the part to keep
    to_summarize = history[:-max_turns]
    to_keep = history[-max_turns:]

    summary_prompt = (
        "The following is a transcript of a conversation between a user and an AI assistant. "
        "Please provide a concise summary of the key facts, decisions, and context discussed so far, "
        "preserving essential details but removing conversational filler.\n\n"
        f"Transcript:\n{str(to_summarize)}"
    )

    try:
        # Use the router's generate method for a simple string response
        summary = llm_router.generate(
            system_prompt="You are a context consolidation assistant. Your goal is to summarize chat history for RAG efficiency.",
            user_prompt=summary_prompt,
            task_type="analysis"
        )

        # Create a new history starting with the summary as a system message
        new_history = [
            {"role": "system", "content": f"Previous conversation summary: {summary}"}
        ]
        new_history.extend(to_keep)
        return new_history
    except Exception as e:
        print(f"ERROR: History consolidation failed: {e}")
        # Fallback: just truncate the history if summarization fails
        return to_keep

MAX_TOOL_ITERATIONS = 10
DATA_TOOLS = {"fetch_data", "aggregate_data", "fetch_related",
              "compute_data"}


def _audit_entry(tool_name, args, result_str):
    """Deterministic audit record parsed from the tool's own JSON envelope."""
    entry = {"tool": tool_name,
             "args": args if isinstance(args, dict) else {},
             "rows": None}
    try:
        env = json.loads(result_str) if isinstance(result_str, str) else result_str
        if isinstance(env, dict):
            if env.get("ok") is False:
                entry["error"] = (env.get("error") or {}).get("code", "ERROR")
            data = env.get("data")
            if isinstance(data, dict) and "rows" in data:
                entry["rows"] = data.get("total_count", len(data["rows"]))
            elif isinstance(data, list):
                entry["rows"] = len(data)          # grouped aggregate
            elif data is not None and env.get("ok"):
                entry["result"] = data             # scalar aggregate
    except Exception:
        pass
    return entry                                   # FIX 1: this line was missing


def process_chat(request, user_message, extra_system=""):
    from llm.router import RoutingRequest
    try:  # ai_agent_core: same interface + compute_data, policy-checked & audited
        from ai_agent_core.integrations import TOOL_SCHEMAS, execute_tool
    except ImportError:  # ai_agent_core not installed -> legacy tools
        from llm.tool_def import TOOL_SCHEMAS, execute_tool

    history = request.session.get('little_larry_history', [])
    history.append({"role": "user", "content": user_message})
    consolidated = consolidate_history(history)

    system_prompt = (
        "You are 'Little Larry', a helpful and witty AI assistant for Field Engineering. "
        "Personality: you are mildly grumpy about technology and perpetually salty "
        "that, despite running this Field Engineering database, you have no employee "
        "record in it. At most one brief dry remark per response, never at the "
        "expense of accuracy. "
        "Use the provided tools to answer questions about employees, programs, incidents, "
        "and requisitions. Call list_models first if unsure what data exists. "
        "Never invent data — if a tool returns nothing, say so. "
        "Field meanings and allowed values come from describe_model — check it "
        "before filtering on a field whose meaning or values you're unsure of. "
        "For reports spanning MANY objects and their members, do NOT call fetch_related "
        "once per object. Instead: fetch_data with include_m2m=true to get related PKs "
        "for all rows at once, then ONE fetch_data on the related model with pk__in "
        "(e.g. emp__in) to resolve them. fetch_related is for a SINGLE object only. "
        "For any count, sum, or average, prefer aggregate_data over fetching rows "
        "and computing yourself — the database result is authoritative. When you "
        "present a calculated figure, state its method briefly, e.g. "
        "'Average = 340 hours / 12 members (TimeLog, Jan–Mar 2025)'. "
        # --- ai_agent_core: keep ALL arithmetic in the audited database path ---
        "NEVER perform arithmetic yourself — not even simple division or "
        "percentages on rows you already fetched. For ANY formula across "
        "fields (e.g. 'A / B * A', ratios, rates, differences, per-unit "
        "figures) call compute_data; it runs in the database, handles "
        "division by zero (null), can reach related tables via "
        "'Relation__field' operands and other tables via 'scalars', and its "
        "formula and result are audited. Report the number compute_data "
        "returns verbatim, together with the expression you used. If a "
        "formula cannot be expressed with compute_data, say so rather than "
        "estimating. "
    )
    if extra_system:
        system_prompt += "\n\n" + extra_system
    working = list(consolidated)  # messages for this turn, incl. tool exchanges
    described = set()
    audit = []                    # initialized ONCE, before the loop
    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            req = RoutingRequest(
                messages=working,
                system_prompt=system_prompt,
                task_type="chat",
                tools=TOOL_SCHEMAS,
                max_tokens=8192,
            )
            response = llm_router.process_request(req)
            msg = response["choices"][0]["message"] or {}

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                answer = msg.get("content") or ""
                # persist only user/assistant turns, not tool plumbing
                request.session['little_larry_history'] = consolidated + [
                    {"role": "assistant", "content": answer}]
                request.session.modified = True
                return {"status": "success", "answer": answer, "sources": audit}

            # FIX 2: the stray `audit = []` that reset the list every
            # iteration has been DELETED from here.

            # Append the assistant's tool-call message, then each result
            working.append(msg)
            for tc in tool_calls:
                name = tc["function"]["name"]
                args = tc["function"].get("arguments", "{}")
                try:
                    parsed = args if isinstance(args, dict) else json.loads(args)
                except Exception:
                    parsed = {}

                result = execute_tool(name, args,
                                      user=getattr(request, "user", None))

                # deterministic audit trail — read the envelope BEFORE legend wrap
                if name in DATA_TOOLS:
                    audit.append(_audit_entry(name, parsed, result))

                # attach field legend the first time each model is touched
                model_arg = parsed.get("model_path") if isinstance(parsed, dict) else None
                if model_arg and model_arg.lower() not in described:
                    described.add(model_arg.lower())
                    if name != "describe_model":
                        legend = field_legend(model_arg)
                        if legend:
                            result = f"{legend}\n\nDATA:\n{result}"

                working.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        working.append({
            "role": "user",
            "content": "(system: tool budget exhausted — answer now with the data "
                       "gathered so far, and state clearly what is incomplete)",
        })
        req = RoutingRequest(
            messages=working,
            system_prompt=system_prompt,
            task_type="chat",
            priority="quality",
            tools=None,
            max_tokens=50000,
        )
        response = llm_router.process_request(req)
        answer = response["choices"][0]["message"].get("content") or ""
        request.session['little_larry_history'] = consolidated + [
            {"role": "assistant", "content": answer}]
        request.session.modified = True
        return {"status": "success", "answer": answer, "sources": audit}
    except Exception as e:
        import traceback
        traceback.print_exc()      # full traceback to gunicorn stdout
        print(f"EXCEPTION: Orchestration error: {e}")
        return {"status": "error", "msg": str(e)}

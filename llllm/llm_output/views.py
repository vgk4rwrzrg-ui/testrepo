"""
Example endpoint showing where llm_output plugs into YOUR backend.

The RAG, the router/orchestrator and the AI_tool loop are yours and stay
unchanged -- llm_output only runs BEFORE (prepare) and AFTER (finalize).
Most likely you will not use this view at all: instead, call prepare() and
finalize() inside your existing chat view. This file is a working reference.
"""
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .pipeline import prepare, finalize


def run_orchestrator(user_message: str, extra_system: str) -> str:
    """
    REPLACE with a call into your existing router/orchestrator.

    Contract:
      - user_message: forward it exactly as you do today.
      - extra_system: APPEND this to the system prompt (or add it as a
        document in the RAG context) for this request. Do not put it in the
        user message -- it must survive the whole tool loop.
      - Run your normal loop: LLM -> router decides tools -> AI_tool fetches
        data -> results back to LLM -> repeat until the LLM produces its
        final message.
      - Return that FINAL message text (not tool traffic).

    Sketch:
        def run_orchestrator(user_message, extra_system):
            system = MY_SYSTEM_PROMPT + "\n\n" + extra_system
            messages = [{"role": "user", "content": user_message}]
            while True:
                resp = my_llm.chat(system=system, messages=messages,
                                   tools=MY_TOOLS)
                if resp.tool_calls:
                    for call in resp.tool_calls:
                        result = my_ai_tool.run(call.name, call.arguments)
                        messages.append(tool_result_message(call, result))
                    continue
                return resp.text
    """
    raise NotImplementedError("Wire this to your orchestrator.")


@csrf_exempt          # replace with proper CSRF/auth in production
@require_POST
def generate(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    user_message = (payload.get("prompt") or "").strip()
    if not user_message:
        return JsonResponse({"error": "Missing 'prompt'."}, status=400)

    # Phase 1: user picks the type in the UI (payload["output_type"]);
    # keyword detection is only the fallback.
    prep = prepare(user_message, payload.get("output_type"))

    # Your RAG + router + AI_tool loop, unchanged:
    raw = run_orchestrator(prep.user_message, prep.instructions)

    # Phase 2: build the file / chat bubble.
    result = finalize(raw, prep, payload.get("bubble_template"))
    return JsonResponse(result)

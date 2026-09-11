import os
import shutil
from django.shortcuts import render
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .Task import *
from celery.result import AsyncResult
from collections import defaultdict
from django.utils import timezone
from FiledEngSite.models import *
from Incident.models import Incindent_attachments
from django.db.models.functions import Concat
from django.db.models import CharField, Value, F
from django.core.serializers.json import DjangoJSONEncoder
from llm_output.pipeline import prepare, finalize
import json
def little_larry_view(request):
    """
    Render the Little Larry chat UI component. 
    """
    # This view is typically used to render the page/component.
    # Since robot_wink.html is likely included, we can just return a success.
    return JsonResponse({'status': 'ready'}, status=200)

@csrf_exempt
@login_required
def little_larry_chat(request):
    """
    API endpoint for the Little Larry chat.
    Handles POST requests, manages session history, and invokes LLM orchestration.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'Only POST requests are allowed'}, status=405)

    try:
        body = json.loads(request.body)
        user_message = body.get('message', '').strip()

        if not user_message:
            return JsonResponse({'status': 'error', 'msg': 'No message provided'}, status=400)

        # ── PHASE 1: detect/accept output type, build format instructions ──
        prep = prepare(user_message, body.get('output_type'))

        from llm.orchestrator import process_chat
        result = process_chat(request, user_message,
                              extra_system=prep.instructions)

        # ── PHASE 2: this is the block you asked about ──────────────────────
        if result['status'] == 'success':
            payload = finalize(result['answer'], prep, body.get('bubble_template'))

            # Don't leave raw block-JSON in history for file outputs
            if payload['type'] != 'chat' and 'filename' in payload:
                hist = request.session.get('little_larry_history', [])
                if hist and hist[-1]['role'] == 'assistant':
                    hist[-1]['content'] = (
                        f"[Generated a {payload['type']} file for the user: "
                        f"{payload['filename']}]"
                    )
                    request.session['little_larry_history'] = hist
                    request.session.modified = True

            response = {
                'status': 'success',
                'answer': payload['html'],      # ready-to-inject chat bubble
                'type': payload['type'],        # "chat" | "word" | "pdf" | "excel"
            }
            if 'download_url' in payload:
                response['download_url'] = payload['download_url']
                response['filename'] = payload['filename']
            if 'warning' in payload:
                response['warning'] = payload['warning']
            return JsonResponse(response)
        # ────────────────────────────────────────────────────────────────────
        else:
            return JsonResponse({'status': 'error', 'msg': result['msg']}, status=500)

    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'msg': 'Invalid JSON payload'}, status=400)
    except Exception as e:
        print(f"EXCEPTION: Chat view error: {e}")
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)

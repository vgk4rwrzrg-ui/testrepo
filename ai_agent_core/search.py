"""The bot's answer engine: semantic-lite search + optional LLM hook.

Out of the box the bot answers with keyword/`icontains` retrieval across
every table+field the user is allowed to see (all reads go through the
``guarded_*`` wrappers, so Group policies, field narrowing and auditing all
apply). To plug in a real LLM, point ``AI_AGENT_LLM_HANDLER`` at a callable:

    # settings.py
    AI_AGENT_LLM_HANDLER = "myproject.ai.answer"

    def answer(user, message, results, bot, history):
        \"\"\"Return the reply string (results = retrieved context rows).\"\"\"
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from django.apps import apps as django_apps
from django.conf import settings
from django.db import models as dj_models
from django.utils.module_loading import import_string

from . import registry
from .utils import guarded_fetch_data, guarded_list_models

logger = logging.getLogger(__name__)

MAX_TABLES_SCANNED = 12
MAX_QUERIES = 24
ROWS_PER_TABLE = 3
STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are",
             "was", "what", "who", "which", "show", "me", "find", "list",
             "all", "with", "and", "or", "how", "many", "does", "do"}


def _terms(message: str) -> List[str]:
    words = re.findall(r"[\w@.-]{2,}", message.lower())
    return [w for w in words if w not in STOPWORDS][:6]


def _text_fields(app_label: str, model_name: str,
                 allowed: List[str]) -> List[str]:
    try:
        model = django_apps.get_model(app_label, model_name)
    except LookupError:
        return []
    out = []
    for name in allowed:
        try:
            f = model._meta.get_field(name)
        except Exception:
            continue
        if isinstance(f, (dj_models.CharField, dj_models.TextField,
                          dj_models.EmailField, dj_models.SlugField)):
            out.append(name)
    return out


def run_search(user, message: str) -> List[Dict[str, Any]]:
    """Keyword retrieval across every table the user may see."""
    registry.sync_registry()
    catalogue = guarded_list_models(user)["data"]
    terms = _terms(message)
    if not terms:
        return []

    results: List[Dict[str, Any]] = []
    queries = 0
    for path, allowed_fields in list(catalogue.items())[:MAX_TABLES_SCANNED]:
        app_label, model_name = path.split(".")
        seen_pks, table_rows = set(), []
        for field in _text_fields(app_label, model_name, allowed_fields):
            for term in terms:
                if queries >= MAX_QUERIES:
                    break
                queries += 1
                r = guarded_fetch_data(
                    user, path,
                    filters={f"{field}__icontains": term},
                    limit=ROWS_PER_TABLE)
                if r["ok"]:
                    for row in r["data"]["rows"]:
                        key = tuple(sorted(row.items()))
                        if key not in seen_pks:
                            seen_pks.add(key)
                            table_rows.append(row)
            if len(table_rows) >= ROWS_PER_TABLE:
                break
        if table_rows:
            results.append({"table": path,
                            "rows": table_rows[:ROWS_PER_TABLE]})
    return results


def _default_reply(bot_name: str, message: str,
                   results: List[Dict[str, Any]]) -> str:
    if not results:
        return ("I could not find anything matching that in the tables you "
                "have access to. Try different keywords, or ask an "
                "administrator to open more tables to your group.")
    total = sum(len(r["rows"]) for r in results)
    tables = ", ".join(r["table"] for r in results)
    return (f"I found {total} matching record{'s' if total != 1 else ''} "
            f"across: {tables}. Here is what I can show you:")


def answer(user, message: str, bot,
           history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Full pipeline: retrieve -> (optional LLM) -> reply + result cards."""
    results = run_search(user, message)

    handler_path = getattr(settings, "AI_AGENT_LLM_HANDLER", None)
    if handler_path:
        try:
            handler = import_string(handler_path)
            reply = handler(user=user, message=message, results=results,
                            bot=bot, history=history)
            return {"reply": reply, "results": results}
        except Exception:
            logger.exception("AI_AGENT_LLM_HANDLER failed; falling back.")

    name = getattr(bot, "name", "Assistant")
    return {"reply": _default_reply(name, message, results),
            "results": results}

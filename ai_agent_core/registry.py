"""Runtime merge of admin-registered tables into the engine allowlist.

The legacy allowlist in ``legacy/ai_tools.py`` stays byte-identical; this
module *extends* the live ``ALLOWED_MODELS`` dict with rows from
``SearchableTable``/``SearchableField`` so administrators can register any
project model from the Django admin -- and *narrows* per-user field
visibility for fields restricted to specific Groups.

Sync is lazy (TTL) + signal-invalidated, so admin edits go live immediately
in-process and within ``REGISTRY_TTL`` seconds in other workers.
"""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Dict, List, Set, Tuple

from .legacy import ai_tools as legacy

REGISTRY_TTL = 60  # seconds between forced re-reads in other workers

_LEGACY_BASE = deepcopy(legacy.ALLOWED_MODELS)   # pristine legacy allowlist
_LOCK = threading.Lock()
_last_sync = 0.0
_dirty = True

#: (app_label, model_name) -> {field_name: {group_id, ...}} for RESTRICTED fields
FIELD_GROUPS: Dict[Tuple[str, str], Dict[str, Set[int]]] = {}


def mark_dirty(*args, **kwargs) -> None:
    """Signal receiver: invalidate the cache on any registration change."""
    global _dirty
    _dirty = True


def is_legacy_table(app_label: str, model_name: str) -> bool:
    return model_name in _LEGACY_BASE.get(app_label, {})


def sync_registry(force: bool = False) -> None:
    """Merge enabled SearchableTable rows into the live allowlist."""
    global _last_sync, _dirty, FIELD_GROUPS
    now = time.time()
    if not force and not _dirty and (now - _last_sync) < REGISTRY_TTL:
        return
    with _LOCK:
        if not force and not _dirty and (time.time() - _last_sync) < REGISTRY_TTL:
            return
        from .models import SearchableTable  # local import: app-load safety

        merged = deepcopy(_LEGACY_BASE)
        field_groups: Dict[Tuple[str, str], Dict[str, Set[int]]] = {}

        qs = (SearchableTable.objects.filter(enabled=True)
              .prefetch_related("fields__groups"))
        for table in qs:
            print(
                "REGISTERED TABLE:",
                table.app_label,
                table.model_name,
                "FIELDS:",
                list(table.fields.values_list("field_name", flat=True)),
            )
        for table in qs:
            names: List[str] = []
            for f in table.fields.all():
                if f.field_name.lower() in legacy.DENIED_FIELDS:
                    continue  # defence in depth
                names.append(f.field_name)
                gids = {g.id for g in f.groups.all()}
                if gids:
                    field_groups.setdefault(
                        (table.app_label, table.model_name), {}
                    )[f.field_name] = gids
            if names:
                existing = merged.setdefault(table.app_label, {}).get(
                    table.model_name, [])
                merged[table.app_label][table.model_name] = list(
                    dict.fromkeys(existing + names))

        # Mutate the legacy dict IN PLACE so every module holding a
        # reference (validators, resolvers) sees the merged allowlist.
        legacy.ALLOWED_MODELS.clear()
        legacy.ALLOWED_MODELS.update(merged)
        FIELD_GROUPS = field_groups
        _last_sync = time.time()
        _dirty = False
        print(FIELD_GROUPS)


def visible_fields(user, app_label: str, model_name: str,
                   fields: List[str]) -> List[str]:
    """Drop fields whose Group restriction the user does not satisfy."""
    restricted = FIELD_GROUPS.get((app_label, model_name))
    if not restricted:
        return list(fields)
    if getattr(user, "is_superuser", False):
        return list(fields)
    if getattr(user, "is_authenticated", False):
        user_gids = set(user.groups.values_list("id", flat=True))
    else:
        user_gids = set()
    return [f for f in fields
            if f not in restricted or (restricted[f] & user_gids)]

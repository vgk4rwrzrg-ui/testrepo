"""Environment-aware agent configuration profiles.

Personality and engine settings are NEVER hardcoded.  Profiles are JSON or
YAML files that live in a directory resolved (in priority order) from:

1. ``settings.AI_AGENT_CONFIG_DIR``
2. the ``AI_AGENT_CONFIG_DIR`` environment variable
3. the fallback ``/etc/ai_agent/config``

In Kubernetes the directory is typically a mounted ConfigMap or a PVC, so
profiles can be rotated without rebuilding the image.  ConfigMap mounts are
updated atomically through the ``..data`` symlink; this loader is
mtime-aware, so an edited ConfigMap is picked up automatically without a
pod restart.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

try:  # optional dependency
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "/etc/ai_agent/config"
SUPPORTED_SUFFIXES = (".yaml", ".yml", ".json")

_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()

#: Minimal sane defaults, deep-merged UNDER whatever the profile provides.
BASE_PROFILE: Dict[str, Any] = {
    "agent": {
        "name": "assistant",
        "personality": {
            "tone": "professional",
            "system_prompt": "You are a helpful enterprise assistant.",
        },
    },
    "engine": {
        "provider": "openai_compat",
        "model": "",
        "temperature": 0.0,
        "max_tokens": None,
        "timeout_seconds": 60,
    },
    "tools": {
        "table_search": {"enabled": True, "default_limit": 50},
    },
}


def yaml_available() -> bool:
    return yaml is not None


def get_config_dir() -> Path:
    """Resolve the profile directory (settings > env > default)."""
    raw = (
        getattr(settings, "AI_AGENT_CONFIG_DIR", None)
        or os.environ.get("AI_AGENT_CONFIG_DIR")
        or DEFAULT_CONFIG_DIR
    )
    return Path(raw).expanduser()


def get_default_profile_name() -> str:
    return (
        getattr(settings, "AI_AGENT_PROFILE", None)
        or os.environ.get("AI_AGENT_PROFILE")
        or "default"
    )


def list_profiles() -> List[str]:
    """Names of every profile file found in the config dir."""
    cfg_dir = get_config_dir()
    if not cfg_dir.is_dir():
        return []
    return sorted({
        p.stem for p in cfg_dir.iterdir()
        if p.suffix.lower() in SUPPORTED_SUFFIXES and p.is_file()
    })


def _find_profile_file(name: str) -> Optional[Path]:
    cfg_dir = get_config_dir()
    for suffix in SUPPORTED_SUFFIXES:
        candidate = cfg_dir / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _parse(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        if yaml is None:
            raise ImproperlyConfigured(
                f"Profile {path} is YAML but PyYAML is not installed."
            )
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ImproperlyConfigured(
            f"Agent profile {path} must contain a mapping at top level, "
            f"got {type(data).__name__}."
        )
    return data


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_profile(name: Optional[str] = None,
                 use_cache: bool = True) -> Dict[str, Any]:
    """Load an agent profile, merged over :data:`BASE_PROFILE`.

    Raises ``ImproperlyConfigured`` when the profile cannot be found or
    parsed -- fail fast rather than run an agent with a phantom identity.
    """
    name = name or get_default_profile_name()
    path = _find_profile_file(name)
    if path is None:
        raise ImproperlyConfigured(
            f"Agent profile '{name}' not found in {get_config_dir()} "
            f"(looked for {name}{'/'.join(SUPPORTED_SUFFIXES)})."
        )

    stat = path.stat()
    cache_key = str(path)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if use_cache and cached and cached["mtime"] == stat.st_mtime_ns:
            return copy.deepcopy(cached["data"])

    data = _deep_merge(BASE_PROFILE, _parse(path))
    with _LOCK:
        _CACHE[cache_key] = {"mtime": stat.st_mtime_ns, "data": copy.deepcopy(data)}
    logger.info("Loaded agent profile '%s' from %s", name, path)
    return data


def get_setting(dotted_key: str, default: Any = None,
                profile: Optional[str] = None) -> Any:
    """Convenience accessor: ``get_setting("engine.model")``."""
    node: Any = load_profile(profile)
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()

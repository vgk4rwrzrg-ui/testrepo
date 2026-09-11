# ai_agent_core — Configuration Guide

There are **three layers** of configuration. Know which layer a knob lives in and
you will never hunt for it again:

| Layer | Lives in | Needs restart? | Controls |
|---|---|---|---|
| 1. Django settings / env vars | `settings.py` or pod environment | Yes (env: pod restart) | Security, proxy, DB routing, LLM hooks, audit switches |
| 2. Agent profile (YAML/JSON) | files in `AI_AGENT_CONFIG_DIR` | **No** — mtime-aware reload | Bot engine: provider, model, temperature, system prompt fallback |
| 3. Admin models | Django admin UI | No | Bot look & behaviour, searchable tables/fields, access policies, document themes |

Rule of thumb: *infrastructure* → layer 1, *engine/LLM* → layer 2, *anything a
non-developer should change* → layer 3.

---

## Layer 1 — Django settings & environment variables

All optional unless marked. Settings names work both as Django settings and
(where noted) environment variables.

### Core

| Setting | Default | What it does |
|---|---|---|
| `AI_AGENT_CONFIG_DIR` (setting **or** env) | `/etc/ai_agent/config` | Directory containing agent profile files. Resolution order: setting > env > default. |
| `AI_AGENT_PROFILE` (setting **or** env) | `default` | Which profile file to load (`default` -> `default.yaml` / `.yml` / `.json`). |
| `AI_AGENT_DEFAULT_TABLE_ACCESS` | `"deny"` | What happens when a table has **no** `TableAccessPolicy`: `"deny"` (recommended) or `"allow"`. |
| `AI_AGENT_SUPERUSER_BYPASS` | `True` | Superusers skip Group policy checks (audit rows are still written). |
| `AI_AGENT_AUDIT_ENABLED` | `True` | Master switch for `TableAccessAudit` rows. Leave on. |
| `AI_AGENT_LLM_HANDLER` | `None` | Dotted path to your chat handler, e.g. `"llllm.llm.orchestrator.answer"`. Unset = built-in guarded retrieval. |
| `AI_AGENT_REPORT_WRITER` | `None` | Dotted path to your report writer for outline/chapter generation. |

### Proxy / JupyterHub (read from **environment**, not settings)

| Env var | Default | What it does |
|---|---|---|
| `JUPYTERHUB_SERVICE_PREFIX` | `""` | Set by JupyterHub automatically; the middleware uses it to fix `SCRIPT_NAME` behind the double proxy. |
| `AI_AGENT_FORCED_PREFIX` (setting) | unset | Manual override of the URL prefix when not under JupyterHub. |
| `AI_AGENT_TRUST_PREFIX_HEADER` | off | Trust `X-Forwarded-Prefix` from the proxy. |
| `AI_AGENT_BEHIND_TLS_PROXY` | `"1"` | Set to `"0"` only for plain-HTTP local dev; controls the `SECURE_PROXY_SSL_HEADER` behaviour. |
| `AI_AGENT_TRUSTED_PROXY_COUNT` | — | How many proxy hops to strip when resolving the client IP for audit rows. |
| `AI_AGENT_CLIENT_IP` | — | Header override for the audited client IP. |
| `AI_AGENT_ALLOWED_HOSTS` | — | Comma-separated extra `ALLOWED_HOSTS` entries. |
| `AI_AGENT_CSRF_TRUSTED_ORIGINS` | — | Comma-separated `CSRF_TRUSTED_ORIGINS` entries (scheme included). |

`python manage.py check` runs the app's system checks and will warn you when
`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`, or `CSRF_TRUSTED_ORIGINS`
look wrong for a proxied deployment.

---

## Layer 2 — Agent profile files

A profile is one YAML (or JSON) file in `AI_AGENT_CONFIG_DIR`. It is
deep-merged **over** built-in defaults, so you only write the keys you change.
The loader is mtime-aware: edit the file (or update the ConfigMap) and the next
request picks it up — no restart.

Full schema with every supported key:

```yaml
# /etc/ai_agent/config/default.yaml
agent:
  name: larry                        # internal agent name
  personality:
    tone: professional               # free text, folded into prompts
    system_prompt: >                 # fallback system prompt when no
      You are Larry, an enterprise   # BotProfile overrides it
      data assistant. Answer only
      from data returned by the
      table-search tools. Never
      invent rows.

engine:
  provider: openai_compat            # label shown in provenance stamps
  model: gpt-4o-mini                 # model name -> audit trail + documents
  temperature: 0.1
  max_tokens: 2048                   # null = provider default
  timeout_seconds: 60

tools:
  table_search:
    enabled: true
    default_limit: 50                # rows per guarded fetch (hard cap 500)
```

Multiple environments = multiple files: `default.yaml`, `staging.yaml`,
`prod.yaml` — select with `AI_AGENT_PROFILE=prod`. Code access:
`from ai_agent_core.conf import get_setting; get_setting("engine.model")`.

### Kubernetes

Keep profiles in a ConfigMap and mount it (see `deploy/k8s/agent-config.yaml`):

```yaml
singleuser:
  storage:
    extraVolumes:
      - name: ai-agent-profiles
        configMap: { name: ai-agent-profiles }
    extraVolumeMounts:
      - name: ai-agent-profiles
        mountPath: /etc/ai_agent/config
        readOnly: true
  extraEnv:
    AI_AGENT_CONFIG_DIR: /etc/ai_agent/config
    AI_AGENT_PROFILE: default
```

`kubectl edit configmap ai-agent-profiles` -> saved -> kubelet syncs the mount
(typically under a minute) -> loader sees the new mtime -> live. No image
rebuild, no pod restart.

---

## Layer 3 — Admin models (quick map)

| Admin section | Configures |
|---|---|
| **Bot profiles** | name, greeting, personality, avatar, accent color, window mode (left/right/popup), theme mode (auto/light/dark) |
| **Searchable tables / fields** | which models & columns the bot may search; per-field Group visibility |
| **Table access policies** | which Groups may touch which tables at all |
| **Document themes** | colors, fonts, footer for Word/PDF/Excel/PowerPoint output |
| **Audit trail** | read-only browse of every guarded read/compute |

---

## ClickHouse via `django-clickhouse-backend`

Your ClickHouse table is an ordinary Django model on a second database alias.
Three pieces of config, all layer 1:

### 1. `DATABASES`

```python
DATABASES = {
    "default": {                       # Postgres/MySQL/SQLite — the app DB
        "ENGINE": "django.db.backends.postgresql",
        # ...
    },
    "clickhouse": {
        "ENGINE": "clickhouse_backend.backend",
        "NAME": "analytics",           # ClickHouse database name
        "HOST": "clickhouse.mycluster.svc",
        "PORT": 9000,                  # native protocol port
        "USER": "django",
        "PASSWORD": os.environ["CLICKHOUSE_PASSWORD"],
        "OPTIONS": {"settings": {"mutations_sync": 1}},
    },
}
```

### 2. Database router — **mandatory**

The engine never calls `.using()`; routing decides where a query goes. All
`ai_agent_core` tables (audit, policies, conversations, reports, documents)
MUST stay on `default` — they need transactions, updates and autoincrement,
which ClickHouse does not do well.

```python
# myproject/db_routers.py
class ClickHouseRouter:
    """Send ClickHouse-flagged models to the clickhouse alias, all else to default."""

    def _is_ch(self, model):
        return getattr(model, "_clickhouse", False) or model._meta.app_label == "analytics"

    def db_for_read(self, model, **hints):
        return "clickhouse" if self._is_ch(model) else None   # None = default

    def db_for_write(self, model, **hints):
        return "clickhouse" if self._is_ch(model) else None

    def allow_relation(self, obj1, obj2, **hints):
        # No cross-database FK joins.
        return self._is_ch(type(obj1)) == self._is_ch(type(obj2))

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "clickhouse":
            return app_label == "analytics"       # only CH models migrate there
        return app_label != "analytics"           # and nothing else does

DATABASE_ROUTERS = ["myproject.db_routers.ClickHouseRouter"]
```

### 3. The model — explicit primary key required

ClickHouse has no autoincrement; the engine orders by `-pk`, so declare one:

```python
# analytics/models.py
from clickhouse_backend import models as chm
from django.db import models

class SiteMetrics(chm.ClickhouseModel):
    _clickhouse = True                       # router flag

    event_id   = models.UUIDField(primary_key=True)
    site       = models.CharField(max_length=64)     # use models.CharField,
    region     = models.CharField(max_length=32)     # NOT chm.StringField, if you
    visits     = models.IntegerField()               # want free-text search to see it
    revenue    = models.FloatField()
    ts         = models.DateTimeField()
    # ... your remaining columns (25 total is well within limits)

    class Meta:
        managed = True                       # or False for a pre-existing table
        ordering = ["-ts"]

    class ClickhouseMeta:
        engine = chm.MergeTree(order_by=("ts", "event_id"))
```

### Then wire it into the bot (layer 3, no code)

1. Admin -> **Searchable tables** -> app `analytics`, model `SiteMetrics`.
2. Add the fields users may see; set Group restrictions per field as usual.
3. Admin -> **Table access policies** -> grant the Groups that may query it.

Everything then works as on Postgres: fetch, aggregate, `compute_data`
formulas (`revenue / visits * 100`), auditing, per-field visibility. With only
25 columns and unless you have billions of rows, the free-text search pass is
fine to leave enabled.

**Cross-table formulas and ClickHouse:** `Relation__field` join paths cannot
cross databases — between a ClickHouse table and a default-DB table use the
`scalars` mechanism (each scalar is its own audited single-DB aggregate, then a
constant in the formula).

### Former sharp edges — now fixed in-app (`ClickHouseCompatTests`)

1. **String-field detection — FIXED.** `search.py` now matches text columns by
   `get_internal_type()` (includes `StringField` / `FixedStringField`), so
   `chm.StringField` columns are visible to free-text search. Using
   `models.CharField` is no longer required (though still fine).
2. **Exotic column types — FIXED.** JSON serialization falls back to `str()`
   for unknown types (Array/Map/Tuple/IPv4/IPv6, memoryview, ...) instead of
   raising `INTERNAL_ERROR`.
3. **Division guard — VERIFIED + hardened.** `compute_data`'s divide-by-zero
   guard compiles to standard `NULLIF(x, y)` (asserted by a test), which
   ClickHouse accepts as a case-insensitive alias of `nullIf`. Additionally,
   any `inf`/`nan` that ClickHouse float math produces is scrubbed to `null`
   in every tool response, keeping output strict JSON.
4. **Your own tests** touching ClickHouse models still need
   `TransactionTestCase` (ClickHouse has no transactions) — that one is
   inherent to the backend, not fixable app-side:

   ```python
   from django.test import TransactionTestCase

   class MetricsTests(TransactionTestCase):
       databases = {"default", "clickhouse"}
   ```

---

## Local-dev minimal settings (no proxy, no K8s)

```python
INSTALLED_APPS += ["ai_agent_core"]
MIDDLEWARE = ["ai_agent_core.middleware.ProxyPrefixMiddleware"] + MIDDLEWARE
AI_AGENT_CONFIG_DIR = BASE_DIR / "config"          # holds default.yaml
AI_AGENT_DEFAULT_TABLE_ACCESS = "deny"
```

```bash
AI_AGENT_BEHIND_TLS_PROXY=0 python manage.py runserver
```

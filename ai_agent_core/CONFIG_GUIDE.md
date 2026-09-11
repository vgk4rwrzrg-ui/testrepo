# ai_agent_core — Configuration Guide

Configuration lives in a small number of **actual files** plus the admin UI.
This guide walks through **each file one at a time**: what it is, where it
lives, a complete working example, and line-by-line instructions.

## 0. The files at a glance

| # | File | Lives where | Restart needed? | Controls |
|---|------|-------------|-----------------|----------|
| 1 | `settings.py` | your Django project | yes | apps, middleware, databases, all `AI_AGENT_*` knobs |
| 2 | pod environment / `.env` | container spec or shell | pod restart | hosts, CSRF origins, TLS mode, profile selection |
| 3 | `<profile>.yaml` | directory named by `AI_AGENT_CONFIG_DIR` | **no** — hot reload | AI engine: provider, model, temperature, system prompt |
| 4 | `deploy/k8s/agent-config.yaml` | your cluster (ConfigMap) | no | the K8s wrapper that delivers file #3 into pods |
| 5 | `db_router.py` | your Django project | yes | which database each model uses (mandatory with ClickHouse) |
| 6 | Django admin | browser | no | bot look, searchable tables, access policies, document themes |

Copy-paste starting points are shipped in the repo:
`deploy/examples/settings_example.py`, `deploy/examples/db_router.py`,
`deploy/config/default.yaml`, `deploy/k8s/agent-config.yaml`.

---

## 1. `settings.py` — the Django project file

**What it is:** your project's normal settings module. You add five blocks.
**Full example:** [`deploy/examples/settings_example.py`](../deploy/examples/settings_example.py) — copy blocks from there.

### Block 1: register the app

```python
INSTALLED_APPS = [
    # ... django defaults ...
    "ai_agent_core",
]
```

### Block 2: the proxy middleware — must be FIRST

```python
MIDDLEWARE = [
    "ai_agent_core.middleware.JupyterHubProxyMiddleware",  # FIRST, always
    # ... everything else after it ...
]
```

It must run before any other middleware because it rewrites `SCRIPT_NAME`/
`PATH_INFO` (the JupyterHub `/user/<name>/` prefix) and resolves the real
client IP that audit rows record. Putting it later means redirects, `{% url %}`
and login URLs are built with the wrong prefix.

### Block 3: databases (skip if you only use one database)

```python
DATABASES = {
    "default": {  # relational DB — audit trail, policies, bots, reports
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myapp", "USER": "myapp", "PASSWORD": "...",
        "HOST": "postgres", "PORT": "5432",
    },
    "clickhouse": {  # analytics tables (django-clickhouse-backend)
        "ENGINE": "clickhouse_backend.backend",
        "NAME": "default", "USER": "default", "PASSWORD": "",
        "HOST": "clickhouse",
        "PORT": 9000,   # native protocol port — NOT the 8123 HTTP port
    },
}
DATABASE_ROUTERS = ["myproject.db_router.ClickHouseRouter"]  # section 5
```

### Block 4: the `AI_AGENT_*` knobs — complete reference

Every knob is optional; the value shown is the default.

```python
AI_AGENT_CONFIG_DIR = "/etc/ai_agent/config"
#   Directory containing agent profile files (section 3).
#   Resolution order: this setting > env var AI_AGENT_CONFIG_DIR > default.

AI_AGENT_PROFILE = "default"
#   Which profile to load: "default" -> default.yaml / .yml / .json.
#   Also settable as an env var; the setting wins.

AI_AGENT_DEFAULT_TABLE_ACCESS = "deny"
#   A table registered as searchable but WITHOUT a TableAccessPolicy row:
#   "deny" = invisible to the AI (recommended), "allow" = readable by all.

AI_AGENT_SUPERUSER_BYPASS = True
#   Superusers skip Group policy checks. Audit rows are still written.

AI_AGENT_AUDIT_ENABLED = True
#   Master switch for TableAccessAudit rows (every fetch/aggregate/compute,
#   allowed or denied, with query + result). Leave on.

AI_AGENT_LLM_HANDLER = None
#   Dotted path to your chat callable, e.g. "llllm.llm.orchestrator.answer".
#   Unset = built-in guarded keyword retrieval (no external LLM).

AI_AGENT_REPORT_WRITER = None
#   Dotted path to your outline/chapter writer for big reports,
#   e.g. "myproject.ai.report_writer". Unset = built-in writer.

AI_AGENT_TRUST_PREFIX_HEADER = True
#   Honour X-Forwarded-Prefix from jupyter-server-proxy when resolving the
#   URL prefix. Set False only if an untrusted proxy could inject it.

AI_AGENT_TRUSTED_PROXY_COUNT = 2
#   How many proxy hops are trusted when reading X-Forwarded-For for the
#   audited client IP. Ingress + JupyterHub CHP = 2. Add 1 per extra LB.

AI_AGENT_FORCED_PREFIX = None       # e.g. "/myapp"
#   Manual URL prefix when NOT running under JupyterHub. Normally unset:
#   the middleware reads JUPYTERHUB_SERVICE_PREFIX from the environment.
```

### Block 5: security hardening — the LAST lines of settings.py

```python
from ai_agent_core.security import apply_proxy_security
apply_proxy_security(globals())
```

One call sets (only where you haven't already): `SECURE_PROXY_SSL_HEADER`,
`USE_X_FORWARDED_HOST/PORT`, secure cookies, per-user cookie paths under the
JupyterHub prefix (so alice's and bob's sessions don't collide on the shared
host), and reads the env allowlists from section 2. It must be last so it can
see everything you set above it.

---

## 2. Environment variables — the pod / `.env` file

**What it is:** values that differ per cluster, so they live in the container
spec (K8s `extraEnv`, Docker `--env-file`) rather than in code.

**Complete example `.env`:**

```bash
# Where the profile ConfigMap is mounted (section 3/4):
AI_AGENT_CONFIG_DIR=/etc/ai_agent/config
# Which profile file to load (default.yaml here):
AI_AGENT_PROFILE=default

# Public hostnames, comma-separated -> ALLOWED_HOSTS:
AI_AGENT_ALLOWED_HOSTS=hub.example.com
# Public origins WITH scheme, comma-separated -> CSRF_TRUSTED_ORIGINS:
AI_AGENT_CSRF_TRUSTED_ORIGINS=https://hub.example.com

# Set to 0 ONLY for plain-HTTP local dev (disables secure-cookie flags):
AI_AGENT_BEHIND_TLS_PROXY=1

# Set automatically by JupyterHub/KubeSpawner — do NOT set by hand in K8s.
# For non-Hub deployments use the AI_AGENT_FORCED_PREFIX *setting* instead.
# JUPYTERHUB_SERVICE_PREFIX=/user/alice
```

Instructions:

1. The first two are read by the profile loader (settings win over env).
2. The host/origin/TLS vars only take effect through
   `apply_proxy_security(globals())` — Block 5 above must be present.
3. Changing any env var requires a pod restart (unlike section 3 files).

---

## 3. The agent profile — `default.yaml`

**What it is:** the AI engine's identity and tuning. One file per profile in
the directory named by `AI_AGENT_CONFIG_DIR`. `.yaml`, `.yml` and `.json` all
work. **Edits are picked up live** (the loader checks the file's mtime on
every read) — no restart, no pod bounce.

**Complete annotated example** (shipped at `deploy/config/default.yaml`):

```yaml
agent:
  name: larry                    # bot identity used in prompts & provenance
  personality:
    tone: professional           # free-form hint passed to your LLM handler
    system_prompt: >             # fallback system prompt; the BotProfile
      You are Larry, an          # admin record overrides this when set
      enterprise data assistant.
      Answer only from data returned
      by the table-search tools.

engine:
  provider: openai_compat        # label recorded in every document's
                                 #   "Generated by AI" provenance section
  model: gpt-4o-mini             # model name — also stamped on documents
  temperature: 0.1               # passed to your LLM handler
  max_tokens: 2048               # ditto (null = provider default)
  timeout_seconds: 60            # ditto

tools:
  table_search:
    enabled: true                # master switch for the search pipeline
    default_limit: 50            # rows per table when the caller sets none
```

Instructions:

1. **Every key is optional.** Your file is deep-merged **over** built-in
   defaults (`ai_agent_core.conf.BASE_PROFILE`), so a profile containing only
   `engine: {model: gpt-4o}` is valid.
2. **Adding a second profile:** drop `prod.yaml` in the same directory and
   set `AI_AGENT_PROFILE=prod`. Files: `<name>.yaml` ↔ profile `<name>`.
3. **JSON variant** (if PyYAML isn't installed you get warning W002 and only
   JSON loads):

   ```json
   {"agent": {"name": "larry"},
    "engine": {"provider": "openai_compat", "model": "gpt-4o-mini"}}
   ```

4. **Reading values in your own code:**

   ```python
   from ai_agent_core.conf import get_setting
   model = get_setting("engine.model")          # dotted-path accessor
   limit = get_setting("tools.table_search.default_limit", 50)
   ```

5. A missing or malformed profile raises `ImproperlyConfigured` at first use
   — deliberately fail-fast rather than running a bot with a phantom
   identity. `python manage.py check` warns (W001) if the directory itself
   is missing.

---

## 4. Kubernetes delivery — `deploy/k8s/agent-config.yaml`

**What it is:** the ConfigMap that carries section 3's file into every
JupyterHub user pod, plus the KubeSpawner snippet that mounts it.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ai-agent-profiles
  namespace: jupyterhub          # <- your Hub's namespace
data:
  default.yaml: |                # key name = file name in the mount
    agent:
      name: larry
      personality:
        tone: professional
        system_prompt: "You are Larry, an enterprise data assistant."
    engine:
      provider: openai_compat
      model: gpt-4o-mini
      temperature: 0.1
```

And in your JupyterHub Helm `values.yaml`:

```yaml
singleuser:
  storage:
    extraVolumes:
      - name: ai-agent-profiles
        configMap:
          name: ai-agent-profiles
    extraVolumeMounts:
      - name: ai-agent-profiles
        mountPath: /etc/ai_agent/config     # = AI_AGENT_CONFIG_DIR
        readOnly: true
  extraEnv:
    AI_AGENT_CONFIG_DIR: /etc/ai_agent/config
    AI_AGENT_PROFILE: default
    AI_AGENT_ALLOWED_HOSTS: "hub.example.com"
    AI_AGENT_CSRF_TRUSTED_ORIGINS: "https://hub.example.com"
```

Instructions:

1. Apply: `kubectl apply -f deploy/k8s/agent-config.yaml`, then
   `helm upgrade` with the values change (one-time; new pods get the mount).
2. **Live edits:** `kubectl edit configmap ai-agent-profiles -n jupyterhub`
   (or re-`apply`). Kubelet refreshes the mounted file within ~a minute via
   the atomic `..data` symlink, the mtime changes, and the loader picks it up
   on the next request. **Running pods do not restart and do not need to.**
3. To add profiles, add more keys under `data:` (`prod.yaml: |`, ...); switch
   pods between them with the `AI_AGENT_PROFILE` env var (that part *is* a
   restart, it's an env var).

---

## 5. The database router — `myproject/db_router.py`

**What it is:** a plain Python class Django consults for every query to pick
a database alias. **Mandatory when ClickHouse is in play** — the AI engine
never calls `.using()`, it relies entirely on this routing. It must also pin
all `ai_agent_core` tables to the relational DB (they need UPDATEs and
autoincrement PKs; ClickHouse supports neither properly).

**Complete example** (shipped at `deploy/examples/db_router.py` — copy it
into your project and edit one line):

```python
# Django app labels whose models live in ClickHouse.  EDIT THIS.
CLICKHOUSE_APPS = {"analytics"}


class ClickHouseRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label in CLICKHOUSE_APPS:
            return "clickhouse"
        return "default"

    def db_for_write(self, model, **hints):
        if model._meta.app_label in CLICKHOUSE_APPS:
            return "clickhouse"
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        # No FK/M2M across databases: Django cannot JOIN between them.
        db1 = "clickhouse" if obj1._meta.app_label in CLICKHOUSE_APPS else "default"
        db2 = "clickhouse" if obj2._meta.app_label in CLICKHOUSE_APPS else "default"
        return db1 == db2

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in CLICKHOUSE_APPS:
            return db == "clickhouse"
        return db == "default"
```

Line-by-line:

- `CLICKHOUSE_APPS` — the **only line you edit**: the Django app label(s)
  containing your ClickHouse models (the label is the app folder name unless
  overridden in `apps.py`).
- `db_for_read` / `db_for_write` — send those apps' queries to the
  `"clickhouse"` alias from `DATABASES`; everything else (including every
  `ai_agent_core` table) explicitly goes to `"default"`.
- `allow_relation` — blocks accidental FK/M2M between the two worlds. This is
  why the compute tool's `Relation__field` operands work only *within* one
  database; cross-database formulas use `scalars` (each scalar runs as its
  own audited aggregate on its own DB and enters the formula as a constant).
- `allow_migrate` — `manage.py migrate` creates ai_agent_core/auth tables
  only on `default` and your analytics tables only on `clickhouse`.

Wire it up (settings.py Block 3): 
`DATABASE_ROUTERS = ["myproject.db_router.ClickHouseRouter"]`

Then: `python manage.py migrate` (default DB) and
`python manage.py migrate analytics --database clickhouse`.

**ClickHouse model requirements** (25 columns is fine — no special tuning):
declare an explicit `primary_key=True` field (no autoincrement in
ClickHouse), use `models.CharField` *or* the backend's `StringField` (both
are detected for free-text search since commit `4341b6d`), and keep the
model in an app listed in `CLICKHOUSE_APPS`.

---

## 6. The admin layer (no files)

Everything a non-developer changes lives in Django admin:

| Admin section | What you configure |
|---|---|
| **Bot profiles** | name, greeting, avatar/launcher, theme colors, dark-mode behaviour, position |
| **Searchable tables / fields** | which models the AI may see, which columns are searchable/visible |
| **Table access policies** + **Group rules** | who may read what; default is deny (see `AI_AGENT_DEFAULT_TABLE_ACCESS`) |
| **Document themes** | colors, fonts, footer for Word/PDF/Excel/PowerPoint; mark one default |
| **Table access audits** / **Generated documents** / **Report jobs** | read-only inspection of everything the AI did |

Register your ClickHouse table here exactly like any other model — admin →
Searchable tables → add `analytics.MyTable`, tick the searchable string
columns, then grant a Group policy. No code.

---

## 7. Verifying a deployment

```bash
python manage.py check
```

| Warning | Meaning | Fix |
|---|---|---|
| W001 | profile directory missing | mount the ConfigMap / set `AI_AGENT_CONFIG_DIR` |
| W002 | PyYAML not installed | `pip install PyYAML` (or use `.json` profiles) |
| W003 | `SECURE_PROXY_SSL_HEADER` unset | add settings Block 5 |
| W004 | `USE_X_FORWARDED_HOST` off | add settings Block 5 |
| W005 | `CSRF_TRUSTED_ORIGINS` empty | set `AI_AGENT_CSRF_TRUSTED_ORIGINS` env |

Smoke test the stack end-to-end: `python manage.py test ai_agent_core`
(77 tests, including the ClickHouse-compat batch).

---

## 8. Minimal local development block

```python
# settings.py — local dev only
AI_AGENT_CONFIG_DIR = BASE_DIR / "deploy" / "config"   # use the repo example
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": BASE_DIR / "db.sqlite3"}}
```

```bash
AI_AGENT_BEHIND_TLS_PROXY=0 python manage.py runserver
```

No router, no ConfigMap, no env allowlists needed; the middleware finds no
prefix and serves from `/`.

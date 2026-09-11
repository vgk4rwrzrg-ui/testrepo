# `ai_agent_core` — Enterprise AI Agent Core for Django on JupyterHub/K8s

A clean, pluggable, reusable Django application that consolidates the legacy
standalone AI-agent modules (read-only table search, agent engine settings)
into one enterprise-ready cloud app. Designed to run inside a **JupyterHub
single-user container on Kubernetes**, behind a **double reverse proxy**
(Ingress → JupyterHub configurable-http-proxy → pod).

```
Client ──HTTPS──> Ingress (Nginx/Traefik) ──HTTP──> JupyterHub CHP ──> Pod (Django)
                  sets X-Forwarded-*                routes /user/<name>/…
```

---

## Contents

| Component | Purpose |
|---|---|
| `apps.py` | `AiAgentCoreConfig`; registers deployment system checks on `ready()` |
| `models.py` | `TableAccessPolicy` (Group → table grants) and `TableAccessAudit` (immutable audit trail) |
| `admin.py` | Admin panel to map table access policies to Django Groups on the fly |
| `utils.py` | `guarded_fetch_data / guarded_aggregate_data / guarded_list_models / guarded_describe_model` — the ONLY functions you expose to the agent |
| `access.py` | Group-based `AccessDecision` engine layered over the legacy allowlist |
| `middleware.py` | `JupyterHubProxyMiddleware` — prefix + `X-Forwarded-For` handling for the double proxy |
| `security.py` | `apply_proxy_security(globals())` — env-driven proxy security settings |
| `conf.py` | JSON/YAML agent-profile loader (K8s ConfigMap / PVC aware, mtime cache) |
| `checks.py` | `manage.py check` warnings for missing config dir / proxy settings |
| `legacy/ai_tools.py` | **Byte-for-byte copy of the source repo's `llllm/ai_tools.py`** — allowlist, denylist, alias resolution, pagination clamps and the uniform result envelope are preserved exactly |
| `views.py`, `urls.py` | `healthz/` probe and staff-only `proxy-debug/` endpoint |
| `tests.py` | Access-control + middleware regression suite |

---

## 1. Installation

```bash
pip install django PyYAML   # PyYAML optional; JSON profiles work without it
```

Copy (or submodule) the `ai_agent_core/` package into your project, then:

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "ai_agent_core",
]

MIDDLEWARE = [
    "ai_agent_core.middleware.JupyterHubProxyMiddleware",   # MUST be first
    "django.middleware.security.SecurityMiddleware",
    # ... the rest of your stack
]

# Where the mounted ConfigMap / PVC lives (env var AI_AGENT_CONFIG_DIR wins
# if the setting is absent):
AI_AGENT_CONFIG_DIR = "/etc/ai_agent/config"
AI_AGENT_PROFILE = "default"            # active profile name (or env var)

# One call wires SECURE_PROXY_SSL_HEADER, USE_X_FORWARDED_HOST,
# ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS from env, secure cookies and
# per-user cookie paths:
from ai_agent_core.security import apply_proxy_security
apply_proxy_security(globals())
```

```python
# urls.py
from django.urls import include, path
urlpatterns = [
    path("admin/", admin.site.urls),
    path("aiagent/", include("ai_agent_core.urls")),
]
```

```bash
python manage.py migrate ai_agent_core
python manage.py check          # surfaces ai_agent_core.W001–W005 if misconfigured
python manage.py test ai_agent_core
```

---

## 2. Double-proxy compatibility (JupyterHub)

### 2.1 What the middleware does

`JupyterHubProxyMiddleware` resolves the dynamic user prefix
(`/user/<username>/`) in priority order:

1. `settings.AI_AGENT_FORCED_PREFIX` (explicit override),
2. the `X-Forwarded-Prefix` header (sent by jupyter-server-proxy),
3. the `JUPYTERHUB_SERVICE_PREFIX` env var (injected by KubeSpawner),
4. inference from a `/user/<name>/…` or `/services/<name>/…` path.

It then moves the prefix from `PATH_INFO` into `SCRIPT_NAME` and calls
`set_script_prefix()`, so **`reverse()`, `{% url %}`, redirects, the admin
and `LOGIN_URL` all emit fully-prefixed absolute URLs** — no hardcoded paths
anywhere. It also resolves the true client IP from `X-Forwarded-For` using a
**trusted hop count** (`AI_AGENT_TRUSTED_PROXY_COUNT`, default `2` =
ingress + CHP), which defeats client-side XFF spoofing; read it with
`ai_agent_core.utils.get_client_ip(request)`.

### 2.2 Settings knobs

| Setting | Default | Meaning |
|---|---|---|
| `AI_AGENT_FORCED_PREFIX` | – | Hard override of the URL prefix |
| `AI_AGENT_TRUST_PREFIX_HEADER` | `True` | Honour `X-Forwarded-Prefix` (disable if the outer proxy does not sanitise it) |
| `AI_AGENT_TRUSTED_PROXY_COUNT` | `2` | Number of trusted reverse proxies for XFF resolution |

### 2.3 Proxy security (`security.py`)

`apply_proxy_security(globals())` sets, unless you already did:

* `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` — trust the
  TLS edge; `request.is_secure()` and secure cookies work correctly.
* `USE_X_FORWARDED_HOST / USE_X_FORWARDED_PORT = True` — absolute URLs use
  the public host, preventing CSRF/Origin mismatches.
* `ALLOWED_HOSTS` from env `AI_AGENT_ALLOWED_HOSTS` (comma-separated).
* `CSRF_TRUSTED_ORIGINS` from env `AI_AGENT_CSRF_TRUSTED_ORIGINS`
  (e.g. `https://hub.example.com`).
* `SESSION_COOKIE_SECURE / CSRF_COOKIE_SECURE = True`
  (set env `AI_AGENT_BEHIND_TLS_PROXY=0` for local dev).
* `SESSION_COOKIE_PATH / CSRF_COOKIE_PATH = $JUPYTERHUB_SERVICE_PREFIX/` —
  Alice's and Bob's pods on the same public host cannot clobber each other's
  cookies.

**Proxy-side requirements** (both hops):

```nginx
# outer Nginx ingress — OVERWRITE, never blindly append client-supplied values
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
proxy_set_header X-Forwarded-Prefix /user/$jh_user;   # if you strip prefixes
```

Traefik: enable `forwardedHeaders.trustedIPs` for the ingress IPs only.
JupyterHub CHP forwards these headers and appends its own XFF hop — which is
exactly why the trusted-hop count defaults to **2**.

### 2.4 Verifying a deployment

Log in as staff and open
`https://hub.example.com/user/<you>/aiagent/proxy-debug/` — it echoes the
resolved script prefix, scheme, host, forwarded headers, client IP, config
dir and available profiles. `aiagent/healthz/` is an unauthenticated K8s
liveness/readiness probe target.

---

## 3. Kubernetes-aware agent configuration

Personality and engine settings live in **JSON or YAML profiles** in
`AI_AGENT_CONFIG_DIR` — never in code. Loader behaviour (`conf.py`):

* Resolution order: `settings.AI_AGENT_CONFIG_DIR` → env
  `AI_AGENT_CONFIG_DIR` → `/etc/ai_agent/config`.
* `load_profile("default")` finds `default.yaml|yml|json`, validates it is a
  mapping, and deep-merges it over sane `BASE_PROFILE` defaults.
* **mtime-aware caching**: ConfigMap updates (atomic `..data` symlink swap)
  are picked up automatically — no pod restart, no redeploy.
* `get_setting("engine.model")` for dotted access; `list_profiles()` for
  discovery; missing/broken profiles raise `ImproperlyConfigured` (fail fast).

### Mounting strategies

**ConfigMap (recommended for profiles)** — see `deploy/k8s/agent-config.yaml`:

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

**PVC (for large/user-editable profile sets)** — mount the claim at the same
path; the loader does not care which storage backs the directory. Keep
`readOnly: true` unless admins edit profiles in-cluster.

Example profile: `deploy/config/default.yaml`.

---

## 4. Database-backed access control (Django Groups)

### 4.1 Model

`TableAccessPolicy` maps an **allowlisted** table to the Groups permitted to
search it:

* `access_level`: `read` | `aggregate_only` | `deny`
* `allowed_fields`: optional JSON list **narrowing** the legacy field
  allowlist for that audience (validated as a subset — policies can never
  widen the legacy surface)
* `max_rows_per_call`, `priority`, `is_active`,
  `applies_to_all_authenticated`

Evaluation: highest `priority` first; first policy whose groups intersect
the user's groups wins, so a high-priority `deny` overrides broader grants.
No match → site default `AI_AGENT_DEFAULT_TABLE_ACCESS` (`"deny"` unless you
set `"read"`). Superusers bypass (disable with
`AI_AGENT_SUPERUSER_BYPASS = False`). Every decision is written to
`TableAccessAudit` (disable with `AI_AGENT_AUDIT_ENABLED = False`).

### 4.2 Admin panel

*AI Agent Core → Table access policies*: allowlist-constrained dropdowns for
app/table, `filter_horizontal` group picker, bulk activate/deactivate
actions, and full validation via `clean()`. *Table access audits* is a
read-only browsing UI with date hierarchy and verdict filters.

### 4.3 Usage from the agent

```python
from ai_agent_core.utils import (
    guarded_list_models, guarded_describe_model,
    guarded_fetch_data, guarded_aggregate_data,
)

guarded_list_models(request.user)              # only tables the user may see
guarded_describe_model(request.user, "Req_tracker.Requisition")  # aliases work
guarded_fetch_data(request.user, "Req_tracker.Requistion",
                   filters={"Status": "Open"}, limit=25)
guarded_aggregate_data(request.user, "Incident.Incident_report",
                       "count", "Incident_number", group_by="Severity")
```

All four return the legacy envelope
`{"ok", "data", "error": {code, message, hint}}`; denials use
`code == "PERMISSION_DENIED"` with an actionable hint. Register **only** the
`guarded_*` functions in your LLM tool registry.

---

## 5. Data integrity preservation

`ai_agent_core/legacy/ai_tools.py` is a **verbatim copy** of the source
repository's `llllm/ai_tools.py`. Nothing was modified: the model/field
allowlist, `DENIED_FIELDS`/`DENIED_LOOKUPS` (blocking `password`, `token`,
`regex`, …, including via FK traversal), model/field alias maps
(`Requisition→Requistion`, `candidate_key→candiate_key`, …), `MAX_LIMIT`
pagination clamps, the `apply_user_scope` tenancy hook and the uniform
result envelope behave exactly as before. Group policies are enforced
**before** the legacy engine runs and can only *narrow* its output —
defence in depth, never a second source of truth.

---

## 6. Reference: settings & environment

| Name | Where | Default | Purpose |
|---|---|---|---|
| `AI_AGENT_CONFIG_DIR` | settings/env | `/etc/ai_agent/config` | Profile directory (ConfigMap/PVC mount) |
| `AI_AGENT_PROFILE` | settings/env | `default` | Active profile name |
| `AI_AGENT_DEFAULT_TABLE_ACCESS` | settings | `deny` | Fallback when no policy matches |
| `AI_AGENT_SUPERUSER_BYPASS` | settings | `True` | Superusers skip policy checks |
| `AI_AGENT_AUDIT_ENABLED` | settings | `True` | Write `TableAccessAudit` rows |
| `AI_AGENT_FORCED_PREFIX` | settings | – | Hard URL-prefix override |
| `AI_AGENT_TRUST_PREFIX_HEADER` | settings | `True` | Honour `X-Forwarded-Prefix` |
| `AI_AGENT_TRUSTED_PROXY_COUNT` | settings | `2` | Trusted hops for XFF |
| `AI_AGENT_ALLOWED_HOSTS` | env | – | Comma list → `ALLOWED_HOSTS` |
| `AI_AGENT_CSRF_TRUSTED_ORIGINS` | env | – | Comma list → `CSRF_TRUSTED_ORIGINS` |
| `AI_AGENT_BEHIND_TLS_PROXY` | env | `1` | `0` disables secure-cookie enforcement (dev) |
| `JUPYTERHUB_SERVICE_PREFIX` | env (KubeSpawner) | – | Per-user prefix source |

---

## 7. Running the tests

```bash
python manage.py test ai_agent_core
```

Covers: group grant + field narrowing, denial + audit, hidden-field filter
blocking, superuser bypass, aggregate-only mode, priority `deny` override,
policy validation against the legacy allowlist, prefix stripping,
prefix inference, and spoof-resistant client-IP resolution.

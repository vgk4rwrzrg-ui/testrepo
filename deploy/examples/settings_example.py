"""Complete example settings.py additions for ai_agent_core.

This is NOT a full Django settings file -- it shows every block you add to
your existing ``myproject/settings.py``, in the order it should appear.
Each block is explained in ai_agent_core/CONFIG_GUIDE.md section 1.
"""

# --- 1. Apps -----------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ai_agent_core",              # <- the app
    # "analytics",                # <- your ClickHouse models app (optional)
]

# --- 2. Middleware: proxy fixer must be FIRST --------------------------------
MIDDLEWARE = [
    "ai_agent_core.middleware.JupyterHubProxyMiddleware",   # FIRST, always
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --- 3. Databases (ClickHouse users only need the second entry) --------------
DATABASES = {
    "default": {                                   # relational: audit, policies,
        "ENGINE": "django.db.backends.postgresql", # bots, reports, documents
        "NAME": "myapp",
        "USER": "myapp",
        "PASSWORD": "change-me",
        "HOST": "postgres",
        "PORT": "5432",
    },
    "clickhouse": {                                # analytics tables
        "ENGINE": "clickhouse_backend.backend",
        "NAME": "default",
        "USER": "default",
        "PASSWORD": "",
        "HOST": "clickhouse",
        "PORT": 9000,                              # native protocol, not 8123
    },
}
DATABASE_ROUTERS = ["myproject.db_router.ClickHouseRouter"]  # see db_router.py

# --- 4. ai_agent_core knobs (all optional; defaults shown) -------------------
AI_AGENT_CONFIG_DIR = "/etc/ai_agent/config"  # where profile YAMLs live
AI_AGENT_PROFILE = "default"                  # which profile file to load
AI_AGENT_DEFAULT_TABLE_ACCESS = "deny"        # tables without a policy: deny
AI_AGENT_SUPERUSER_BYPASS = True              # superusers skip Group checks
AI_AGENT_AUDIT_ENABLED = True                 # master audit switch: leave on
AI_AGENT_LLM_HANDLER = None                   # "llllm.llm.orchestrator.answer"
AI_AGENT_REPORT_WRITER = None                 # "myproject.ai.report_writer"

# Proxy tuning (defaults are correct for ingress -> JupyterHub CHP -> pod):
AI_AGENT_TRUST_PREFIX_HEADER = True           # honour X-Forwarded-Prefix
AI_AGENT_TRUSTED_PROXY_COUNT = 2              # hops to strip for client IP
# AI_AGENT_FORCED_PREFIX = "/myapp"           # only when NOT under JupyterHub

# --- 5. Proxy security hardening: LAST line of settings.py -------------------
from ai_agent_core.security import apply_proxy_security
apply_proxy_security(globals())

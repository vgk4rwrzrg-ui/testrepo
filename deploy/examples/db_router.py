"""Example database router for ai_agent_core + django-clickhouse-backend.

Copy this file into your project (e.g. ``myproject/db_router.py``) and add:

    DATABASE_ROUTERS = ["myproject.db_router.ClickHouseRouter"]

WHY THIS FILE IS MANDATORY WITH CLICKHOUSE
------------------------------------------
The AI engine never calls ``.using(alias)`` -- it queries models through the
default manager and relies on Django's routing to reach the right database.
Without a router, every query goes to ``default`` and your ClickHouse models
simply error. The router must also PIN all ai_agent_core tables (audit rows,
policies, report jobs, generated documents) to the relational database:
they use UPDATEs and autoincrement PKs, which ClickHouse does not support.
"""

# Django app labels whose models live in ClickHouse.  EDIT THIS.
CLICKHOUSE_APPS = {"analytics"}

# Everything ai_agent_core owns must stay on the relational DB.
RELATIONAL_APPS = {
    "ai_agent_core",   # audit trail, policies, bots, reports, documents
    "admin", "auth", "contenttypes", "sessions", "messages",
}


class ClickHouseRouter:
    """Route ClickHouse-app models to the 'clickhouse' alias, pin the rest."""

    def db_for_read(self, model, **hints):
        if model._meta.app_label in CLICKHOUSE_APPS:
            return "clickhouse"
        return "default"          # explicit: audit/policy writes stay here

    def db_for_write(self, model, **hints):
        if model._meta.app_label in CLICKHOUSE_APPS:
            return "clickhouse"
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        """Forbid FK/M2M relations that would span the two databases.

        Django cannot JOIN across databases; the compute tool's
        ``Relation__field`` operands only work within ONE database.
        Cross-database math must use the ``scalars`` mechanism instead.
        """
        db1 = "clickhouse" if obj1._meta.app_label in CLICKHOUSE_APPS else "default"
        db2 = "clickhouse" if obj2._meta.app_label in CLICKHOUSE_APPS else "default"
        return db1 == db2

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """ClickHouse migrations only for ClickHouse apps, and vice versa."""
        if app_label in CLICKHOUSE_APPS:
            return db == "clickhouse"
        return db == "default"

from django.urls import path

from . import views

app_name = "ai_agent_core"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("proxy-debug/", views.proxy_debug, name="proxy_debug"),
    path("chat/", views.bot_chat, name="bot_chat"),
    path("widget-demo/", views.widget_demo, name="widget_demo"),
    path("guide/", views.user_guide, name="user_guide"),
    path("reports/<int:job_id>/step/", views.report_step, name="report_step"),
    path("reports/<int:job_id>/", views.report_status, name="report_status"),
    path("reports/<int:job_id>/download/", views.report_download,
         name="report_download"),
    path("documents/<int:doc_id>/download/", views.document_download,
         name="document_download"),
]

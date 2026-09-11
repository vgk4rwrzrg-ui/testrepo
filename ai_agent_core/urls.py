from django.urls import path

from . import views

app_name = "ai_agent_core"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("proxy-debug/", views.proxy_debug, name="proxy_debug"),
    path("chat/", views.bot_chat, name="bot_chat"),
    path("widget-demo/", views.widget_demo, name="widget_demo"),
]

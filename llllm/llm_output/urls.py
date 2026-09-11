from django.urls import path
from . import views

app_name = "llm_output"
urlpatterns = [
    path("output/generate/", views.generate, name="generate"),
]

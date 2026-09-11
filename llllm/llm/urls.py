from django.urls import path
from django.urls import include
urlpatterns = [
    path("output/", include("llm_output.urls")),
]  # No direct views; router is used from other apps.
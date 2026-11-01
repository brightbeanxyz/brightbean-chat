"""The platform API, mounted at ``/api/platform/v1/`` by ``config/urls.py``."""

from django.urls import path

from apps.api.platform_api import platform_api

urlpatterns = [path("", platform_api.urls)]

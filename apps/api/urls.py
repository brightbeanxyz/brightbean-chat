"""The public API, mounted at ``/api/v1/`` by ``config/urls.py``.

Two things live under this prefix: the Ninja instance (every operation, plus
``/openapi.json``) and the human reference page. ``docs`` is registered without
a trailing slash to match SPEC §17's literal path, the same way ``/healthz`` is.
"""

from django.urls import path

from apps.api import views_docs
from apps.api.api import api

urlpatterns = [
    path("docs", views_docs.api_docs, name="api_docs"),
    path("", api.urls),
]

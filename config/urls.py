from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from apps.common import views

urlpatterns = [
    path("admin/", admin.site.urls),
    # No trailing slash: SPEC §20 specifies /healthz, and probes are literal.
    path("healthz", views.healthz, name="healthz"),
    path("", views.index, name="index"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

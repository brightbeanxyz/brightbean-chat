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

# Serve uploads from disk in development. Gated on local storage as well as
# DEBUG: with STORAGE_BACKEND=s3 media lives off-origin, and mounting static()
# on an off-origin MEDIA_URL would add a route this project does not want.
if settings.DEBUG and settings.STORAGE_IS_LOCAL:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

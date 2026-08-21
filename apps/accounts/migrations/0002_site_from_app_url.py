"""Point Site 1 at this deployment.

``django.contrib.sites`` seeds ``example.com``, and allauth uses the current
Site for the name that appears in account emails. Left alone, a self-hoster's
password-reset mail introduces itself as example.com.

Idempotent and re-runnable: it reads ``APP_URL``, which every deployment already
has to set, so there is nothing extra to configure.
"""

from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import migrations

SITE_NAME = "BrightBean Chat"


def set_site(apps: Any, schema_editor: Any) -> None:
    Site = apps.get_model("sites", "Site")
    domain = urlparse(getattr(settings, "APP_URL", "") or "").netloc or "localhost:8000"
    Site.objects.update_or_create(
        pk=getattr(settings, "SITE_ID", 1),
        defaults={"domain": domain, "name": SITE_NAME},
    )


def unset_site(apps: Any, schema_editor: Any) -> None:
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(pk=getattr(settings, "SITE_ID", 1)).update(domain="example.com", name="example.com")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [migrations.RunPython(set_site, unset_site)]

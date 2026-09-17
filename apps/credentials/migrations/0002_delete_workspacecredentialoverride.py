"""Drop the workspace-level credential override table.

Platform app credentials are developer credentials: they are set with
``PLATFORM_<PLATFORM>_<KEY>`` environment variables, with the organization row
as a fallback for a deployment serving several organizations. The workspace
override and the settings page that wrote it are gone.

**This drops stored secrets.** Any workspace that had its own Meta app
credentials must have them supplied as environment variables (or as an
organization row in the admin) before its channels authenticate again.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="WorkspaceCredentialOverride"),
    ]

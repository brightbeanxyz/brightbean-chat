"""Stop storing invitation tokens; store a keyed digest instead.

The token is the whole credential — anyone holding it joins the organization at
whatever role the invitation names — so a column holding it meant a database
snapshot handed over every pending invitation (SECURITY-BASELINE §5). The digest
is a keyed HMAC: deterministic enough to carry a unique index, useless without
``SECRET_KEY``.

Existing rows are migrated rather than dropped, so a branch that has already run
``0001_initial`` keeps its outstanding invitations working.

**Forward only.** The reverse cannot restore the tokens — a one-way digest is
the point — and re-adding a unique column with a single default would fail
obscurely on the second row. It raises with an explanation instead: roll forward
and reissue.
"""

from typing import Any

from django.db import migrations, models


def digest_existing_tokens(apps: Any, schema_editor: Any) -> None:
    from apps.common.encryption import hmac_digest

    Invitation = apps.get_model("members", "Invitation")
    for invitation in Invitation.objects.all().iterator():
        invitation.token_digest = hmac_digest(invitation.token)
        invitation.save(update_fields=["token_digest"])


def cannot_restore_tokens(apps: Any, schema_editor: Any) -> None:
    raise RuntimeError(
        "Invitation tokens are stored as one-way digests and cannot be restored. "
        "Roll forward and resend the outstanding invitations instead."
    )


class Migration(migrations.Migration):
    dependencies = [("members", "0001_initial")]

    operations = [
        # Order matters: the token column has to still exist while the digests
        # are computed, and the unique constraint can only go on once every row
        # has one.
        migrations.AddField(
            model_name="invitation",
            name="token_digest",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(digest_existing_tokens, cannot_restore_tokens),
        migrations.RemoveField(model_name="invitation", name="token"),
        migrations.AlterField(
            model_name="invitation",
            name="token_digest",
            field=models.CharField(max_length=64, unique=True),
        ),
    ]

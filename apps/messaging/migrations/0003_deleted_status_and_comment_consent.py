"""Two new choices, no data change (issue #17, L5-A).

``MessageStatus.DELETED`` carries SPEC §6.3's "keep row with status deleted" for
Instagram's ``message_deletions`` webhook, and ``OptInSource.COMMENT`` records
that consent came from a public comment rather than from a message the person
sent us (SPEC §11.8's audit).

Both are ``choices`` edits, so PostgreSQL sees no DDL for the values themselves —
Django stores these as ``varchar`` and validates the choice in Python. The
migration exists because ``makemigrations --check`` runs in CI and a model whose
choices differ from the last migration fails it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0002_send_pipeline'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contactchannelidentity',
            name='opt_in_source',
            field=models.CharField(blank=True, choices=[('message_in', 'Inbound message'), ('comment', 'Public comment'), ('data_collection', 'Data collection'), ('import', 'Import'), ('api', 'API'), ('manual', 'Manual')], default='', max_length=32),
        ),
        migrations.AlterField(
            model_name='message',
            name='status',
            field=models.CharField(choices=[('queued', 'Queued'), ('sent', 'Sent'), ('delivered', 'Delivered'), ('read', 'Read'), ('failed', 'Failed'), ('deleted', 'Deleted')], default='queued', max_length=16),
        ),
    ]

"""A partial index for "is this person's next message a private reply?" (#17).

Asked by an adapter on the send path, which SPEC §7.1 budgets at 1.5 s of wall
clock including the outbound call — so it has to be an index probe rather than a
scan. Partial, over the unanswered rows only: every row this table keeps after
its reply went out answers no, so indexing them would grow the index for ever to
serve a question none of them can answer yes to.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('channels', '0002_flowpreviewlink'),
        ('contacts', '0002_contactimport'),
        ('flows', '0003_triggers_and_routing'),
        ('workspaces', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='handledcomment',
            index=models.Index(condition=models.Q(('private_reply_sent_at__isnull', True)), fields=['channel_connection', 'commenter_ref'], name='flows_hcomment_pending_idx'),
        ),
    ]

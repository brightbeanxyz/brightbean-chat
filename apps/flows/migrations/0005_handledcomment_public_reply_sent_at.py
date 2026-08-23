"""Record that a comment's public reply went out (#17).

``apps.queueing.registry``'s handler contract says a handler "must be safe to
run more than once" — zombie recovery re-runs one that committed without being
marked done. The private reply already had ``private_reply_sent_at`` to make
that true; the public reply had nothing, so a re-run posted a second visible
comment on the customer's post.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0004_handledcomment_flows_hcomment_pending_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='handledcomment',
            name='public_reply_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

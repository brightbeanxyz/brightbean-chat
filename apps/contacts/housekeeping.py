"""Dropping the uploaded files finished CSV imports no longer need (SPEC §19).

A ``ContactImport`` keeps two things: a report — counters plus the row errors —
and the spreadsheet those numbers came from. Only the first is worth keeping. An
operator asking "what happened to that import last month" wants the counts and
the failures; nobody re-reads the file, and it is a list of names, email
addresses and phone numbers sitting in object storage for as long as the row
does. Holding personal data with no remaining purpose is the thing SPEC §19 is
about, so the file goes and the report stays.

Not in ``apps/queueing/housekeeping.py``'s ``OPTIONAL_JOB_PATHS`` — that tuple
was written naming jobs Layer 2 already knew were coming, and this one is issue
#13's. The decorator is the documented way to add one from your own app's
``ready()``, and the lazy resolver skips a name already registered, so the two
mechanisms cannot double-run a job.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.contacts.models import FINISHED_IMPORT_STATUSES, ContactImport
from apps.queueing.housekeeping import register_housekeeping_job

__all__ = ["prune_import_files"]

logger = logging.getLogger(__name__)


@register_housekeeping_job("prune_contact_import_files")
def prune_import_files() -> str:
    """Drop the stored CSV **and the quoted row errors** of finished imports.

    Idempotent, as every housekeeping job must be: a run with nothing left to
    drop is excluded by the filter, so a repeat sweep is a query and nothing
    else.

    The delete and the column clear share a transaction per row, in that order.
    Storage deletion is not transactional, so the pairing cannot be atomic in
    both directions — and this is the direction that fails safely: a crash
    between them leaves a row pointing at a file that is gone, which the next
    sweep skips and which nothing reads, whereas clearing first would leave an
    orphaned file no sweep can ever find again.

    **``errors`` is cleared with the file (issue #95).** Each entry quotes the
    offending cell, so the list holds names, email addresses and phone numbers
    from the uploaded spreadsheet — the same personal data as the file it came
    from, and just as unreachable by a contact erasure, because nothing links a
    row of it back to the contact it created. Dropping the file while keeping
    its rejected rows would have retained the residue and deleted only the
    evidence of where it came from.

    ``error_count`` is deliberately **kept**. It is what an aged report actually
    needs — "412 rows failed" stays true and useful once the rows themselves are
    gone — and it is a number, not personal data.
    """
    cutoff = timezone.now() - timedelta(days=settings.CONTACT_IMPORT_FILE_RETENTION_DAYS)
    # unscoped(): housekeeping sweeps the whole deployment by definition, and an
    # import belongs to whichever workspace owns it (CONTRIBUTING.md asks for
    # the comment, not for the call to be avoided).
    stale = (
        ContactImport.objects.unscoped()
        .filter(status__in=sorted(FINISHED_IMPORT_STATUSES), finished_at__lt=cutoff)
        .exclude(file="", errors=[])
    )
    dropped = 0
    for run in stale.iterator():
        with transaction.atomic():
            if run.file:
                run.file.delete(save=False)
                run.file = ""
            run.errors = []
            run.save(update_fields=["file", "errors", "updated_at"])
        dropped += 1
    if dropped:
        logger.info("Pruned %s finished contact import(s) from before %s", dropped, cutoff)
    return f"pruned {dropped} import(s)"

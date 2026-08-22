"""The workspace quota holds under concurrency, or it is not a quota.

Read-then-write without a lock is the classic failure: two uploads both observe
the pre-upload total, both find room, and both commit. That is exactly the
concurrency an attacker generates, so the check is taken under
``select_for_update`` on the workspace row and this test is the proof.

``transaction=True`` because the threads need real, separately-committed
transactions — the usual wrapped-in-a-rollback fixture would hide the race by
never letting one thread see another's writes.
"""

import threading

import pytest

from apps.media_library.models import MediaAsset
from apps.media_library.quotas import QuotaExceededError, used_bytes
from apps.media_library.services import create_asset
from apps.media_library.tests import factories as f


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_uploads_cannot_both_spend_the_last_slot(settings, tmp_path):
    from django.db import connection

    from tests.support import create_tenancy

    settings.MEDIA_ROOT = str(tmp_path / "media")
    tenancy = create_tenancy("concurrent")
    workspace = tenancy.workspace

    # A PDF, not an image: the quota counts generated thumbnails too, so an
    # image's cost is its length plus a thumbnail whose size depends on JPEG
    # encoding. This test is about the row lock, and it should not also be a
    # test of how well Pillow compresses.
    payload = f.PDF
    # Room for exactly one of the two uploads below.
    settings.MEDIA_WORKSPACE_QUOTA_BYTES = len(payload) + 1

    start = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def upload() -> None:
        try:
            start.wait(timeout=10)
            create_asset(
                workspace=workspace,
                uploaded_file=f.upload(payload, name="race.pdf"),
                uploaded_by=None,
            )
            result = "accepted"
        except QuotaExceededError:
            result = "refused"
        finally:
            connection.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=upload) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["accepted", "refused"]
    assert MediaAsset.objects.for_workspace(workspace).count() == 1
    assert used_bytes(workspace) <= settings.MEDIA_WORKSPACE_QUOTA_BYTES

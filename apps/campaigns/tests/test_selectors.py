"""The counts the sequence pages put in front of a person.

Split from the view tests because the question is arithmetic, not rendering: a
count that disagrees with the list beside it is read as a row the filter is
hiding, and the reader goes looking for somebody who is not there.
"""

from typing import Any

import pytest

from apps.campaigns import selectors
from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.tests.support import sequence_with
from apps.contacts.models import Contact, ContactStatus

pytestmark = pytest.mark.django_db


def _enroll(workspace: Any, sequence: Any, contact: Any, status: str) -> SequenceEnrollment:
    enrollment = SequenceEnrollment(
        workspace=workspace,
        sequence=sequence,
        contact=contact,
        current_step=1,
        status=status,
    )
    enrollment.save()
    return enrollment


class TestSubscriberCount:
    """ "How many people are on this sequence in any state at all"."""

    def test_one_contact_walked_twice_is_one_person(self, tenancy: Any) -> None:
        """``enrollment_one_active_per_contact`` is unique only *while active*,
        so re-subscribing after a completed walk leaves two rows for one
        contact. Counting rows told the panel there were two subscribers."""
        sequence = sequence_with(tenancy.workspace)
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        _enroll(tenancy.workspace, sequence, contact, EnrollmentStatus.COMPLETED)
        _enroll(tenancy.workspace, sequence, contact, EnrollmentStatus.ACTIVE)

        assert selectors.subscriber_count(sequence) == 1

    def test_it_counts_every_state(self, tenancy: Any) -> None:
        """The point of this count rather than the filtered one: somebody whose
        only enrollment is finished is still a reason the panel is not empty."""
        sequence = sequence_with(tenancy.workspace)
        for index, status in enumerate(
            [EnrollmentStatus.ACTIVE, EnrollmentStatus.COMPLETED, EnrollmentStatus.UNSUBSCRIBED]
        ):
            contact = Contact.objects.create(workspace=tenancy.workspace, first_name=f"C{index}")
            _enroll(tenancy.workspace, sequence, contact, status)

        assert selectors.subscriber_count(sequence) == 3

    def test_a_deleted_contact_is_not_counted(self, tenancy: Any) -> None:
        sequence = sequence_with(tenancy.workspace)
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Gone", status=ContactStatus.DELETED)
        _enroll(tenancy.workspace, sequence, contact, EnrollmentStatus.ACTIVE)

        assert selectors.subscriber_count(sequence) == 0

    def test_another_sequence_does_not_leak_in(self, tenancy: Any) -> None:
        sequence = sequence_with(tenancy.workspace, name="Onboarding")
        other = sequence_with(tenancy.workspace, name="Winback")
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        _enroll(tenancy.workspace, other, contact, EnrollmentStatus.ACTIVE)

        assert selectors.subscriber_count(sequence) == 0

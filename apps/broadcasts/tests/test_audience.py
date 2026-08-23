"""Targeting and eligibility — SPEC §13.1's live preview, SPEC §13.2's filter.

What these assert is the property the whole feature rests on: **the preview and
the fanout answer the same question**. They go through
``compliance.annotate_eligibility``, which shares its rule list with
``can_send`` object-for-object, so a divergence here would mean a broadcast
whose count includes people the send then refuses.
"""

from datetime import timedelta

import pytest

from apps.broadcasts import audience
from apps.broadcasts.tests.conftest import EVERYONE
from apps.messaging.codes import Denial


@pytest.mark.django_db
class TestPreview:
    def test_it_counts_the_people_the_filter_matches(self, make_contacts, make_broadcast, connection):
        make_contacts(5, connection=connection)
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.total == 5
        assert preview.eligible == 5
        assert preview.skipped_total == 0

    def test_an_opted_out_contact_is_skipped_with_a_reason(self, make_contacts, make_broadcast, connection):
        make_contacts(3, connection=connection)
        make_contacts(2, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.total == 5
        assert preview.eligible == 3
        assert preview.skipped[Denial.OPTED_OUT.value] == 2

    def test_a_contact_with_no_identity_is_counted_not_dropped(self, make_contacts, make_broadcast, connection):
        """The join produces no row for them, so a naive count loses them entirely.

        SPEC §13.2 wants skip counts that reconcile against the audience, and a
        contact this workspace holds no address for is a skip with a reason, not
        an absence.
        """
        make_contacts(2, connection=connection)
        make_contacts(3, connection=connection, identity=False, prefix="ghost")
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.total == 5
        assert preview.eligible == 2
        assert preview.skipped[Denial.NO_IDENTITY.value] == 3

    def test_the_reasons_carry_examples(self, make_contacts, make_broadcast, connection):
        """A count tells an operator something is wrong; a name tells them what."""
        make_contacts(2, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.samples[Denial.OPTED_OUT.value]
        assert any("out0" in name for name in preview.samples[Denial.OPTED_OUT.value])

    def test_an_empty_audience_costs_no_eligibility_query(self, make_broadcast, connection):
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.total == 0
        assert preview.eligible == 0
        assert preview.skipped == {}

    def test_it_stays_set_wise_as_the_audience_grows(
        self, make_contacts, make_broadcast, connection, django_assert_max_num_queries
    ):
        """The composer recomputes this on every keystroke, so it must not scale.

        Three aggregates plus the sample query, whatever the audience size — the
        point of ``annotate_eligibility`` being a ``Case``/``When`` over the set
        rather than a loop over contacts.
        """
        make_contacts(40, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with django_assert_max_num_queries(6):
            audience.preview(broadcast)


@pytest.mark.django_db
class TestWindowedPlatforms:
    def test_outside_the_window_a_tagless_messenger_send_needs_a_tag(
        self, make_contacts, make_broadcast, messenger_connection
    ):
        make_contacts(4, connection=messenger_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=messenger_connection)

        preview = audience.preview(broadcast)

        assert preview.eligible == 0
        assert preview.needs(Denial.NEEDS_TAG.value) == 4

    def test_a_valid_tag_makes_the_same_audience_eligible(self, make_contacts, make_broadcast, messenger_connection):
        """The tag is read off the broadcast and put on the compliance probe.

        Which is the whole reason SPEC §6.4 has the composer force one: the same
        people go from refused to reachable because of a field on this row.
        """
        make_contacts(4, connection=messenger_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=messenger_connection, tag="ACCOUNT_UPDATE")

        preview = audience.preview(broadcast)

        assert preview.eligible == 4
        assert preview.needs(Denial.NEEDS_TAG.value) == 0

    def test_outside_the_window_whatsapp_needs_a_template(self, make_contacts, make_broadcast, whatsapp_connection):
        make_contacts(3, connection=whatsapp_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=whatsapp_connection)

        preview = audience.preview(broadcast)

        assert preview.eligible == 0
        assert preview.needs(Denial.NEEDS_TEMPLATE.value) == 3


@pytest.mark.django_db
class TestCandidates:
    def test_one_candidate_per_contact_even_with_a_leftover_pending_identity(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """A pending record and a real one for the same person is one send.

        ``annotate_eligibility`` admits both deliberately — a pending row is how
        an address captured before the connection existed is still counted — so
        the collapse has to happen here, or a contact would be messaged twice.
        """
        from django.utils import timezone

        from apps.messaging.models import ContactChannelIdentity, OptInSource

        [contact] = make_contacts(1, connection=connection)
        ContactChannelIdentity.objects.create(
            workspace=tenancy.workspace,
            contact=contact,
            channel_connection=None,
            platform=connection.platform,
            platform_user_id="pending-address",
            opt_in=True,
            # The consent audit is a check constraint, not a convention
            # (SPEC §11.8): opt_in without a recorded when and how is refused.
            opt_in_at=timezone.now(),
            opt_in_source=OptInSource.IMPORT,
        )
        broadcast = make_broadcast(connection=connection)

        candidates = list(audience.iter_candidates(broadcast))

        assert len(candidates) == 1
        assert candidates[0].is_eligible

    def test_the_cursor_walks_the_audience_without_repeating(self, make_contacts, make_broadcast, connection):
        """Fanout resumes from a contact id, which is what makes chunking safe."""
        make_contacts(7, connection=connection)
        broadcast = make_broadcast(connection=connection)

        first = list(audience.iter_candidates(broadcast, limit=3))
        second = list(audience.iter_candidates(broadcast, after=first[-1].contact_id, limit=3))
        third = list(audience.iter_candidates(broadcast, after=second[-1].contact_id, limit=3))

        seen = [candidate.contact_id for candidate in first + second + third]
        assert len(seen) == 7
        assert len(set(seen)) == 7

    def test_the_probe_carries_only_what_compliance_reads(self, make_broadcast, messenger_connection):
        """Blocks and buttons take no part in a compliance decision.

        Building the real message per contact just to ask whether it may be sent
        would be ten thousand renders for one boolean.
        """
        broadcast = make_broadcast(connection=messenger_connection, tag="ACCOUNT_UPDATE")

        probe = audience.probe_for(broadcast)

        assert probe.tag == "ACCOUNT_UPDATE"
        assert probe.blocks == ()
        assert probe.buttons == ()


@pytest.mark.django_db
class TestTenancy:
    def test_another_workspace_is_never_in_the_audience(
        self, tenancy, other_tenancy, make_contacts, make_broadcast, connection
    ):
        """Targeting goes through ``conditions.queryset``, which is scoped.

        Worth an explicit test rather than trusting the manager: the audience is
        the one query in this feature whose result becomes a list of people to
        message, and the guard fires at execution rather than at ``.filter()``.
        """
        from apps.contacts.models import Contact

        make_contacts(2, connection=connection)
        Contact.objects.create(workspace=other_tenancy.workspace, first_name="Outsider")
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert preview.total == 2

    def test_an_empty_all_rules_document_targets_the_whole_workspace(self, make_contacts, make_broadcast, connection):
        """The hazard ``apps/contacts/conditions.py`` names by issue number.

        "An empty ``rules`` list under ``match: all`` matches everyone […] a live
        hazard: an empty segment handed to a broadcast targets the workspace.
        Issue #23 must show a count before sending." This asserts the count is
        the truth, and ``test_composer`` asserts the composer shows it.
        """
        make_contacts(4, connection=connection)
        broadcast = make_broadcast(connection=connection, filter_json=EVERYONE)

        assert audience.preview(broadcast).total == 4

    def test_an_any_rules_document_targets_nobody(self, make_contacts, make_broadcast, connection):
        make_contacts(4, connection=connection)
        broadcast = make_broadcast(connection=connection, filter_json={"match": "any", "rules": []})

        assert audience.preview(broadcast).total == 0


@pytest.mark.django_db
class TestSamplesPerReason:
    def test_a_reason_that_sorts_late_still_gets_examples(
        self, tenancy, make_contacts, make_broadcast, messenger_connection
    ):
        """A single shared slice, drawn in contact order, starves the minority.

        Sixty opted-out contacts whose ids sort first and three needing a tag:
        with one slice across all reasons the tag bucket comes back empty — and
        that is exactly the reason an operator has to inspect, because it is the
        one blocking the send.
        """
        make_contacts(60, connection=messenger_connection, opted_out=True, prefix="aaa")
        make_contacts(3, connection=messenger_connection, window=-timedelta(hours=1), prefix="zzz")
        broadcast = make_broadcast(connection=messenger_connection)

        preview = audience.preview(broadcast)

        assert preview.skipped[Denial.OPTED_OUT.value] == 60
        assert preview.skipped[Denial.NEEDS_TAG.value] == 3
        assert preview.samples[Denial.NEEDS_TAG.value], "the blocking reason has no examples"
        assert all("zzz" in name for name in preview.samples[Denial.NEEDS_TAG.value])

    def test_each_bucket_is_capped(self, tenancy, make_contacts, make_broadcast, connection):
        make_contacts(20, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)

        assert len(preview.samples[Denial.OPTED_OUT.value]) == audience.PREVIEW_SAMPLE

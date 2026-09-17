"""The templates this repository ships, checked against the real importer.

Carries half of SPEC §21 phase 3's "flow export/import round-trips including
triggers" — the per-template half the issue asks for. The general claim lives in
``test_portability_roundtrip.py``; this module is that claim applied to each
shipped template, plus the rest of the acceptance criterion: a workspace missing
every requirement completes the mapping step, and publishing succeeds once the
connections are mapped.

``flow-templates/`` is the seed of the shared library and the directory a
community pull request adds to. A template that stops importing is therefore a
red build rather than a download that fails for a stranger, and the check is the
same call the upload path makes — not a weaker one against a different code
path.
"""

import os
import re
from typing import Any

import pytest

from apps.flows import portability
from apps.flows.engine.registry import types_without_runtime
from apps.flows.models import Flow, Trigger
from apps.flows.portability.library import (
    SLUG_PATTERN,
    TEMPLATE_COPY,
    library_path,
    read_template,
    template_card,
    template_cards,
    template_for_slug,
    template_paths,
)
from apps.flows.tests.portability_support import answer_channels
from apps.flows.triggers.types import PLATFORMS_FOR_TYPE
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db

#: Every template this repository ships, pinned by name so that deleting one is
#: a deliberate act with a test to update rather than a directory that quietly
#: empties, and so that adding one without running the suite is not possible.
#:
#: New templates are authored through scripts/make_flow_templates.py rather than
#: typed: the files are written by the real exporter, because their
#: `requirements` manifests are derived from the graph and a hand-written one
#: asks the importer for the wrong things.
EXPECTED = (
    "collect-an-email-address.json",
    "event-reminder.json",
    "feedback-after-a-purchase.json",
    "first-message-welcome.json",
    "follow-up-an-unanswered-enquiry.json",
    "hand-over-to-a-person.json",
    "instagram-comment-affiliate-picks.json",
    "instagram-comment-follow-to-unlock.json",
    "instagram-comment-link-in-dm.json",
    "instagram-comment-product-gallery.json",
    "instagram-comment-reel-to-product.json",
    "instagram-comment-rsvp.json",
    "instagram-comment-to-discount-code.json",
    "instagram-comment-to-dm-lead-magnet.json",
    "instagram-default-reply-autoresponder.json",
    "instagram-keyword-course-early-access.json",
    "instagram-keyword-dm-to-sms.json",
    "instagram-keyword-email-capture.json",
    "instagram-keyword-faq-hub.json",
    "instagram-keyword-link-drop.json",
    "instagram-keyword-qualify-quiz.json",
    "instagram-keyword-sms-list.json",
    "instagram-keyword-where-is-this-from.json",
    "instagram-keyword-youtube-subscribe.json",
    "instagram-link-in-bio-capture.json",
    "instagram-price-question.json",
    "instagram-story-collab-requests.json",
    "instagram-story-limited-time-offer.json",
    "instagram-story-mention-thank-you.json",
    "instagram-story-reply-to-conversation.json",
    "messenger-comment-to-dm.json",
    "messenger-quote-request.json",
    "messenger-welcome.json",
    "out-of-hours-reply.json",
    "sms-appointment-reminder.json",
    "sms-keyword-opt-in.json",
    "sms-review-request.json",
    "telegram-booking-enquiry.json",
    "telegram-support-triage.json",
    "telegram-welcome-and-faq.json",
    "waitlist-signup.json",
    "whatsapp-opening-hours.json",
    "whatsapp-order-status.json",
)


def _paths() -> list[Any]:
    paths = template_paths()
    assert paths, f"no templates found in {library_path()}"
    return paths


class TestTheShippedTemplates:
    def test_the_starter_templates_are_all_there(self) -> None:
        assert tuple(path.name for path in _paths()) == EXPECTED

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_validates(self, name: str) -> None:
        path = library_path() / name
        document, issues = read_template(path)
        assert document is None or not issues
        assert document is not None, [f"{issue.path or '-'}: {issue.message}" for issue in issues]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_imports_into_an_empty_workspace(self, name: str) -> None:
        """The acceptance criterion, per template: a workspace missing every
        requirement completes the mapping step with no dangling references."""
        document, issues = read_template(library_path() / name)
        assert not issues
        assert document is not None

        clean = create_tenancy(name.replace(".json", "")[:20])
        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)

        # The channel question has no default, deliberately: a blank one widens
        # the trigger to every platform its type supports, so nobody gets to
        # choose that by not looking. Everything *else* is answered by the
        # defaults, which is the acceptance criterion.
        #
        # `platform` at most once, not exactly once: a template started by `api`
        # or `default_reply` names no channel — those types are delivered by no
        # platform — so it has nothing left to answer at all.
        unanswered = [
            r.requirement.kind for r in portability.plan_import(clean.workspace, document, mapping).unanswered
        ]
        assert set(unanswered) <= {"platform"}, unanswered

        answer_channels(document, mapping)
        plan = portability.plan_import(clean.workspace, document, mapping)
        assert plan.can_apply, [
            f"{r.requirement.kind} {r.requirement.name or r.requirement.key}: {r.problem}" for r in plan.unanswered
        ]

        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)
        assert flows

        # No synthetic reference survived into a stored graph.
        landed = Flow.objects.for_workspace(clean.workspace).first()
        assert landed is not None
        stored = str([version.graph_json for version in landed.versions.all()])
        for requirement in portability.requirements_for(document):
            if requirement.ref:
                assert requirement.ref not in stored

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_round_trips(self, name: str) -> None:
        raw = (library_path() / name).read_text(encoding="utf-8")
        document, issues = read_template(library_path() / name)
        assert not issues
        assert document is not None

        clean = create_tenancy(f"rt-{name.replace('.json', '')}"[:20])
        # Like for like on the one thing no default can guess: the channel each
        # trigger watches. Without it a bound trigger lands unbound, which is a
        # legal import and a different document.
        mapping = answer_channels(
            document,
            portability.default_mapping(clean.workspace, document, user=clean.owner),
            connections=True,
            workspace=clean.workspace,
        )
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)
        assert portability.serialize(portability.export_document(flows[0])) == raw

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_publishes_once_its_channel_is_mapped(self, name: str) -> None:
        """The rest of the acceptance criterion: "publish succeeds after
        connections mapped".

        Publishing is the strict gate — every graph error blocks it — so a
        template that imports and then cannot be published would be a template
        that does not work. Capability warnings do not block (SPEC §9.1), which
        is why mapping the channel matters for the warnings and not for the
        outcome.
        """
        from apps.flows.services import publish

        document, _ = read_template(library_path() / name)
        assert document is not None
        clean = create_tenancy(f"p-{name[:16]}".replace(".", "-"))

        mapping = answer_channels(
            document,
            portability.default_mapping(clean.workspace, document, user=clean.owner),
            connections=True,
            workspace=clean.workspace,
        )
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)

        for flow in flows:
            result = publish(flow, user=clean.owner)
            assert result.validation.is_publishable
            assert result.version.published

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_arrives_as_a_draft_with_triggers_off(self, name: str) -> None:
        document, _ = read_template(library_path() / name)
        assert document is not None
        clean = create_tenancy(f"d-{name.replace('.json', '')}"[:20])
        mapping = answer_channels(document, portability.default_mapping(clean.workspace, document, user=clean.owner))
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)

        for flow in flows:
            assert flow.status == "draft"
            assert not flow.versions.filter(published=True).exists()
        triggers = Trigger.objects.for_workspace(clean.workspace)
        assert triggers.exists()
        assert not triggers.filter(enabled=True).exists()

    @pytest.mark.parametrize("name", EXPECTED)
    def test_the_manifest_matches_what_the_flows_reference(self, name: str, tenancy: Any) -> None:
        """No stale entries, and nothing missing.

        The importer re-derives requirements from the flows and reports any
        manifest entry nothing references. A shipped template producing that
        note would mean the exporter and the importer disagree about what counts
        as a reference — which is exactly what one shared walk exists to prevent.
        """
        document, _ = read_template(library_path() / name)
        assert document is not None
        plan = portability.plan_import(tenancy.workspace, document, {})
        assert not [note for note in plan.notes if "manifest" in note]

    def test_no_template_carries_a_workspace_id(self) -> None:
        """A shipped template is a shared file like any other, and is held to the
        same rule: no ids, no credentials, no signed URLs."""
        for path in _paths():
            raw = path.read_text(encoding="utf-8")
            assert "/m/" not in raw, f"{path.name} carries a media delivery URL"
            document, _ = read_template(path)
            assert document is not None
            for entries in document["requirements"].values():
                for entry in entries:
                    assert "used_by" in entry

    def test_every_link_points_at_the_documentation_domain(self) -> None:
        """``docs/flow-templates.md`` says "no live URLs you do not control", as
        prose. This is that sentence as a gate.

        ``example.com`` and its subdomains are reserved by RFC 2606 and can never
        become somebody's real site, so a placeholder left in by accident is inert
        rather than an endorsement of whoever registers the domain next — or, for
        an affiliate link, somebody else's tracking code shipped in our repository.
        """
        pattern = re.compile(r"https?://([^/\"\s]+)")
        for path in _paths():
            for host in set(pattern.findall(path.read_text(encoding="utf-8"))):
                assert host == "example.com" or host.endswith(".example.com"), (
                    f"{path.name} links to {host!r}; shipped templates use example.com placeholders"
                )

    @pytest.mark.parametrize("name", EXPECTED)
    def test_every_trigger_runs_on_the_platform_it_names(self, name: str) -> None:
        """The one cross-table invariant a hand-edited file can break silently.

        ``PLATFORMS_FOR_TYPE`` is the SPEC §10 channel column as data, and a
        template pairing a type with a platform that column forbids — a welcome
        trigger tagged ``instagram``, say — fails late and obscurely inside
        ``apply_import`` as "cannot be created here". Asserting it here names it.
        """
        document, _ = read_template(library_path() / name)
        assert document is not None
        for flow in document["flows"]:
            for trigger in flow["triggers"]:
                platform = trigger["platform"]
                if platform is None:
                    continue
                assert platform in PLATFORMS_FOR_TYPE[trigger["type"]], (
                    f"{name}: a {trigger['type']} trigger does not run on {platform}"
                )

    @pytest.mark.parametrize("name", EXPECTED)
    def test_no_template_ships_a_node_that_does_nothing(self, name: str) -> None:
        """A node type with a schema but no runtime is a step that silently does
        nothing at run time. Shipping one in a template would be a promise the
        engine does not keep."""
        document, _ = read_template(library_path() / name)
        assert document is not None
        inert = types_without_runtime()
        for flow in document["flows"]:
            for node in flow["graph"]["nodes"]:
                assert node["type"] not in inert, f"{name}: {node['id']!r} is a {node['type']}, which has no runtime"


class TestTheGalleryCards:
    """What the picker shows, and where it refuses to look."""

    def test_every_shipped_template_has_catalogue_copy(self) -> None:
        """Both directions: a template with no blurb and a blurb with no template
        are both mistakes, and only one of them is visible on the page."""
        assert set(TEMPLATE_COPY) == {path.stem for path in _paths()}

    def test_every_filename_is_addressable_as_a_slug(self) -> None:
        """A file whose stem the URL could never carry would show a card with a
        button that 404s. Catch it here instead."""
        for path in _paths():
            assert SLUG_PATTERN.match(path.stem), f"{path.name} is not addressable as a URL segment"

    @pytest.mark.parametrize("name", EXPECTED)
    def test_a_card_names_the_platforms_the_document_requires(self, name: str) -> None:
        document, _ = read_template(library_path() / name)
        assert document is not None
        card = template_card(library_path() / name)
        assert card is not None
        assert list(card.platforms) == [entry["key"] for entry in document["requirements"]["platform"]]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_a_card_takes_its_name_from_the_entry_flow(self, name: str) -> None:
        document, _ = read_template(library_path() / name)
        assert document is not None
        entry = next(flow for flow in document["flows"] if flow["key"] == document["entry"])
        card = template_card(library_path() / name)
        assert card is not None
        assert card.name == entry["name"]

    def test_a_card_lists_the_other_requirement_kinds_but_not_the_channel(self) -> None:
        card = template_card(library_path() / "sms-keyword-opt-in.json")
        assert card is not None
        assert card.needs == ("custom_field", "tag")
        assert "platform" not in card.needs

    def test_a_card_says_when_the_template_calls_out(self) -> None:
        """Installing a template only ever touches your own workspace — except
        for an external_request node, which sends what the flow gathered to a
        third party. Tested on the predicate rather than through a shipped file
        because no template ships one today; the badge is what makes the first
        one that does say so on the card.
        """
        from apps.flows.portability.library import _calls_out

        node = {"type": "external_request", "id": "n1"}
        assert _calls_out({"requirements": {}, "flows": [{"graph": {"nodes": [node]}}]}) is True

    def test_a_card_says_it_calls_out_when_only_a_header_survived_the_export(self) -> None:
        """The other half. An export strips header *values*, so a template can
        arrive with the requirement and, in a hand-written file, no node to show
        for it — and vice versa, which is why neither check stands alone."""
        from apps.flows.portability.library import _calls_out

        headers = {"request_header": [{"key": "r1"}]}
        assert _calls_out({"requirements": headers, "flows": [{"graph": {"nodes": []}}]}) is True

    def test_a_card_does_not_claim_an_ordinary_template_calls_out(self) -> None:
        from apps.flows.portability.library import _calls_out

        ordinary = {"type": "send_message", "id": "n1"}
        document = {
            "requirements": {"request_header": [], "tag": [{"key": "t1"}]},
            "flows": [{"graph": {"nodes": [ordinary]}}],
        }
        assert _calls_out(document) is False

    def test_no_shipped_template_calls_out_today(self) -> None:
        """Not a rule — a record. If this goes red, a template that makes an
        outbound request has been added: check the badge renders for it and
        that docs/flow-templates.md's warning about live URLs was heeded, then
        update this test rather than the card.
        """
        assert [card.slug for card in template_cards() if card.calls_out] == []

    def test_a_file_with_no_copy_still_gets_a_card(self, settings: Any, tmp_path: Any) -> None:
        """A self-hoster's drop-in is never invisible just because this repository
        has no sentence about it."""
        library = tmp_path / "flow-templates"
        library.mkdir()
        (library / "somebody-elses.json").write_bytes((library_path() / "telegram-welcome-and-faq.json").read_bytes())
        settings.BASE_DIR = tmp_path

        cards = template_cards()
        assert [card.slug for card in cards] == ["somebody-elses"]
        assert cards[0].summary == ""
        assert cards[0].name == "Telegram welcome and FAQ"

    def test_an_invalid_file_produces_no_card_and_does_not_raise(self, settings: Any, tmp_path: Any) -> None:
        """One bad drop-in costs that template its card, not the whole page."""
        library = tmp_path / "flow-templates"
        library.mkdir()
        (library / "broken.json").write_text("{ not json")
        settings.BASE_DIR = tmp_path

        assert template_cards() == []

    @pytest.mark.parametrize(
        "slug",
        [
            "",
            "..",
            "../conftest",
            "../../etc/passwd",
            "/etc/passwd",
            "telegram-welcome-and-faq.json",
            "TELEGRAM-WELCOME-AND-FAQ",
            "no-such-template",
        ],
    )
    def test_template_for_slug_refuses_anything_outside_the_library(self, slug: str) -> None:
        """The lookup never builds a path from its argument — it compares against
        stems it produced — so none of these have anything to act on."""
        assert template_for_slug(slug) is None

    def test_template_for_slug_finds_a_real_one(self) -> None:
        path = template_for_slug("telegram-welcome-and-faq")
        assert path is not None
        assert path.name == "telegram-welcome-and-faq.json"
        assert path.parent == library_path()

    def test_a_card_counts_runnable_steps_and_not_annotations(self) -> None:
        """A note is guidance on the canvas, not a step the flow runs.

        Every shipped template carries one, so counting graph nodes would
        overstate all of them — and by 100% on the one-message ones.
        """
        card = template_card(library_path() / "instagram-keyword-link-drop.json")
        assert card is not None
        document, _ = read_template(library_path() / "instagram-keyword-link-drop.json")
        assert document is not None
        nodes = document["flows"][0]["graph"]["nodes"]
        assert len([node for node in nodes if node["type"] == "note"]) == 1
        assert card.step_count == len(nodes) - 1 == 1

    def test_a_card_that_cannot_be_read_does_not_take_the_page_down(self, settings: Any, tmp_path: Any) -> None:
        """The guard covers I/O, not just validation.

        A file whose bytes cannot be read reaches a different failure than one
        whose contents are wrong, and only one of the two used to be handled.
        """
        library = tmp_path / "flow-templates"
        library.mkdir()
        unreadable = library / "unreadable.json"
        unreadable.write_bytes((library_path() / "telegram-welcome-and-faq.json").read_bytes())
        unreadable.chmod(0o000)
        settings.BASE_DIR = tmp_path
        try:
            assert template_cards() == []
        finally:
            unreadable.chmod(0o644)

    def test_a_rewritten_file_of_the_same_length_gets_a_fresh_card(self, settings: Any, tmp_path: Any) -> None:
        """The card cache keys on content, not on ``(mtime, size)``.

        An edit that keeps a file the same length is invisible to mtime
        granularity on a filesystem that rounds it, and a stale card would then
        outlive the file it describes.
        """
        library = tmp_path / "flow-templates"
        library.mkdir()
        target = library / "swapped.json"
        raw = (library_path() / "telegram-welcome-and-faq.json").read_text(encoding="utf-8")
        target.write_text(raw, encoding="utf-8")
        settings.BASE_DIR = tmp_path
        assert template_cards()[0].name == "Telegram welcome and FAQ"

        # Same byte length — three characters swapped for three others — and the
        # mtime deliberately restored, which is the case (mtime, size) misses.
        before = target.stat()
        renamed = raw.replace('"name": "Telegram welcome and FAQ"', '"name": "Telegram welcome and Faq"', 1)
        assert renamed != raw
        target.write_text(renamed, encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_size == before.st_size
        assert target.stat().st_mtime_ns == before.st_mtime_ns

        assert template_cards()[0].name == "Telegram welcome and Faq"

    def test_a_slug_with_a_trailing_newline_is_refused_by_the_pattern(self) -> None:
        """``\\Z`` and not ``$``: the latter also matches before a trailing newline,
        which would leave the stem comparison as the only thing refusing it."""
        assert SLUG_PATTERN.match("telegram-welcome-and-faq\n") is None

    def test_the_slug_the_idor_sweep_names_is_a_real_template(self) -> None:
        """``tests/idor.py`` needs a neutral value for ``template_slug`` and uses a
        shipped filename. Renaming that file would otherwise fail the cross-tenant
        sweep with an error about workspace isolation, which is the wrong place to
        go looking."""
        from tests.idor import NEUTRAL_KWARG_VALUES

        assert template_for_slug(NEUTRAL_KWARG_VALUES["template_slug"]) is not None


class TestTheValidateCommand:
    def test_it_passes_on_the_shipped_templates(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("validate_flow_templates", stdout=out)
        assert f"All {len(EXPECTED)} template(s) validate." in out.getvalue()

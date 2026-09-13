"""The import review page says which question is unanswered.

Found by a person, not by the suite: importing a template into a workspace with
no Instagram account produced a page reading "1 answer still needed before this
can be imported" with eight questions on it and no indication which one. The
platform picker's own message had been suppressed — deliberately, to avoid
repeating a note above it — so the single pointer to the problem was gone.

Every assertion here is about a reader being able to find the thing that is
blocking them, which is the one property the page exists for.
"""

from typing import Any

import pytest

from apps.flows.models import FlowImport, FlowImportStatus
from apps.flows.portability import library

pytestmark = pytest.mark.django_db


def _review(client_for: Any, tenancy: Any, slug: str = "collect-an-email-address") -> str:
    """Start an import of a shipped template and return the review page's HTML."""
    path = next(p for p in library.template_paths() if p.stem == slug)
    document, issues = library.read_template(path)
    assert document is not None and not issues

    record = FlowImport(
        workspace=tenancy.workspace,
        document=document,
        mapping={},
        original_filename=path.name,
        created_by=tenancy.owner,
    )
    record.save()
    url = f"/w/{tenancy.workspace.id}/flows/imports/{record.pk}/"
    return client_for(tenancy.owner).get(url).content.decode()


class TestAnUnansweredQuestionIsFindable:
    def test_the_card_that_needs_an_answer_says_so(self, client_for, tenancy) -> None:
        body = _review(client_for, tenancy)

        assert "is-unanswered" in body
        assert "Needs an answer" in body

    def test_the_reason_survives_an_empty_picker(self, client_for, tenancy) -> None:
        """The regression itself.

        This workspace has no Instagram account, so the channel picker offers
        nothing but the widening option — which is exactly when the sentence
        explaining what to do used to be hidden.
        """
        body = _review(client_for, tenancy)

        assert "No Instagram account is connected yet" in body, "the empty-picker note is gone"
        assert "Pick the Instagram account this trigger should watch" in body, (
            "the blocking question renders no reason at all, which is what shipped"
        )

    def test_the_footer_names_what_is_missing_rather_than_counting_it(self, client_for, tenancy) -> None:
        body = _review(client_for, tenancy)

        assert "Still to answer:" in body
        assert "Channels: Instagram" in body

    def test_the_footer_links_to_the_control_that_answers_it(self, client_for, tenancy) -> None:
        """A name is better than a count; a name you can click is better again."""
        body = _review(client_for, tenancy)

        assert 'href="#ask-platform-instagram"' in body
        assert 'id="ask-platform-instagram"' in body


class TestImportIsOneClick:
    """Import saves the answers on screen and applies them, in one request.

    It used to POST to the confirm endpoint on its own, so it acted on the
    answers the *server* last stored — and it was disabled whenever that stored
    plan was incomplete. Answering a question therefore did nothing until you
    also pressed "Save answers", and the button that looked like the way
    forward was inert with no styling to say so.
    """

    def _start(self, tenancy: Any, slug: str = "collect-an-email-address") -> FlowImport:
        path = next(p for p in library.template_paths() if p.stem == slug)
        document, _ = library.read_template(path)
        assert document is not None
        record = FlowImport(
            workspace=tenancy.workspace,
            document=document,
            mapping={},
            original_filename=path.name,
            created_by=tenancy.owner,
        )
        record.save()
        return record

    ANSWERS = {
        "tag|newsletter|action": "create",
        "tag|newsletter|name": "Newsletter",
        "custom_field|signup source|action": "create",
        "custom_field|signup source|name": "Signup source",
        "custom_field|signup source|field_type": "text",
        "trigger|flow-1:trigger-0|action": "keep",
    }

    def test_one_click_saves_and_imports(self, client_for, tenancy) -> None:
        record = self._start(tenancy)
        url = f"/w/{tenancy.workspace.id}/flows/imports/{record.pk}/"

        response = client_for(tenancy.owner).post(
            url, {**self.ANSWERS, "platform|instagram|id": "any", "then": "import"}, follow=True
        )

        record.refresh_from_db()
        assert record.status == FlowImportStatus.APPLIED
        assert response.redirect_chain[-1][0].endswith("/flows/")
        assert "arrived as draft" in response.content.decode()

    def test_an_unanswered_question_comes_back_flagged_rather_than_refusing(self, client_for, tenancy) -> None:
        """The trap, from the other side: clicking Import with a blank answer
        used to do nothing at all. Now it returns to the page and says what."""
        record = self._start(tenancy)
        url = f"/w/{tenancy.workspace.id}/flows/imports/{record.pk}/"

        response = client_for(tenancy.owner).post(
            url, {**self.ANSWERS, "platform|instagram|id": "", "then": "import"}, follow=True
        )
        body = response.content.decode()

        record.refresh_from_db()
        assert record.status == FlowImportStatus.PENDING, "nothing may be created from an incomplete plan"
        assert "is-unanswered" in body
        assert "Channels: Instagram" in body

    def test_the_answers_reaching_the_importer_are_the_ones_submitted(self, client_for, tenancy) -> None:
        """The heart of it: one request, so the stored mapping cannot be stale."""
        record = self._start(tenancy)
        url = f"/w/{tenancy.workspace.id}/flows/imports/{record.pk}/"

        client_for(tenancy.owner).post(
            url,
            {**self.ANSWERS, "tag|newsletter|name": "Renamed here", "platform|instagram|id": "any", "then": "import"},
        )

        from apps.contacts.models import Tag

        assert Tag.objects.for_workspace(tenancy.workspace).filter(name="Renamed here").exists()

    def test_the_button_is_never_disabled(self, client_for, tenancy) -> None:
        """A disabled control is how this was unexplainable in the first place."""
        body = _review(client_for, tenancy)
        row = body[body.index("Import 1 flow") - 400 : body.index("Import 1 flow")]

        assert "disabled" not in row


class TestAnsweringItClearsThePage:
    def test_nothing_is_flagged_once_every_question_has_an_answer(self, client_for, tenancy) -> None:
        path = next(p for p in library.template_paths() if p.stem == "collect-an-email-address")
        document, _ = library.read_template(path)
        assert document is not None

        record = FlowImport(
            workspace=tenancy.workspace,
            document=document,
            mapping={
                "tag": {"newsletter": {"action": "create", "name": "Newsletter"}},
                "custom_field": {"signup source": {"action": "create", "name": "Signup source", "field_type": "text"}},
                # "any" is the sentinel for "every account this kind of trigger
                # works on" — the only answer available with nothing connected.
                "platform": {"instagram": {"id": "any"}},
            },
            original_filename=path.name,
            created_by=tenancy.owner,
        )
        record.save()

        body = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/flows/imports/{record.pk}/").content.decode()

        assert "is-unanswered" not in body
        assert "Still to answer:" not in body
        assert "Import 1 flow" in body

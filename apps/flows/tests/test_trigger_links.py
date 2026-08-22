"""Ref-URL deep links, the handle seam, and the QR bytes."""

import pytest
import segno

from apps.common.platforms import Platform
from apps.flows.models import Trigger, TriggerType
from apps.flows.tests.support import connection_for, graph, node, published_flow
from apps.flows.triggers import links, qr

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


@pytest.fixture
def flow(tenancy):
    return published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="Ref flow")


def _trigger(flow, *, connection=None, ref="promo", handle=""):
    config = {"ref": ref}
    if handle:
        config["link_handle"] = handle
    trigger = Trigger(flow=flow, channel_connection=connection, type=TriggerType.REF_URL, config_json=config)
    trigger.save()
    return trigger


@pytest.mark.django_db
class TestDeepLinks:
    def test_messenger_works_with_no_registration(self, tenancy, flow):
        """m.me/<page-id> is a real deep link, and external_id *is* the page id —
        so one platform works out of the box."""
        connection = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-123")
        link = links.ref_link(connection, "promo", trigger=_trigger(flow, connection=connection))

        assert link.url == "https://m.me/page-123?ref=promo"
        assert link.available is True

    def test_telegram_reports_why_it_cannot_build_one(self, tenancy, flow):
        """external_id is a bot *id*, not a @username, and ChannelConnection has
        no column for one — apps/channels belongs to a sibling workstream."""
        connection = connection_for(tenancy.workspace, external_id="bot-999")
        link = links.ref_link(connection, "promo", trigger=_trigger(flow, connection=connection))

        assert link.url == ""
        assert "username" in link.unavailable_reason

    def test_the_trigger_can_carry_the_handle(self, tenancy, flow):
        """Step one of the three-step rule: a self-hoster gets a working link and
        QR today, with no adapter and no schema change."""
        connection = connection_for(tenancy.workspace, external_id="bot-999")
        trigger = _trigger(flow, connection=connection, handle="@my_bot")

        link = links.ref_link(connection, "promo", trigger=trigger)

        assert link.url == "https://t.me/my_bot?start=promo"

    def test_an_adapter_can_register_a_resolver(self, tenancy, flow):
        """Step two: one line in L4-B's own ready(), no edit here or in channels."""
        connection = connection_for(tenancy.workspace, external_id="bot-999")
        links.register_handle_resolver(Platform.TELEGRAM, lambda conn: "resolved_bot", replace=True)
        try:
            link = links.ref_link(connection, "promo", trigger=_trigger(flow, connection=connection))
            assert link.url == "https://t.me/resolved_bot?start=promo"
        finally:
            links._RESOLVERS.pop(Platform.TELEGRAM, None)

    def test_the_ref_needs_no_encoding(self, tenancy, flow):
        """REF_PATTERN restricts a ref to URL-safe characters, which is what lets
        the link and the bytes inside the QR be the same string."""
        connection = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-1")
        link = links.ref_link(connection, "a_b-C9", trigger=_trigger(flow, connection=connection, ref="a_b-C9"))

        assert link.url.endswith("?ref=a_b-C9")

    def test_a_platform_with_no_ref_support_says_so(self, tenancy, flow):
        connection = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550003")
        link = links.ref_link(connection, "promo")

        assert link.available is False
        assert "does not support" in link.unavailable_reason

    def test_an_unbound_trigger_covers_every_matching_connection(self, tenancy, flow):
        """One link per account, which is why the QR endpoint is addressed by
        connection rather than by trigger."""
        connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-1")
        connection_for(tenancy.workspace, platform=Platform.TELEGRAM, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550004")

        found = links.ref_links_for(_trigger(flow))

        assert {link.platform for link in found} == {Platform.MESSENGER, Platform.TELEGRAM}

    def test_a_bound_trigger_covers_only_its_own(self, tenancy, flow):
        mine = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-1")
        connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-2")

        found = links.ref_links_for(_trigger(flow, connection=mine))

        assert [link.connection.pk for link in found] == [mine.pk]

    def test_a_resolver_that_raises_does_not_take_the_panel_down(self, tenancy, flow):
        connection = connection_for(tenancy.workspace, external_id="bot-999")

        def explodes(conn):
            raise RuntimeError("boom")

        links.register_handle_resolver(Platform.TELEGRAM, explodes, replace=True)
        try:
            assert links.ref_link(connection, "promo").available is False
        finally:
            links._RESOLVERS.pop(Platform.TELEGRAM, None)


class TestQrRendering:
    def test_the_svg_encodes_exactly_the_link(self):
        """ "Scannable" without adding a decoder dependency: the response is
        byte-identical to what segno — a correct encoder — produces for that
        exact URL, so it *is* a QR code of that link."""
        url = "https://t.me/my_bot?start=promo"
        assert qr.render_svg(url) == _expected(url, "svg")

    def test_the_png_encodes_exactly_the_link(self):
        url = "https://m.me/page-1?ref=promo"
        assert qr.render_png(url) == _expected(url, "png")

    def test_two_different_links_give_different_codes(self):
        assert qr.render_svg("https://t.me/a?start=x") != qr.render_svg("https://t.me/a?start=y")

    def test_the_svg_carries_no_xml_declaration(self):
        """No declaration and no DOCTYPE, so the markup can be embedded as well
        as served and there is no external-entity surface at all."""
        body = qr.render_svg("https://t.me/a?start=x")

        assert body.startswith(b"<svg")
        assert b"DOCTYPE" not in body


def _expected(url: str, kind: str) -> bytes:
    import io

    buffer = io.BytesIO()
    if kind == "svg":
        segno.make(url, error=qr.ERROR_CORRECTION).save(
            buffer, kind="svg", scale=qr.SVG_SCALE, border=2, xmldecl=False, svgns=True, omitsize=False
        )
    else:
        segno.make(url, error=qr.ERROR_CORRECTION).save(buffer, kind="png", scale=qr.PNG_SCALE, border=2)
    return buffer.getvalue()

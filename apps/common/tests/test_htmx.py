"""The HTMX response helpers (apps/common/htmx.py)."""

import json

import pytest

from apps.common.htmx import toast_response, trigger_response


class TestTriggerResponse:
    def test_empty_204_with_the_events_in_the_header(self):
        response = trigger_response({"contactSaved": True})

        assert response.status_code == 204
        assert response.content == b""
        assert json.loads(response.headers["HX-Trigger"]) == {"contactSaved": True}

    def test_status_is_overridable(self):
        assert trigger_response({"x": 1}, status=200).status_code == 200

    def test_header_is_json_not_a_bare_event_name(self):
        """htmx only parses `detail` out of the header when it is a JSON object."""
        header = trigger_response({"showToast": {"tone": "info"}}).headers["HX-Trigger"]

        assert json.loads(header)["showToast"]["tone"] == "info"


class TestToastResponse:
    def test_payload_shape_matches_what_the_host_reads(self):
        """The keys here are the ones _toast_host.html destructures."""
        response = toast_response(tone="success", title="Saved", body="All good")

        assert response.status_code == 204
        assert json.loads(response.headers["HX-Trigger"]) == {
            "showToast": {"tone": "success", "title": "Saved", "body": "All good"}
        }

    def test_body_defaults_to_empty(self):
        payload = json.loads(toast_response(tone="info", title="Hi").headers["HX-Trigger"])

        assert payload["showToast"]["body"] == ""

    def test_extra_events_are_merged_alongside_the_toast(self):
        """The common pattern: toast + a refresh event for the calling surface."""
        response = toast_response(tone="success", title="Deleted", events={"contactsChanged": True})

        payload = json.loads(response.headers["HX-Trigger"])
        assert payload["showToast"]["title"] == "Deleted"
        assert payload["contactsChanged"] is True

    def test_extra_events_cannot_be_silently_dropped_by_a_falsy_value(self):
        payload = json.loads(toast_response(tone="info", title="t", events={"count": 0}).headers["HX-Trigger"])

        assert payload["count"] == 0

    @pytest.mark.parametrize("tone", ["success", "info", "warn", "error"])
    def test_every_tone_the_host_styles_round_trips(self, tone):
        payload = json.loads(toast_response(tone=tone, title="t").headers["HX-Trigger"])

        assert payload["showToast"]["tone"] == tone

    def test_arguments_are_keyword_only(self):
        with pytest.raises(TypeError):
            toast_response("success", "Saved")  # type: ignore[misc]

    def test_hostile_text_is_carried_verbatim_for_the_client_to_escape(self):
        """Toast bodies carry platform-supplied content (SECURITY-BASELINE §2).

        The server does not escape: the host writes both fields with
        ``textContent``, so escaping here would double-encode and *show* the
        entities to the user. What matters is that json.dumps cannot break out
        of the header, which the round-trip below proves.
        """
        hostile = "</script><img src=x onerror=alert(1)>"
        payload = json.loads(toast_response(tone="error", title="Failed", body=hostile).headers["HX-Trigger"])

        assert payload["showToast"]["body"] == hostile

"""The shared template tags (apps/common/templatetags/common_extras.py)."""

import json
import uuid

import pytest
from django.template import Context, Template
from django.utils.translation import gettext_lazy

from apps.common.templatetags.common_extras import json_attr, ui_select


class _Channel:
    """Stands in for a model instance without needing a real model."""

    def __init__(self, pk, name, platform=None):
        self.id = pk
        self.name = name
        self.platform = platform

    def __str__(self):
        return f"str:{self.name}"


class TestJsonAttr:
    def test_quotes_are_escaped_so_they_cannot_close_the_attribute(self):
        """The whole point: JSON's own quotes must not terminate x-data="..."."""
        rendered = json_attr([{"value": "a"}])

        assert '"' not in rendered
        assert "&quot;" in rendered

    def test_angle_brackets_are_escaped(self):
        rendered = json_attr({"label": "</script><img src=x onerror=alert(1)>"})

        assert "<" not in rendered
        assert ">" not in rendered
        assert "&lt;" in rendered

    def test_the_browser_decodes_back_to_the_original_json(self):
        """Escaping is for the HTML parser; Alpine must still receive valid JSON."""
        import html

        value = [{"value": "1", "label": 'He said "hi" & <left>'}]

        assert json.loads(html.unescape(json_attr(value))) == value

    @pytest.mark.parametrize("empty", [None, ""])
    def test_none_and_empty_string_both_become_an_empty_array(self, empty):
        """ "" is Django's string_if_invalid fallback for an unresolved variable.

        Without this, a typo'd template variable would emit `x-data="{opts: }"`
        and take the whole Alpine component down rather than rendering empty.
        """
        import html

        assert json.loads(html.unescape(json_attr(empty))) == []

    def test_uuids_and_other_non_json_types_serialize_via_default_str(self):
        import html

        oid = uuid.uuid4()

        assert json.loads(html.unescape(json_attr({"id": oid})))["id"] == str(oid)

    def test_output_is_marked_safe_so_django_does_not_double_escape(self):
        template = Template("{% load common_extras %}{{ value|json_attr }}")

        rendered = template.render(Context({"value": ["a"]}))

        assert "&amp;quot;" not in rendered

    def test_non_ascii_survives_unmangled(self):
        import html

        assert json.loads(html.unescape(json_attr(["café"]))) == ["café"]


class TestUiSelectNormalization:
    def test_dicts_pass_through_all_three_keys(self):
        ctx = ui_select(model="m", options=[{"value": "a", "label": "A", "icon": "telegram"}])

        assert ctx["options"] == [{"value": "a", "label": "A", "icon": "telegram"}]

    def test_dicts_with_missing_keys_do_not_raise(self):
        ctx = ui_select(model="m", options=[{"value": "a"}])

        assert ctx["options"] == [{"value": "a", "label": None, "icon": None}]

    def test_a_dict_is_matched_before_the_getattr_branch(self):
        """Branch order matters: a dict has no .id, so falling through would
        silently produce an empty value for every option."""
        ctx = ui_select(model="m", options=[{"value": "keep", "label": "Keep"}])

        assert ctx["options"][0]["value"] == "keep"

    def test_two_tuples_are_django_choices(self):
        ctx = ui_select(model="m", options=[("open", "Open"), ("closed", "Closed")])

        assert [o["value"] for o in ctx["options"]] == ["open", "closed"]
        assert [o["label"] for o in ctx["options"]] == ["Open", "Closed"]

    def test_longer_tuples_degrade_instead_of_raising(self):
        ctx = ui_select(model="m", options=[("a", "A", "extra", "more")])

        assert ctx["options"] == [{"value": "a", "label": "A", "icon": None}]

    def test_plain_strings_are_their_own_value_and_label(self):
        ctx = ui_select(model="m", options=["urgent", "vip"])

        assert ctx["options"] == [
            {"value": "urgent", "label": "urgent", "icon": None},
            {"value": "vip", "label": "vip", "icon": None},
        ]

    def test_model_instances_read_the_named_fields(self):
        ctx = ui_select(
            model="m",
            options=[_Channel("7", "Support", "telegram")],
            label_field="name",
            icon_field="platform",
        )

        assert ctx["options"] == [{"value": "7", "label": "Support", "icon": "telegram"}]

    def test_without_label_field_the_model_str_is_used(self):
        ctx = ui_select(model="m", options=[_Channel("7", "Support")])

        assert ctx["options"][0]["label"] == "str:Support"

    def test_a_typo_in_label_field_raises_rather_than_rendering_blanks(self):
        """Deliberate asymmetry: label_field uses the two-argument getattr, so a
        mistake surfaces at the call site instead of shipping empty rows."""
        with pytest.raises(AttributeError):
            ui_select(model="m", options=[_Channel("7", "Support")], label_field="nope")

    def test_a_missing_value_or_icon_field_is_tolerated(self):
        """Three-argument getattr: a heterogeneous list legitimately has options
        without an icon."""
        ctx = ui_select(
            model="m",
            options=[_Channel("7", "Support")],
            value_field="nope",
            label_field="name",
            icon_field="nope",
        )

        assert ctx["options"] == [{"value": "", "label": "Support", "icon": None}]

    def test_values_are_stringified_so_a_uuid_pk_compares_equal_in_alpine(self):
        oid = uuid.uuid4()

        ctx = ui_select(model="m", options=[_Channel(oid, "Support")], label_field="name")

        assert ctx["options"][0]["value"] == str(oid)

    def test_options_js_carries_value_and_label_only(self):
        """Icons are stripped: the Alpine trigger-label lookup does not need them."""
        ctx = ui_select(model="m", options=[{"value": "a", "label": "A", "icon": "sms"}])

        assert ctx["options_js"] == [{"value": "a", "label": "A"}]

    def test_options_js_stringifies_lazy_labels_so_json_dumps_cannot_choke(self):
        ctx = ui_select(model="m", options=[("a", gettext_lazy("Open"))])

        assert ctx["options_js"][0]["label"] == "Open"
        assert isinstance(ctx["options_js"][0]["label"], str)

    def test_arguments_are_keyword_only(self):
        with pytest.raises(TypeError):
            ui_select("m", [])  # type: ignore[misc]

    def test_defaults(self):
        ctx = ui_select(model="m", options=[])

        assert ctx["multiple"] is False
        assert ctx["onchange"] == ""
        assert ctx["placeholder"] == "Select"
        assert ctx["icon"] == ""


class TestUiSelectRendering:
    def _render(self, **kwargs):
        kwargs.setdefault("model", "filters.status")
        return Template(
            "{% load common_extras %}{% ui_select model=model options=options "
            "multiple=multiple placeholder=placeholder icon=icon %}"
        ).render(
            Context(
                {
                    "model": kwargs["model"],
                    "options": kwargs.get("options", []),
                    "multiple": kwargs.get("multiple", False),
                    "placeholder": kwargs.get("placeholder", "Select"),
                    "icon": kwargs.get("icon", ""),
                }
            )
        )

    def test_the_alpine_expression_is_interpolated_into_directives(self):
        html = self._render(options=[("a", "A")])

        assert "uiLabel(filters.status)" in html
        assert "filters.status = 'a'" in html

    def test_multiple_switches_to_checkboxes_bound_with_x_model(self):
        html = self._render(multiple=True, options=[("a", "A")])

        assert 'type="checkbox"' in html
        assert 'x-model="filters.status"' in html
        assert "filters.status.length" in html

    def test_the_panel_is_fixed_so_a_scrolling_toolbar_cannot_clip_it(self):
        html = self._render()

        assert "getBoundingClientRect()" in html
        assert "bb-select-panel fixed z-50" in html

    def test_the_panel_chrome_is_a_class_not_an_inline_style(self):
        """Alpine's :style REPLACES the style attribute, so a panel that
        declared its background inline lost it the moment it was positioned and
        opened transparent over the page."""
        html = self._render()

        assert ":style=" in html
        panel = html[html.index("bb-select-panel") - 200 :]
        panel = panel[: panel.index(">") + 1]
        assert 'style="background' not in panel

    def test_the_trigger_uses_the_shared_class_not_a_page_local_one(self):
        """Deviation 5: these styles live in styles.css, not in one page."""
        html = self._render()

        assert "bb-filter-select" in html
        assert "cal-filter-select" not in html

    def test_option_labels_are_escaped(self):
        html = self._render(options=[("a", "<script>alert(1)</script>")])

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_option_values_are_escaped_inside_the_alpine_string_literal(self):
        html = self._render(options=[("it's", "X")])

        assert "filters.status = 'it\\u0027s'" in html or "\\'" in html

    def test_the_icon_partial_is_included_when_an_option_has_one(self):
        html = self._render(options=[{"value": "a", "label": "A", "icon": "telegram"}])

        assert "pi-telegram" in html
        assert 'aria-label="telegram"' in html

    def test_no_inline_event_handler_attributes_are_emitted(self):
        """SECURITY-BASELINE §8: the CSP forbids inline handlers. Alpine's
        @click/x-on are compiled by Alpine, not by the HTML parser, so they are
        fine; a literal onclick= would not be."""
        html = self._render(options=[("a", "A")])

        for attr in ["onclick=", "onchange=", "onmouseover=", "onerror="]:
            assert attr not in html.lower()

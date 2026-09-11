"""The Browse-templates cards.

Two things are being protected here. One is that the sidecar and the directory
stay in step, because they are two lists and nothing else makes them agree. The
other is that this module never raises: the cards render inside the flows list
empty state, so an exception here is a 500 on the core Flows page of every
brand-new workspace — the exact audience it was built for.
"""

import re

import pytest

from apps.flows.portability import gallery, library

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    """The memo is module-level, so a test that points BASE_DIR somewhere else
    must not inherit the previous test's answer."""
    gallery._CACHE = None
    yield
    gallery._CACHE = None


def _write(directory, name, document):
    import json

    (directory / name).write_text(json.dumps(document))


@pytest.fixture
def library_dir(tmp_path, settings, monkeypatch):
    """A library somewhere writable, with the real one's first template in it."""
    directory = tmp_path / "flow-templates"
    directory.mkdir()
    real = library.library_path() / "telegram-welcome-and-faq.json"
    (directory / real.name).write_bytes(real.read_bytes())
    monkeypatch.setattr(library, "library_path", lambda: directory)
    return directory


class TestTheManifest:
    def test_every_shipped_template_has_an_entry(self):
        manifest = gallery._manifest(library.library_path() / gallery.GALLERY_MANIFEST_NAME)
        shipped = {path.name for path in library.template_paths()}

        assert shipped - set(manifest) == set()

    def test_every_entry_names_a_shipped_file(self):
        manifest = gallery._manifest(library.library_path() / gallery.GALLERY_MANIFEST_NAME)
        shipped = {path.name for path in library.template_paths()}

        assert set(manifest) - shipped == set()

    def test_the_manifest_is_not_itself_a_template(self):
        """The whole reason it is TOML. library.template_paths() globs *.json
        and hands every match to the importer, so a JSON sidecar here would be
        validated as a flow template and rejected."""
        assert gallery.GALLERY_MANIFEST_NAME not in {path.name for path in library.template_paths()}

    def test_every_shipped_filename_can_be_a_url_segment(self):
        """A stem that is not slug-safe makes {% url %} raise *during template
        rendering*, where no try/except in a view can catch it."""
        for path in library.template_paths():
            assert re.match(r"^[-a-zA-Z0-9_]+$", path.stem), path.name

    def test_every_card_has_a_summary(self):
        assert [card.slug for card in gallery.gallery() if not card.summary] == []


class TestTheCards:
    def test_the_instagram_template_says_it_needs_instagram(self):
        """Readable without importing anything, which is the whole point of
        putting the badge on the card rather than on the review page."""
        card = next(c for c in gallery.gallery() if c.slug == "instagram-comment-to-dm-lead-magnet")

        assert [p["key"] for p in card.platforms] == ["instagram"]
        assert [p["label"] for p in card.platforms] == ["Instagram"]

    def test_the_counts_match_the_document(self):
        for card in gallery.gallery():
            document, _ = library.read_template(library.library_path() / card.filename)
            flows = document["flows"]

            assert card.flow_count == len(flows)
            assert card.node_count == sum(len(f["graph"]["nodes"]) for f in flows)
            assert card.trigger_count == sum(len(f.get("triggers") or []) for f in flows)

    def test_created_kinds_are_grouped_and_counted(self):
        card = next(c for c in gallery.gallery() if c.slug == "sms-keyword-opt-in")

        assert card.creates == ("1 tag", "1 custom field")

    def test_the_title_falls_back_to_the_entry_flow_name(self, library_dir):
        """No manifest at all, so every card is titled by its own document — and
        a card cannot drift from the flow it installs."""
        cards = gallery.gallery()

        assert [card.title for card in cards] == ["Telegram welcome and FAQ"]
        assert cards[0].category == gallery.DEFAULT_CATEGORY

    def test_the_requirements_come_from_the_walk_not_the_manifest(self, library_dir):
        """docs/flow-templates calls the in-file manifest advisory. A document
        that forgot to declare its platform still shows the badge."""
        document, _ = library.read_template(library_dir / "telegram-welcome-and-faq.json")
        document["requirements"] = dict.fromkeys(document["requirements"], [])
        _write(library_dir, "telegram-welcome-and-faq.json", document)
        gallery._CACHE = None

        assert [p["key"] for p in gallery.gallery()[0].platforms] == ["telegram"]


class TestItNeverRaises:
    def test_a_template_that_does_not_validate_is_skipped(self, library_dir):
        _write(library_dir, "broken.json", {"app": "brightbean-chat", "format": 999})

        assert {card.slug for card in gallery.gallery()} == {"telegram-welcome-and-faq"}

    def test_a_file_that_is_not_json_at_all_is_skipped(self, library_dir):
        (library_dir / "garbage.json").write_text("this is not json")

        assert {card.slug for card in gallery.gallery()} == {"telegram-welcome-and-faq"}

    def test_a_filename_that_is_not_slug_safe_is_skipped(self, library_dir):
        real = (library_dir / "telegram-welcome-and-faq.json").read_bytes()
        (library_dir / "My Template.json").write_bytes(real)

        assert {card.slug for card in gallery.gallery()} == {"telegram-welcome-and-faq"}

    def test_a_broken_manifest_falls_back_to_document_titles(self, library_dir):
        (library_dir / gallery.GALLERY_MANIFEST_NAME).write_text("[[template]\nthis is not toml")

        assert [card.title for card in gallery.gallery()] == ["Telegram welcome and FAQ"]

    def test_a_manifest_entry_for_a_missing_file_is_ignored(self, library_dir):
        (library_dir / gallery.GALLERY_MANIFEST_NAME).write_text(
            '[[template]]\nfile = "not-here.json"\nsummary = "x"\n'
        )

        assert [card.slug for card in gallery.gallery()] == ["telegram-welcome-and-faq"]

    def test_a_template_with_no_manifest_entry_still_appears(self, library_dir):
        real = (library_dir / "telegram-welcome-and-faq.json").read_bytes()
        (library_dir / "second.json").write_bytes(real)
        (library_dir / gallery.GALLERY_MANIFEST_NAME).write_text(
            '[[template]]\nfile = "telegram-welcome-and-faq.json"\nsummary = "listed"\n'
        )

        cards = gallery.gallery()

        assert {card.slug for card in cards} == {"telegram-welcome-and-faq", "second"}
        # Listed first, unlisted after.
        assert cards[0].slug == "telegram-welcome-and-faq"

    def test_a_missing_library_is_an_empty_gallery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "library_path", lambda: tmp_path / "nowhere")

        assert gallery.gallery() == []


class TestTheCache:
    def test_a_second_call_does_not_re_read(self, library_dir, monkeypatch):
        gallery.gallery()
        calls = []
        monkeypatch.setattr(library, "read_template", lambda path: calls.append(path) or (None, []))

        gallery.gallery()

        assert calls == []

    def test_editing_a_template_invalidates_it(self, library_dir):
        assert gallery.gallery()[0].title == "Telegram welcome and FAQ"
        document, _ = library.read_template(library_dir / "telegram-welcome-and-faq.json")
        document["flows"][0]["name"] = "Renamed"
        _write(library_dir, "telegram-welcome-and-faq.json", document)

        assert gallery.gallery()[0].title == "Renamed"


class TestTheLookup:
    def test_a_real_slug_resolves(self):
        assert gallery.template_for_slug("telegram-welcome-and-faq") is not None

    @pytest.mark.parametrize(
        "slug",
        ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "gallery", "telegram-welcome-and-faq.json", ""],
    )
    def test_anything_that_is_not_a_shipped_stem_resolves_to_nothing(self, slug):
        """Built from the directory listing and looked up, never joined onto a
        path — so traversal is not defended against, it is unrepresentable."""
        assert gallery.template_for_slug(slug) is None

    def test_the_idor_suites_neutral_slug_is_a_real_template(self):
        """tests/idor.py hardcodes a stem so its sweep 404s on the workspace and
        not on a template that is not there. Renaming a template without
        updating it would make that sweep pass for the wrong reason."""
        from tests.idor import NEUTRAL_KWARG_VALUES

        assert NEUTRAL_KWARG_VALUES["template_slug"] in {card.slug for card in gallery.gallery()}

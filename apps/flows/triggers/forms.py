"""Turning a POST into a validated ``config_json``.

Hand-parsed rather than a ``ModelForm``, because none of the four shapes here is
a flat field set: a keyword list is two parallel ``getlist``s, the comment config
nests, and the JSON schema in :mod:`apps.flows.triggers.schema` is already the
authority on what is valid. A form class would be a second, weaker copy of it.

What this module owns is **normalisation** — trimming, dropping blanks, deduping
— which happens before validation so that a user typing a trailing space does not
get a schema error about it.
"""

from typing import Any

from apps.flows.triggers.registry import spec_for
from apps.flows.triggers.schema import MAX_KEYWORDS
from apps.flows.triggers.types import TriggerType

__all__ = ["KeywordMismatchError", "config_from_post"]

_MODES = {"exact", "contains", "any_word"}


class KeywordMismatchError(ValueError):
    """The keyword text and mode lists did not line up."""


def config_from_post(trigger_type: str, post: Any) -> dict[str, Any]:
    """Build this type's config from a submitted form.

    Never trusts lengths or values — the schema check that follows is what
    accepts it. This only has to produce something *shaped* like a config.
    """
    spec = spec_for(trigger_type)
    if spec is None:
        return {}
    if trigger_type in {TriggerType.KEYWORD, TriggerType.STORY_REPLY}:
        return {"keywords": _keywords(post)}
    if trigger_type == TriggerType.REF_URL:
        return {
            "ref": (post.get("ref") or "").strip(),
            "link_handle": (post.get("link_handle") or "").strip().lstrip("@"),
        }
    if trigger_type == TriggerType.COMMENT:
        return _comment(post)
    if trigger_type == TriggerType.API:
        return {"key": (post.get("key") or "").strip()}
    if trigger_type == TriggerType.RULE:
        return {"event": (post.get("event") or "").strip()}
    return spec.default_config()


def _keywords(post: Any) -> list[dict[str, str]]:
    """Zip the two parallel lists the repeatable rows post.

    A length mismatch **raises** rather than zipping to the shorter list. Silently
    pairing off a truncated list would give somebody a trigger matching words
    they did not configure, in modes they did not pick, and the only signal would
    be a keyword quietly missing from the panel afterwards.
    """
    texts = post.getlist("keyword_text")
    modes = post.getlist("keyword_mode")
    if len(texts) != len(modes):
        raise KeywordMismatchError("Each keyword needs a matching mode.")

    seen: set[str] = set()
    keywords: list[dict[str, str]] = []
    for text, mode in zip(texts, modes, strict=True):
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        # Deduped case-insensitively: two keywords differing only in case would
        # both match the same messages, and the second could never win.
        fingerprint = f"{cleaned.casefold()}|{mode}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        keywords.append({"text": cleaned, "mode": mode if mode in _MODES else "contains"})
        if len(keywords) >= MAX_KEYWORDS:
            break
    return keywords


def _comment(post: Any) -> dict[str, Any]:
    mode = (post.get("public_reply_mode") or "none").strip()
    return {
        "post_scope": (post.get("post_scope") or "all").strip(),
        "post_ids": _lines(post.get("post_ids")),
        "include_keywords": _lines(post.get("include_keywords")),
        "exclude_keywords": _lines(post.get("exclude_keywords")),
        "top_level_only": _flag(post, "top_level_only"),
        "public_reply": {
            "mode": mode,
            "texts": [] if mode == "none" else _lines(post.get("public_reply_texts")),
        },
        "like_comment": _flag(post, "like_comment"),
        "once_per_contact_per_post": _flag(post, "once_per_contact_per_post"),
    }


def _lines(value: Any) -> list[str]:
    """One entry per line, trimmed, blanks dropped, order preserved, deduped."""
    if not isinstance(value, str):
        return []
    seen: set[str] = set()
    entries: list[str] = []
    for line in value.splitlines():
        cleaned = line.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            entries.append(cleaned)
    return entries


def _flag(post: Any, name: str) -> bool:
    """An unchecked checkbox posts nothing at all, which is what ``in`` reads."""
    return name in post

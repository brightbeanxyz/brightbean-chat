"""Reader-facing sentences for a flow's triggers.

SPEC §10's vocabulary is precise and developer-shaped: ``ref_url``,
``default_reply``, ``story_mention``. :class:`TriggerType`'s own labels are
better ("New follower") but still name the *mechanism* rather than the moment —
and a flow list that says "Keyword · Comment" tells a reader what the product
calls things, not when their flow runs.

So this module answers one question in the reader's words: **when does this
flow start?** "When someone comments on a post." "When a message contains
'hours' or 'returns'."

**Why here and not on TriggerType.** Those labels are the admin's and the
developer's; they appear in forms, in the API and in error messages, and they
should stay stable and literal. This is UI copy, revised on the product's
cadence rather than the schema's, and a second string on the enum would put two
registers in one place and invite the wrong one being picked.

**Why it degrades rather than raises.** A trigger type with no phrase here —
one a later layer adds — falls back to its enum label. A flow list is not the
place to discover that a migration landed without copy.
"""

from typing import Any

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.flows.triggers.types import TriggerType

__all__ = ["CAVEATS", "MAX_QUOTED_KEYWORDS", "describe_trigger", "describe_triggers", "sentence_case"]

#: How many keywords a sentence quotes before it gives up and counts.
#: Three is the point at which "a, b or c" stops reading as a phrase and starts
#: reading as a list.
MAX_QUOTED_KEYWORDS = 3

#: One phrase per trigger type, written to follow "This flow runs…".
_PHRASES: dict[str, Any] = {
    TriggerType.KEYWORD: _("when someone sends a keyword"),
    TriggerType.COMMENT: _("when someone comments on a post"),
    TriggerType.STORY_MENTION: _("when someone mentions you in a story"),
    TriggerType.STORY_REPLY: _("when someone replies to your story"),
    TriggerType.FOLLOW: _("when someone follows you"),
    TriggerType.REF_URL: _("when someone opens your link or scans your QR code"),
    TriggerType.DEFAULT_REPLY: _("when nothing else matches"),
    TriggerType.WELCOME: _("when someone messages you for the first time"),
    TriggerType.RULE: _("when a message matches your rules"),
    TriggerType.API: _("when another system asks it to"),
}


#: Trigger types that cannot fire today, as a sentence of their own.
#:
#: A sentence rather than a clause, because it has to sit after a join it does
#: not control. As a clause it read "When someone follows you, which Instagram
#: does not support yet, so it will not run, or when someone comments" — three
#: commas and a dangling "or" — the moment a flow had a second trigger.
#:
#: Worded to fit both: "it" is the trigger just named when a flow has one, and
#: the new-follower trigger among several when it has more.
#:
#: ``TriggerSpec.unavailable`` says the same thing at the length a form can
#: afford. This one has to survive a truncated meta line beside "edited 2 hours
#: ago", and must still stop a reader, because the pill above it says "Live".
CAVEATS: dict[str, Any] = {
    TriggerType.FOLLOW: _("Instagram does not support new-follower triggers yet, so it will not run."),
}


def _quoted_keywords(config: dict[str, Any]) -> str:
    """The keywords a keyword trigger listens for, as a readable clause.

    Two shapes reach this. SPEC §10's keyword trigger stores
    ``{"keywords": [{"text": ..., "mode": ...}]}``; a comment trigger stores a
    plain list under ``include_keywords``. Both are read because both are
    "words that start this flow" to a reader, and neither is worth a branch at
    the call site.
    """
    words = [
        str(entry.get("text", "")).strip() if isinstance(entry, dict) else str(entry).strip()
        for entry in (config.get("keywords") or config.get("include_keywords") or [])
    ]
    words = [word for word in words if word]
    if not words:
        return ""
    if len(words) > MAX_QUOTED_KEYWORDS:
        return gettext("“%(word)s” and %(count)s more") % {"word": words[0], "count": len(words) - 1}
    if len(words) == 1:
        return f"“{words[0]}”"
    return gettext("%(list)s or “%(last)s”") % {
        "list": ", ".join(f"“{word}”" for word in words[:-1]),
        "last": words[-1],
    }


def describe_trigger(trigger: Any) -> str:
    """One trigger, as a sentence fragment starting with "when".

    Says when it *would* run and never whether it can: a trigger the platform
    never delivers is described here exactly like one that fires, and
    :func:`describe_triggers` adds :data:`CAVEATS` once for the whole flow.
    """
    phrase = _PHRASES.get(trigger.type)
    if phrase is None:
        # A type added without copy. Its enum label is wrong in register but
        # right in substance, which beats an empty line on a list page.
        return gettext("when a %(type)s trigger fires") % {"type": trigger.get_type_display().lower()}

    config = trigger.config_json if isinstance(trigger.config_json, dict) else {}
    quoted = _quoted_keywords(config)
    if quoted and trigger.type == TriggerType.KEYWORD:
        phrase = gettext("when someone sends %(quoted)s") % {"quoted": quoted}
    elif quoted and trigger.type == TriggerType.COMMENT:
        phrase = gettext("when someone comments %(quoted)s") % {"quoted": quoted}

    return str(phrase)


def sentence_case(phrase: str) -> str:
    """Upper-case the first letter and touch nothing else.

    Not ``str.capitalize()``, which lower-cases the whole remainder: a keyword
    trigger reading ``when someone sends "Black Friday"`` came out of it as
    ``"black friday"``, silently rewriting a word the workspace chose, and the
    same would happen to any brand name a phrase quotes.
    """
    return phrase[:1].upper() + phrase[1:]


def describe_triggers(triggers: list[Any]) -> str:
    """Every trigger on a flow, as one sentence.

    Empty is not "no description" but a warning worth reading: a flow with no
    trigger is a flow that will never run, and saying so on the list is how
    somebody notices before they wonder why nothing happened.

    Beyond two triggers the sentence stops listing and starts counting — three
    "when…" clauses joined by "or" is a paragraph, not a row.
    """
    enabled = [t for t in triggers if getattr(t, "enabled", True)]
    if not enabled:
        return gettext("No trigger yet, so it will not run until you add one")
    if len(enabled) == 1:
        sentence = sentence_case(describe_trigger(enabled[0]))
    elif len(enabled) == 2:
        sentence = gettext("%(first)s, or %(second)s") % {
            "first": sentence_case(describe_trigger(enabled[0])),
            "second": describe_trigger(enabled[1]),
        }
    else:
        sentence = gettext("%(first)s, and %(count)s other triggers") % {
            "first": sentence_case(describe_trigger(enabled[0])),
            "count": len(enabled) - 1,
        }

    # Appended once, however many triggers there are, and de-duplicated: two
    # trigger types blocked for the same reason should say it once.
    notes = dict.fromkeys(str(note) for t in enabled if (note := CAVEATS.get(t.type)))
    return " ".join([sentence + "." if notes else sentence, *notes])

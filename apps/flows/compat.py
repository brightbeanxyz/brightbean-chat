"""Reaching an app that has not been built yet.

Several things this issue serves belong to workstreams still in flight — tags
and custom fields to #3, channel connections to #4, sequences to L6-A. Each of
those lookups has to answer "not yet" rather than raising, and each has to start
answering for real the moment its app is installed, with no edit here.

One helper rather than one per call site: "is this app installed *and* does it
have the model I expect" is the whole question, and a second copy of it is where
the two would eventually disagree about what "not yet" looks like.
"""

from typing import Any

from django.apps import apps as django_apps

__all__ = ["installed_model"]


def installed_model(app_label: str, app_module: str, model_name: str) -> Any | None:
    """The model class, or ``None`` while the app that owns it has not landed."""
    if not django_apps.is_installed(app_module):
        return None
    try:
        return django_apps.get_model(app_label, model_name)
    except LookupError:  # pragma: no cover - installed, but the model is not there
        return None

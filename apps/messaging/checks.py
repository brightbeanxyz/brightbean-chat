"""Startup checks for this app's settings.

``DEFAULT_SEND_RATE_OVERRIDES`` is operator-authored JSON from the environment,
and every way of getting it wrong is silent at boot: a typo in a platform name
simply never applies, and a zero rate means a bucket that never fills. Both show
up much later as "why is nothing sending", so they are caught here instead —
the shape ``apps.common.checks.check_platform_env_slugs`` already uses for the
same class of mistake.
"""

from typing import Any

from django.conf import settings
from django.core.checks import Error, register
from django.core.checks import Tags as CheckTags

from apps.common.platforms import Platform

__all__ = ["check_send_rate_overrides"]


@register(CheckTags.compatibility)
def check_send_rate_overrides(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    overrides = getattr(settings, "DEFAULT_SEND_RATE_OVERRIDES", {})
    if not isinstance(overrides, dict):
        return [
            Error(
                "DEFAULT_SEND_RATE_OVERRIDES must be a JSON object mapping a platform to a rate.",
                hint='For example: DEFAULT_SEND_RATE_OVERRIDES={"telegram": 10}',
                id="messaging.E001",
            )
        ]

    errors: list[Error] = []
    for platform, rate in overrides.items():
        if platform not in Platform.values:
            errors.append(
                Error(
                    f"DEFAULT_SEND_RATE_OVERRIDES names unknown platform {platform!r}.",
                    hint=f"Known platforms: {', '.join(sorted(Platform.values))}.",
                    id="messaging.E002",
                )
            )
            continue
        if not isinstance(rate, int | float) or isinstance(rate, bool) or rate <= 0:
            errors.append(
                Error(
                    f"DEFAULT_SEND_RATE_OVERRIDES[{platform!r}] must be a number greater than zero, got {rate!r}.",
                    hint="A rate of zero is a bucket that never fills, which reads as a silent outage.",
                    id="messaging.E003",
                )
            )
    return errors

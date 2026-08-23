"""Flow-node runtimes this app owns.

``apps/flows/engine/nodes/__init__.py`` says where these belong:

    ``send_sms`` and ``send_email`` (L5-D/E) arrive with their own layers and
    **register from their own apps**.

Registration is an import side effect, as it is there. ``ChannelsConfig.ready``
imports this package; nothing else should need to.
"""

from apps.channels.nodes import send_email  # noqa: F401 - imported for its registration

__all__: list[str] = []

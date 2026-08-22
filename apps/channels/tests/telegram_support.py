"""A fake Telegram Bot API, and the fixtures that feed it.

Two things every Telegram test needs and none of them should build again:

:func:`fake_bot_api`
    Routes the adapter's HTTP through ``httpx.MockTransport``, so nothing opens
    a socket. It replaces :func:`apps.channels.providers.telegram._client`,
    which is the seam that module documents for exactly this — and going through
    it rather than stubbing ``request_json`` means the **real** error mapping
    runs: a 429 really does become a ``RateLimitError`` carrying the
    ``retry_after`` the fake sent, which is the thing several of these tests are
    about.

:func:`load_update`
    A recorded update from ``fixtures/telegram/``. Real payload shapes, so a
    test that passes here is a test against what Telegram actually sends rather
    than against what we imagined it sends.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from apps.channels.providers import telegram

FIXTURES = Path(__file__).parent / "fixtures" / "telegram"

#: A token shaped like a real one — ``<bot_id>:<35 chars>`` — because the log
#: scrubber recognises that shape, and a test using "secret" as a token would
#: prove nothing about it.
BOT_TOKEN = "123456789:AAHfiqksKZ8WmR2zSjiQ7_v4vJ4tqZzVFLU"  # noqa: S105 - a fake credential for tests


def load_update(name: str) -> dict[str, Any]:
    """One recorded update, by fixture file name (without ``.json``)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@dataclass
class Reply:
    """What the fake answers for one Bot API method."""

    result: Any = None
    status: int = 200
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx.Response:
        if self.body is not None:
            return httpx.Response(self.status, json=self.body, headers=self.headers)
        if self.status >= 400:
            return httpx.Response(
                self.status,
                json={"ok": False, "error_code": self.status, "description": "Fake failure"},
                headers=self.headers,
            )
        return httpx.Response(self.status, json={"ok": True, "result": self.result}, headers=self.headers)


@dataclass
class FakeBotAPI:
    """A recording fake. ``calls`` is ``(method, payload)`` in the order sent."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    replies: dict[str, Reply] = field(default_factory=dict)
    #: Answers any method with no reply of its own.
    default: Reply = field(default_factory=lambda: Reply(result={"message_id": 1}))
    #: Tokens the fake was called with, so a test can assert one was used — and,
    #: more usefully, that it never reached a log.
    tokens: list[str] = field(default_factory=list)

    def reply(self, method: str, reply: Reply) -> None:
        self.replies[method] = reply

    def payloads(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def methods(self) -> list[str]:
        return [name for name, _payload in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        # The path is /bot<token>/<method>.
        _, _, tail = request.url.path.partition("/bot")
        token, _, method = tail.partition("/")
        self.tokens.append(token)
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        return self.replies.get(method, self.default).response()


@contextmanager
def fake_bot_api(configure: Callable[[FakeBotAPI], None] | None = None) -> Iterator[FakeBotAPI]:
    """Run the block with every Bot API call answered by an in-memory fake."""
    fake = FakeBotAPI()
    if configure is not None:
        configure(fake)

    client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    original = telegram._client
    # One client for the whole block. `request_json` only closes a client it
    # created itself, so handing out a fresh one per call would leak them.
    telegram._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        telegram._client = original  # type: ignore[assignment]
        client.close()

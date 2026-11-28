"""Shared types and helpers for provider adapters.

Neutral history format (plain dicts, provider-independent):

    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [{"id", "name", "args"}],
     "raw": {"<provider name>": <provider-specific message>}}
    {"role": "tool", "id": "<tool call id>", "name": "<tool name>", "result": "..."}

Each adapter converts this to its own API format. `raw` is optional: if the
provider that answers a turn is the same one asked to continue, it can reuse
its own message object unchanged (Gemini needs this for thought signatures).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def new_call_id() -> str:
    # 9 alphanumeric chars: accepted by every provider we target (some are picky).
    return uuid.uuid4().hex[:9]


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = field(default_factory=new_call_id)


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    raw: Any = None  # provider-specific message object, kept for that provider only

    def to_message(self) -> dict:
        """Neutral history entry for this reply."""
        return {
            "role": "assistant",
            "text": self.text,
            "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in self.tool_calls],
            "raw": {self.provider: self.raw} if self.raw is not None else {},
        }


# --- error classification --------------------------------------------------

TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_NAME_HINTS = ("Timeout", "ConnectError", "ConnectionError", "RemoteProtocol")


def _status(exc: Exception) -> int | None:
    # google-genai errors expose `.code`, openai errors expose `.status_code`
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def is_rate_limited(exc: Exception) -> bool:
    return _status(exc) == 429


PROVIDER_FATAL_CODES = {401, 403, 404}  # bad key, no access, unknown model


def is_provider_fatal(exc: Exception) -> bool:
    """True when this provider is misconfigured (key/model). Retrying will never help,
    but other providers have their own keys and models, so failing over still can."""
    return _status(exc) in PROVIDER_FATAL_CODES


def is_history_rejected(exc: Exception) -> bool:
    """True when Gemini refuses the conversation because tool calls made by another provider
    carry no thought signature. Every later request would fail the same way, so the
    provider is skipped for the rest of the session instead of being tried again."""
    return _status(exc) == 400 and "thought_signature" in str(exc).lower()


def is_transient(exc: Exception) -> bool:
    """True for errors worth retrying or failing over on (overload, rate limit, network)."""
    if _status(exc) in TRANSIENT_CODES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(hint in type(exc).__name__ for hint in _TRANSIENT_NAME_HINTS)

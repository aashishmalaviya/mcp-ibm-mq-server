"""Trace context for every tool call (W3C Trace Context).

When an agent framework or gateway sends a ``traceparent`` (as an HTTP header
or in the MCP request's ``_meta``), each tool call joins that trace as a new
span. Without one, the call starts a new trace. Either way every audit line
carries ``trace_id``, ``span_id``, ``parent_span_id`` and ``duration_ms``, so
one ID follows a request from the user's click, through the agent, this
server and IBM MQ, to the system that consumes the message.

Into MQ: when the agent sends a message without its own correlation ID, the
trace ID goes into the message's CorrelId, so downstream consumers can join
the same trace.

A malformed traceparent is never trusted or copied: the call starts a fresh
trace instead.
"""

from __future__ import annotations

import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE, _ZERO_SPAN = "0" * 32, "0" * 16

# The traceparent the current tool call arrived with (set by the server per request).
incoming_traceparent: ContextVar[str | None] = ContextVar("incoming_traceparent", default=None)


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    flags: str
    started: float

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    def elapsed_ms(self, clock=time.monotonic) -> float:
        return round((clock() - self.started) * 1000, 3)


def parse_traceparent(value: Any) -> tuple[str, str, str] | None:
    """Return (trace_id, parent_span_id, flags) for a valid version-00 traceparent, else None."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.match(value.strip().lower())
    if not match:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id, span_id, flags


def start_span(traceparent: str | None = None, clock=time.monotonic) -> Span:
    parsed = parse_traceparent(traceparent)
    if parsed:
        trace_id, parent, flags = parsed
    else:
        trace_id, parent, flags = secrets.token_hex(16), None, "01"
    return Span(trace_id=trace_id, span_id=secrets.token_hex(8), parent_span_id=parent,
                flags=flags, started=clock())


def traceparent_from_context(ctx: Any) -> str | None:
    """Find a traceparent in an MCP request: ``_meta`` first, then HTTP headers."""
    meta = None
    try:
        meta = ctx.request_context.meta
    except Exception:  # noqa: BLE001 - no request context (e.g. direct calls in tests)
        meta = None
    value = None
    if isinstance(meta, Mapping):
        value = meta.get("traceparent")
    elif meta is not None:
        value = getattr(meta, "traceparent", None)
    if not value:
        try:
            headers = ctx.headers or {}
        except Exception:  # noqa: BLE001
            headers = {}
        value = headers.get("traceparent")
    return value if isinstance(value, str) else None

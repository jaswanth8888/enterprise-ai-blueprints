# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Per-session event bus feeding the browser over SSE.

Tools and the sandbox manager call emit(); the current session and tool call
come from a ContextVar, which asyncio copies into gathered tasks, so fan-out
sandbox events carry the call that started them.
"""

import asyncio
import itertools
import json
import os
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

BUFFER = 5000
SUBSCRIBER_QUEUE = 2000
_SECRET_ENV = ("E2B_API_KEY", "OPENAI_API_KEY", "APP_AUTH_TOKEN")


def scrub(text: str) -> str:
    """Remove secret values from text bound for the browser or the logs."""
    for name in _SECRET_ENV:
        value = os.getenv(name, "")
        if len(value) >= 8:
            text = text.replace(value, f"<{name}>")
    return text


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


class SessionBus:
    """Ordered events for one session: a replay buffer plus live subscribers."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._seq = itertools.count(1)
        self.last_seq = 0
        self.buffer: deque[dict] = deque(maxlen=BUFFER)
        self._subscribers: set[asyncio.Queue] = set()

    def _envelope(self, seq: int, type_: str, data: dict, run_id: str | None, call_id: str | None) -> dict:
        return {
            "v": 1,
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "run_id": run_id,
            "call_id": call_id,
            "type": type_,
            "data": data,
        }

    def publish(self, type_: str, data: dict, run_id: str | None = None, call_id: str | None = None) -> dict:
        self.last_seq = next(self._seq)
        event = self._envelope(self.last_seq, type_, data, run_id, call_id)
        self.buffer.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A lagging browser: drop it; it reconnects and replays from the buffer.
                self._subscribers.discard(queue)
        return event

    def replay_after(self, last_seq: int) -> list[dict] | None:
        """Buffered events after last_seq, or None when some of them have been dropped."""
        if self.buffer and self.buffer[0]["seq"] > last_seq + 1:
            return None
        return [e for e in self.buffer if e["seq"] > last_seq]

    def snapshot_event(self, data: dict) -> dict:
        """A snapshot for one client only; it carries the latest seq so the client resumes after it."""
        return self._envelope(self.last_seq, "session.snapshot", data, None, None)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def is_subscribed(self, queue: asyncio.Queue) -> bool:
        return queue in self._subscribers


@dataclass
class RunContext:
    bus: SessionBus
    run_id: str | None
    call_id: str | None = None
    tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)
    chat: object | None = None  # the ChatSession, for tools


current: ContextVar[RunContext | None] = ContextVar("space_run_context", default=None)


def emit(type_: str, **data) -> None:
    """Publish an event in the current run's session; a no-op outside a run."""
    ctx = current.get()
    if ctx is not None:
        ctx.bus.publish(type_, data, run_id=ctx.run_id, call_id=ctx.call_id)


def sse(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"

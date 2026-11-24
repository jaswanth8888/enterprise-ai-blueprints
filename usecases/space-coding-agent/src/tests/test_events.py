# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
from collections import deque

from app import events


def test_publish_numbers_events_and_replays_after_seq():
    bus = events.SessionBus("s1")
    for n in range(5):
        bus.publish("x", {"n": n})
    assert [e["seq"] for e in bus.replay_after(0)] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in bus.replay_after(3)] == [4, 5]
    assert bus.replay_after(5) == []
    assert bus.last_seq == 5


def test_replay_is_none_once_events_were_dropped():
    bus = events.SessionBus("s1")
    bus.buffer = deque(maxlen=3)
    for n in range(6):
        bus.publish("x", {"n": n})
    assert bus.replay_after(0) is None  # seq 1-3 are gone
    assert bus.replay_after(2) is None
    assert [e["seq"] for e in bus.replay_after(3)] == [4, 5, 6]


def test_snapshot_event_is_not_buffered_and_carries_the_latest_seq():
    bus = events.SessionBus("s1")
    bus.publish("x", {})
    snap = bus.snapshot_event({"messages": []})
    assert snap["seq"] == 1 and snap["type"] == "session.snapshot"
    assert len(bus.buffer) == 1


def test_lagging_subscriber_is_dropped():
    async def scenario():
        bus = events.SessionBus("s1")
        queue = bus.subscribe()
        for n in range(events.SUBSCRIBER_QUEUE + 1):
            bus.publish("x", {"n": n})
        return bus.is_subscribed(queue)

    assert asyncio.run(scenario()) is False


def test_emit_uses_the_current_run_context():
    bus = events.SessionBus("s1")
    events.emit("ignored")  # no context: no-op
    token = events.current.set(events.RunContext(bus=bus, run_id="r_1", call_id="t_1"))
    try:
        events.emit("tool.start", name="run_python")
    finally:
        events.current.reset(token)
    (event,) = bus.buffer
    assert (event["run_id"], event["call_id"], event["data"]) == ("r_1", "t_1", {"name": "run_python"})
    assert events.sse(event).startswith("id: 1\ndata: ")
    assert json.loads(events.sse(event).split("data: ", 1)[1])["type"] == "tool.start"


def test_scrub_and_truncate(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b_secretvalue123")
    assert events.scrub("key=e2b_secretvalue123") == "key=<E2B_API_KEY>"
    assert events.truncate("abcdef", 3).startswith("abc… [3 more")

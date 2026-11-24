# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Chat sessions: MAF history, the event bus, the session sandbox and the active run."""

import asyncio
import contextlib
import logging
import time
import uuid

from .config import settings
from .events import RunContext, SessionBus, current
from .sandboxes import SandboxHandle, SandboxManager

log = logging.getLogger(__name__)


class ChatSession:
    def __init__(self, manager: SandboxManager, agent):
        self.id = uuid.uuid4().hex
        self.manager = manager
        self.agent = agent
        self.bus = SessionBus(self.id)
        self.maf_session = agent.create_session()
        self.lock = asyncio.Lock()
        self.sandbox: SandboxHandle | None = None
        self.run_task: asyncio.Task | None = None
        self.run_id: str | None = None
        self.last_activity = time.monotonic()
        self.messages: list[dict] = []  # transcript for the snapshot

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def publish(self, type_: str, **data) -> None:
        """For work outside a run (reaper, reset): no run or call id."""
        self.bus.publish(type_, data)

    @contextlib.contextmanager
    def _bound(self):
        """Send sandbox events to this session's bus, also outside a run (API calls, the reaper)."""
        ctx = current.get()
        if ctx is not None and ctx.bus is self.bus:
            yield
            return
        token = current.set(RunContext(bus=self.bus, run_id=None, chat=self))
        try:
            yield
        finally:
            current.reset(token)

    async def session_sandbox(self) -> SandboxHandle:
        """This chat's long-lived microVM, created on first use."""
        with self._bound():
            return await self._session_sandbox()

    async def _session_sandbox(self) -> SandboxHandle:
        async with self.lock:
            handle = self.sandbox
            if handle is not None and time.monotonic() - handle.last_used > 60:
                try:
                    alive = await handle.sandbox.is_running()
                except Exception:
                    alive = False
                if not alive:
                    self.manager.live.pop(handle.key, None)
                    self.publish("sandbox.killed", key=handle.key, sandbox_id=handle.sandbox_id, reason="expired",
                                 lifetime_ms=int((time.monotonic() - handle.created) * 1000))
                    self.sandbox = handle = None
            if handle is None:
                handle = await self.manager.create("session", "session", self.id, settings.session_sandbox_ttl_s)
                self.sandbox = handle
            else:
                try:
                    await handle.sandbox.set_timeout(settings.session_sandbox_ttl_s)
                except Exception as exc:
                    log.debug("set_timeout failed: %s", exc)
            handle.last_used = time.monotonic()
            return handle

    async def kill_session_sandbox(self, reason: str) -> None:
        async with self.lock:
            handle, self.sandbox = self.sandbox, None
        if handle is not None:
            with self._bound():
                await self.manager.kill(handle, reason)

    async def cancel_run(self) -> bool:
        task = self.run_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return True
        return False

    async def kill_fanout(self, reason: str) -> None:
        with self._bound():
            for handle in [h for h in self.manager.live.values() if h.session_id == self.id and h.kind == "fanout"]:
                await self.manager.kill(handle, reason)

    async def reset(self) -> None:
        await self.cancel_run()
        await self.kill_fanout("reset")
        await self.kill_session_sandbox("reset")
        self.maf_session = self.agent.create_session()
        self.messages.clear()
        self.publish("session.reset", reason="user")

    def snapshot(self) -> dict:
        return {
            "session_id": self.id,
            "last_seq": self.bus.last_seq,
            "messages": self.messages[-50:],
            "running": self.run_task is not None and not self.run_task.done(),
            "sandboxes": [{"key": h.key, "kind": h.kind, "label": h.label, "sandbox_id": h.sandbox_id, **h.info}
                          for h in self.manager.live.values() if h.session_id == self.id],
            "limits": {"fanout_concurrency": settings.fanout_concurrency, "fanout_max_tasks": settings.fanout_max_tasks,
                       "exec_timeout_s": settings.exec_timeout_s, "template": settings.template},
        }


class SessionRegistry:
    def __init__(self, manager: SandboxManager, agent):
        self.manager = manager
        self.agent = agent
        self.sessions: dict[str, ChatSession] = {}

    async def create(self) -> ChatSession | None:
        if len(self.sessions) >= settings.max_sessions:
            idle = [s for s in self.sessions.values() if not (s.run_task and not s.run_task.done())]
            if not idle:
                return None
            oldest = min(idle, key=lambda s: s.last_activity)
            await self.delete(oldest.id)
        chat = ChatSession(self.manager, self.agent)
        self.sessions[chat.id] = chat
        return chat

    def get(self, sid: str) -> ChatSession | None:
        chat = self.sessions.get(sid)
        if chat:
            chat.touch()
        return chat

    async def delete(self, sid: str) -> None:
        chat = self.sessions.pop(sid, None)
        if chat:
            await chat.cancel_run()
            await chat.kill_fanout("session_closed")
            await chat.kill_session_sandbox("session_closed")

    async def reap(self) -> None:
        now = time.monotonic()
        for chat in list(self.sessions.values()):
            busy = chat.run_task is not None and not chat.run_task.done()
            if busy:
                continue
            if now - chat.last_activity > settings.session_max_idle_s:
                await self.delete(chat.id)
            elif chat.sandbox and now - chat.sandbox.last_used > settings.session_sandbox_idle_s:
                await chat.kill_session_sandbox("idle")
        await self.manager.retry_orphans()

    async def shutdown(self) -> None:
        for chat in list(self.sessions.values()):
            await chat.cancel_run()
        await self.manager.shutdown()

# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""SPACE microVM sandboxes through the E2B SDK: a session sandbox and parallel fan-out.

Every lifecycle step emits an event, so the Sandbox Board shows each microVM
from the moment it is requested until it is killed.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from e2b import (
    AsyncSandbox,
    AuthenticationException,
    CommandExitException,
    RateLimitException,
    SandboxException,
    SandboxQuery,
    TimeoutException,
)

from .config import settings
from .events import emit, scrub, truncate

log = logging.getLogger(__name__)

WORKSPACE = "/home/user/workspace"
_RESULT_LINE = re.compile(r"^RESULT:\s*(\{.*\})\s*$", re.MULTILINE)
_CHUNK_BYTES = 4096
_CHUNK_MS = 100
_UI_BUDGET = 256 * 1024


class PlatformError(Exception):
    """A failure no retry will fix (auth, TLS, DNS): stop creating sandboxes."""


@dataclass
class SandboxHandle:
    key: str
    kind: str  # session | fanout
    label: str
    session_id: str
    sandbox: AsyncSandbox | None = None
    sandbox_id: str | None = None
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    info: dict = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    result_json: dict | None = None
    error: str | None = None


class _OutputPump:
    """Coalesces streamed output into bounded chunks for the UI, keeps a tail for the LLM."""

    def __init__(self, key: str, exec_id: str, stream: str, tail_bytes: int):
        self.key, self.exec_id, self.stream = key, exec_id, stream
        self.tail_bytes = tail_bytes
        self.text: list[str] = []
        self.size = 0
        self.pending = ""
        self.last_flush = time.monotonic()
        self.sent = 0

    def feed(self, chunk: str) -> None:
        self.text.append(chunk)
        self.size += len(chunk)
        if self.size > self.tail_bytes * 2:  # keep memory bounded
            joined = "".join(self.text)[-self.tail_bytes:]
            self.text, self.size = [joined], len(joined)
        self.pending += chunk
        if len(self.pending) >= _CHUNK_BYTES or (time.monotonic() - self.last_flush) * 1000 >= _CHUNK_MS:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        if self.sent < _UI_BUDGET:
            out = self.pending[: _UI_BUDGET - self.sent]
            self.sent += len(out)
            emit("sandbox.exec.output", key=self.key, exec_id=self.exec_id, stream=self.stream, chunk=scrub(out))
        self.pending = ""
        self.last_flush = time.monotonic()

    def tail(self) -> str:
        return "".join(self.text)[-self.tail_bytes:]

    @property
    def truncated(self) -> bool:
        return self.sent >= _UI_BUDGET


def _is_platform_error(exc: BaseException) -> bool:
    if isinstance(exc, AuthenticationException):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("certificate verify failed", "name or service not known", "nodename nor servname"))


def _fanout_stats(rows: list[dict], spans: dict[int, list], wall_ms: int) -> dict:
    """Boot and run phases apart: SPACE may boot VMs one after another, the code then runs side by side."""
    done = [s for s in spans.values() if s[1] is not None]
    run_wall_ms = int((max(s[1] for s in done) - min(s[0] for s in done)) * 1000) if done else 0
    sum_run_ms = sum(r.get("run_ms") or 0 for r in rows)
    boots = [r["create_ms"] for r in rows if r.get("create_ms") is not None]
    ok = sum(1 for r in rows if r.get("exit_code") == 0 and not r.get("error"))
    return {"ok": ok, "failed": len(rows) - ok, "wall_ms": wall_ms, "boot_max_ms": max(boots, default=None),
            "boot_min_ms": min(boots, default=None), "run_wall_ms": run_wall_ms, "sum_run_ms": sum_run_ms,
            "speedup": round(sum_run_ms / run_wall_ms, 2) if run_wall_ms else None}


class SandboxManager:
    def __init__(self, sandbox_factory=None):
        self._create = sandbox_factory or AsyncSandbox.create
        self.live: dict[str, SandboxHandle] = {}
        self.orphans: list[SandboxHandle] = []
        self.global_fanout = asyncio.Semaphore(settings.fanout_global_max)

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def create(self, kind: str, label: str, session_id: str, ttl_s: int, extra: dict | None = None) -> SandboxHandle:
        handle = SandboxHandle(key=f"{kind[0]}_{uuid.uuid4().hex[:10]}", kind=kind, label=label, session_id=session_id)
        emit("sandbox.creating", key=handle.key, kind=kind, label=label, template=settings.template, **(extra or {}))
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                async with asyncio.timeout(settings.create_timeout_s):
                    handle.sandbox = await self._create(
                        template=settings.template,
                        timeout=ttl_s,
                        metadata={"app": "space-coding-agent", "instance": settings.instance,
                                  "session": session_id, "kind": kind},
                        allow_internet_access=settings.sandbox_internet,
                        headers={"X-Client-ID": settings.client_id},
                    )
                break
            except (RateLimitException, TimeoutException, asyncio.TimeoutError, SandboxException) as exc:
                if _is_platform_error(exc):
                    emit("sandbox.error", key=handle.key, phase="create", error=scrub(str(exc)), retryable=False)
                    raise PlatformError(str(exc)) from exc
                if attempts >= 3:
                    emit("sandbox.error", key=handle.key, phase="create", error=scrub(str(exc)) or type(exc).__name__,
                         retryable=True)
                    raise
                await asyncio.sleep(0.5 * 2 ** (attempts - 1))
            except Exception as exc:
                emit("sandbox.error", key=handle.key, phase="create", error=scrub(str(exc)), retryable=False)
                if _is_platform_error(exc):
                    raise PlatformError(str(exc)) from exc
                raise
        handle.sandbox_id = handle.sandbox.sandbox_id
        self.live[handle.key] = handle  # registered before anything else can fail
        emit("sandbox.ready", key=handle.key, sandbox_id=handle.sandbox_id,
             create_ms=int((time.monotonic() - started) * 1000), attempts=attempts)
        await self._probe(handle)
        return handle

    async def _probe(self, handle: SandboxHandle) -> None:
        """Guest facts for the card: this is a microVM with its own kernel."""
        try:
            res = await handle.sandbox.commands.run(
                "printf '%s|%s|%s' \"$(uname -r)\" \"$(python3 -V 2>&1)\" \"$(nproc)\"", timeout=15)
            kernel, python, nproc = (res.stdout.strip().split("|") + ["", "", ""])[:3]
            handle.info = {"kernel": kernel, "python": python.replace("Python ", ""), "nproc": nproc}
            emit("sandbox.info", key=handle.key, **handle.info)
        except Exception as exc:  # the probe is cosmetic
            log.debug("probe failed for %s: %s", handle.key, exc)

    async def kill(self, handle: SandboxHandle, reason: str) -> None:
        self.live.pop(handle.key, None)
        if handle.sandbox is None:
            return
        lifetime = int((time.monotonic() - handle.created) * 1000)
        try:
            async with asyncio.timeout(10):
                await handle.sandbox.kill()
            emit("sandbox.killed", key=handle.key, sandbox_id=handle.sandbox_id, reason=reason, lifetime_ms=lifetime)
        except Exception as exc:
            self.orphans.append(handle)
            emit("sandbox.error", key=handle.key, phase="kill", error=scrub(str(exc)) or type(exc).__name__,
                 retryable=True)

    async def retry_orphans(self) -> None:
        pending, self.orphans = self.orphans, []
        for handle in pending:
            try:
                await handle.sandbox.kill()
            except Exception:
                self.orphans.append(handle)

    async def sweep(self) -> int:
        """Kill sandboxes this instance left behind (pod restart)."""
        killed = 0
        try:
            paginator = AsyncSandbox.list(
                query=SandboxQuery(metadata={"app": "space-coding-agent", "instance": settings.instance}),
                headers={"X-Client-ID": settings.client_id})
            while paginator.has_next:
                for info in await paginator.next_items():
                    if info.sandbox_id not in {h.sandbox_id for h in self.live.values()}:
                        await AsyncSandbox.kill(info.sandbox_id)
                        killed += 1
        except Exception as exc:
            log.warning("orphan sweep failed: %s", exc)
        return killed

    async def shutdown(self) -> None:
        handles = list(self.live.values())
        if handles:
            await asyncio.wait([asyncio.create_task(self.kill(h, "shutdown")) for h in handles], timeout=15)

    # ── execution ───────────────────────────────────────────────────────────
    async def exec(self, handle: SandboxHandle, cmd: str, kind: str, preview: str, timeout_s: int) -> ExecResult:
        handle.last_used = time.monotonic()
        exec_id = uuid.uuid4().hex[:8]
        emit("sandbox.exec.start", key=handle.key, exec_id=exec_id, kind=kind,
             preview=truncate(scrub(preview), 300), timeout_s=timeout_s)
        out = _OutputPump(handle.key, exec_id, "stdout", settings.output_tail_bytes)
        err = _OutputPump(handle.key, exec_id, "stderr", settings.output_tail_bytes)
        started = time.monotonic()
        exit_code, timed_out, error = None, False, None
        command = None
        try:
            command = await handle.sandbox.commands.run(
                cmd, background=True, cwd=WORKSPACE, timeout=timeout_s + 5,
                on_stdout=out.feed, on_stderr=err.feed)
            async with asyncio.timeout(timeout_s + 10):
                result = await command.wait()
            exit_code = result.exit_code
        except CommandExitException as exc:
            exit_code = exc.exit_code
        except (asyncio.TimeoutError, TimeoutException):
            timed_out = True
            if command is not None:
                await asyncio.shield(command.kill())
        except asyncio.CancelledError:
            if command is not None:
                await asyncio.shield(command.kill())
            raise
        except Exception as exc:
            error = scrub(str(exc)) or type(exc).__name__
        finally:
            out.flush()
            err.flush()
        stdout = out.tail()
        match = _RESULT_LINE.findall(stdout)
        result_json = None
        if match:
            try:
                result_json = json.loads(match[-1])
            except ValueError:
                pass
        res = ExecResult(exit_code=exit_code, stdout=stdout, stderr=err.tail(),
                         duration_ms=int((time.monotonic() - started) * 1000), timed_out=timed_out,
                         result_json=result_json, error=error)
        emit("sandbox.exec.end", key=handle.key, exec_id=exec_id, exit_code=exit_code, duration_ms=res.duration_ms,
             timed_out=timed_out, truncated=out.truncated or err.truncated, result_json=result_json, error=error)
        return res

    async def write(self, handle: SandboxHandle, path: str, content: str) -> None:
        handle.last_used = time.monotonic()
        await handle.sandbox.files.write(path, content)

    async def run_python(self, handle: SandboxHandle, code: str, timeout_s: int, name: str = "cell") -> ExecResult:
        path = f"{WORKSPACE}/.cells/{name}_{uuid.uuid4().hex[:6]}.py"
        await handle.sandbox.commands.run(f"mkdir -p {WORKSPACE}/.cells", timeout=15)
        await self.write(handle, path, code)
        return await self.exec(handle, f"python3 -u {path}", "python", code, timeout_s)

    # ── parallel fan-out ────────────────────────────────────────────────────
    async def fan_out(self, session_id: str, tasks: list[tuple[str, str]], timeout_s: int) -> tuple[list[dict], dict]:
        """One fresh microVM per task, concurrently; every VM is killed when its task ends."""
        fanout_id = uuid.uuid4().hex[:8]
        per_call = asyncio.Semaphore(settings.fanout_concurrency)
        broken = asyncio.Event()
        emit("fanout.start", fanout_id=fanout_id, n=len(tasks), concurrency=settings.fanout_concurrency)
        wall = time.monotonic()
        spans: dict[int, list] = {}  # index -> [run start, run end]

        async def one(index: int, label: str, code: str) -> dict:
            row = {"index": index, "label": label, "sandbox_id": None, "create_ms": None, "run_ms": None,
                   "exit_code": None, "result": None, "output": "", "error": None}
            handle = None
            emit("sandbox.queued", key=f"q_{fanout_id}_{index}", label=label, index=index, fanout_id=fanout_id)
            async with per_call, self.global_fanout:
                if broken.is_set():
                    row["error"] = "skipped: the platform rejected an earlier sandbox"
                    emit("sandbox.error", key=f"q_{fanout_id}_{index}", phase="create", error=row["error"],
                         retryable=False)
                    return row
                t0 = time.monotonic()
                reason = "fanout_done"
                try:
                    handle = await self.create("fanout", label, session_id, timeout_s + 120,
                                               extra={"index": index, "fanout_id": fanout_id,
                                                      "queued_key": f"q_{fanout_id}_{index}"})
                    row["sandbox_id"] = handle.sandbox_id
                    row["create_ms"] = int((time.monotonic() - t0) * 1000)
                    spans[index] = [time.monotonic(), None]
                    res = await self.run_python(handle, code, timeout_s, name=f"task{index}")
                    spans[index][1] = time.monotonic()
                    row.update(run_ms=res.duration_ms, exit_code=res.exit_code, result=res.result_json,
                               output=res.stdout[-2000:] + (("\n[stderr] " + res.stderr[-1000:]) if res.stderr else ""),
                               error=res.error or ("timed out" if res.timed_out else None))
                except asyncio.CancelledError:
                    reason = "cancelled"
                    raise
                except PlatformError as exc:
                    broken.set()
                    row["error"] = f"platform error: {exc}"
                except Exception as exc:
                    row["error"] = scrub(str(exc)) or type(exc).__name__
                finally:
                    if handle is not None:
                        await asyncio.shield(self.kill(handle, reason))
            return row

        rows = await asyncio.gather(*(one(i, label, code) for i, (label, code) in enumerate(tasks)),
                                    return_exceptions=True)
        results = [r if isinstance(r, dict) else {"index": i, "label": tasks[i][0], "error": str(r)}
                   for i, r in enumerate(rows)]
        stats = _fanout_stats(results, spans, int((time.monotonic() - wall) * 1000))
        emit("fanout.end", fanout_id=fanout_id, **stats)
        return results, stats

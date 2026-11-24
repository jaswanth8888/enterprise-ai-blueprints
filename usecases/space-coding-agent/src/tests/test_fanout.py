# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""SandboxManager against an in-memory fake of the E2B AsyncSandbox."""

import asyncio
import dataclasses
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from e2b import AuthenticationException

from app import events, sandboxes


class FakeCommand:
    def __init__(self, code: str, on_stdout):
        self.code, self.on_stdout, self.killed = code, on_stdout, False

    async def wait(self):
        if "SLEEP" in self.code:
            await asyncio.sleep(30)
        param = re.search(r"^PARAM = (.*)$", self.code, re.M)
        self.on_stdout(f"working\nRESULT: {json.dumps({'param': param.group(1) if param else None})}\n")
        return SimpleNamespace(exit_code=1 if "FAIL" in self.code else 0)

    async def kill(self):
        self.killed = True


class FakeSandbox:
    def __init__(self, platform):
        self.platform = platform
        self.sandbox_id = uuid.uuid4().hex
        self.files_written: dict[str, str] = {}
        self.files = SimpleNamespace(write=self._write)
        self.commands = SimpleNamespace(run=self._run)

    async def _write(self, path, content):
        self.files_written[path] = content

    async def _run(self, cmd, background=False, on_stdout=None, **_):
        if not background:  # the probe
            return SimpleNamespace(stdout="6.1.155+|Python 3.14.4|2")
        return FakeCommand(self.files_written[cmd.split()[-1]], on_stdout)

    async def kill(self):
        self.platform.killed.add(self.sandbox_id)


class FakePlatform:
    def __init__(self, fail_labels=(), auth_error=False, boot_s=0.01):
        self.created, self.killed = set(), set()
        self.fail_labels, self.auth_error, self.boot_s = fail_labels, auth_error, boot_s
        self.active = self.peak = 0

    async def create(self, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.boot_s)
            if self.auth_error:
                raise AuthenticationException("invalid API key")
            if kwargs["metadata"]["kind"] in self.fail_labels:
                raise RuntimeError("boom")
            sandbox = FakeSandbox(self)
            self.created.add(sandbox.sandbox_id)
            return sandbox
        finally:
            self.active -= 1


def run(coro, monkeypatch, **overrides):
    """Run coro inside a session context; returns (result, events)."""
    monkeypatch.setattr(sandboxes, "settings", dataclasses.replace(sandboxes.settings, **overrides))
    bus = events.SessionBus("s1")

    async def wrapper():
        token = events.current.set(events.RunContext(bus=bus, run_id="r_1", call_id="t_1"))
        try:
            return await coro()
        finally:
            events.current.reset(token)

    return asyncio.run(wrapper()), list(bus.buffer)


def by_key(evs):
    out: dict[str, list[str]] = {}
    for e in evs:
        if "key" in e["data"]:
            out.setdefault(e["data"]["key"], []).append(e["type"])
    return out


def test_fan_out_runs_every_task_in_its_own_vm_and_kills_it(monkeypatch):
    platform = FakePlatform()
    manager = sandboxes.SandboxManager(sandbox_factory=platform.create)
    tasks = [(str(n), f"PARAM = {n}\nprint(1)") for n in range(4)]
    (rows, stats), evs = run(lambda: manager.fan_out("s1", tasks, 30), monkeypatch)

    assert [r["result"] for r in rows] == [{"param": str(n)} for n in range(4)]
    assert len(platform.created) == 4 and platform.killed == platform.created
    assert manager.live == {}
    assert stats["ok"] == 4 and stats["failed"] == 0 and stats["run_wall_ms"] >= 0
    lifecycles = [v for k, v in by_key(evs).items() if k.startswith("f_")]
    assert len(lifecycles) == 4
    for types in lifecycles:
        assert types[:2] == ["sandbox.creating", "sandbox.ready"]
        assert types[-1] == "sandbox.killed" and "sandbox.exec.end" in types
    assert all(e["call_id"] == "t_1" for e in evs)  # cards link to the tool call


def test_concurrency_limit_queues_the_rest(monkeypatch):
    platform = FakePlatform(boot_s=0.05)
    manager = sandboxes.SandboxManager(sandbox_factory=platform.create)
    tasks = [(str(n), "print(1)") for n in range(6)]
    (rows, _), evs = run(lambda: manager.fan_out("s1", tasks, 30), monkeypatch, fanout_concurrency=2)
    assert platform.peak == 2
    assert sum(e["type"] == "sandbox.queued" for e in evs) == 6
    creating = [e["data"] for e in evs if e["type"] == "sandbox.creating"]
    assert {c["queued_key"] for c in creating} == {e["data"]["key"] for e in evs if e["type"] == "sandbox.queued"}


def test_one_failed_create_does_not_sink_the_others(monkeypatch):
    calls = {"n": 0}
    platform = FakePlatform()

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no capacity")
        return await platform.create(**kwargs)

    manager = sandboxes.SandboxManager(sandbox_factory=flaky)
    (rows, stats), _ = run(lambda: manager.fan_out("s1", [("a", "x"), ("b", "x"), ("c", "x")], 30), monkeypatch,
                           fanout_concurrency=1)
    assert [bool(r["error"]) for r in rows] == [True, False, False]
    assert stats["ok"] == 2 and platform.killed == platform.created


def test_platform_error_skips_the_remaining_tasks(monkeypatch):
    platform = FakePlatform(auth_error=True)
    manager = sandboxes.SandboxManager(sandbox_factory=platform.create)
    (rows, stats), _ = run(lambda: manager.fan_out("s1", [("a", "x"), ("b", "x"), ("c", "x")], 30), monkeypatch,
                           fanout_concurrency=1)
    assert rows[0]["error"].startswith("platform error")
    assert all(r["error"].startswith("skipped") for r in rows[1:])
    assert stats["ok"] == 0 and not platform.created


def test_failed_exit_code_is_a_result_not_an_error(monkeypatch):
    platform = FakePlatform()
    manager = sandboxes.SandboxManager(sandbox_factory=platform.create)
    (rows, stats), _ = run(lambda: manager.fan_out("s1", [("bad", "FAIL")], 30), monkeypatch)
    assert rows[0]["exit_code"] == 1 and rows[0]["error"] is None
    assert stats["failed"] == 1


def test_cancel_kills_every_vm(monkeypatch):
    platform = FakePlatform()
    manager = sandboxes.SandboxManager(sandbox_factory=platform.create)

    async def cancel_midway():
        task = asyncio.create_task(manager.fan_out("s1", [(str(n), "SLEEP") for n in range(3)], 60))
        while len(platform.created) < 3:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _, evs = run(cancel_midway, monkeypatch)
    assert len(platform.created) == 3 and platform.killed == platform.created
    assert manager.live == {}
    assert [e["data"]["reason"] for e in evs if e["type"] == "sandbox.killed"] == ["cancelled"] * 3


def test_sweep_params_become_python_literals():
    from app.tools import _param_literal

    assert [_param_literal(v) for v in ("4", "0.5", "quicksort", '{"n": 1}', "1e6")] == \
        ["4", "0.5", "'quicksort'", "{'n': 1}", "1000000.0"]


def test_array_arguments_sent_as_json_strings_are_accepted():
    from app.tools import _json_list, _str_list

    assert _json_list('[{"label": "a", "code": "x"}]') == [{"label": "a", "code": "x"}]
    assert _json_list([1]) == [1] and _json_list("not json") == "not json"
    # JSON with a Python escape, and raw newlines inside a string
    assert _json_list(r'[{"label": "a", "code": "print(f\'x\')"}]') == [{"label": "a", "code": "print(f'x')"}]
    assert _json_list('[{"label": "a", "code": "a = 1\nprint(a)"}]')[0]["code"] == "a = 1\nprint(a)"
    assert _json_list("[{'label': 'a', 'code': 'x'}]") == [{"label": "a", "code": "x"}]
    assert _str_list("[1, 2.5, \"x\"]") == ["1", "2.5", "x"]


def test_parallel_scripts_split_on_task_headers():
    from app.tools import _split_tasks

    scripts = "# task: quicksort\nprint(1)\n\n#task:  heap sort \n```python\nprint(2)\n```\n# task: empty\n"
    assert _split_tasks(scripts) == [("quicksort", "print(1)"), ("heap sort", "print(2)")]
    assert _split_tasks("print(1)") == []


def test_code_above_the_first_task_is_shared():
    from app.tools import _split_tasks

    scripts = "```python\nimport time\ndef f(): return 1\n\n# task: a\nprint(f())\n# task: b\nprint(f() + 1)\n```"
    assert _split_tasks(scripts) == [("a", "import time\ndef f(): return 1\nprint(f())"),
                                     ("b", "import time\ndef f(): return 1\nprint(f() + 1)")]

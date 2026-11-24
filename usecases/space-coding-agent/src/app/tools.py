# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""The agent's tools. All code runs inside SPACE microVMs, never in this pod.

@traced publishes tool.start/tool.end for the timeline and enforces the per-run
tool budget, so the side pane does not depend on MAF internals.
"""

import ast
import asyncio
import functools
import json
import posixpath
import re
import shlex
import time
import uuid
from typing import Annotated

from agent_framework import tool
from pydantic import BeforeValidator, Field

from .config import settings
from .events import current, emit, scrub, truncate
from .sandboxes import WORKSPACE, ExecResult

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_fences(code: str) -> str:
    match = _FENCE.match(code)
    return match.group(1) if match else code


def _json_list(value):
    """Smaller models often send an array argument as a JSON string, sometimes with
    Python escapes such as \\' in it; accept those too."""
    if not isinstance(value, str):
        return value
    for text in (value, value.replace("\\'", "'")):
        try:
            return json.loads(text, strict=False)
        except ValueError:
            pass
    try:
        return ast.literal_eval(value)  # literals only, never code
    except (ValueError, SyntaxError):
        return value


def _str_list(value):
    value = _json_list(value)
    if isinstance(value, list):
        return [v if isinstance(v, str) else json.dumps(v) for v in value]
    return value


def _args_preview(kwargs: dict) -> dict:
    preview = {}
    for name, value in kwargs.items():
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, list):
            value = [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        preview[name] = truncate(scrub(text), 4000)
    return preview


def traced(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ctx = current.get()
        call_id = f"t_{uuid.uuid4().hex[:8]}"
        if ctx is not None:
            ctx.tool_calls += 1
            ctx.call_id = call_id
            if ctx.tool_calls > settings.max_tool_calls_per_run:
                emit("tool.start", name=fn.__name__, args={})
                emit("tool.end", name=fn.__name__, status="error", duration_ms=0,
                     result_preview="tool budget exhausted", result_chars=0)
                ctx.call_id = None
                return "Tool budget for this request is exhausted. Answer with what you have now."
        emit("tool.start", name=fn.__name__, args=_args_preview(kwargs))
        started = time.monotonic()
        status = "ok"
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:
            emit("tool.end", name=fn.__name__, status="cancelled", duration_ms=int((time.monotonic() - started) * 1000),
                 result_preview="cancelled", result_chars=0)
            raise
        except Exception as exc:  # report to the model instead of failing the run
            status = "error"
            result = f"Error: {scrub(str(exc)) or type(exc).__name__}"
        duration = int((time.monotonic() - started) * 1000)
        emit("tool.end", name=fn.__name__, status=status, duration_ms=duration,
             result_preview=truncate(scrub(result), 4000), result_chars=len(result))
        if ctx is not None:
            ctx.call_id = None
        return result

    return wrapper


def _session():
    """The ChatSession of the current run (set by the API layer)."""
    ctx = current.get()
    return getattr(ctx, "chat", None) if ctx else None


def _format(res: ExecResult) -> str:
    parts = []
    if res.stdout:
        parts.append(truncate(res.stdout, settings.llm_result_max_chars))
    if res.stderr:
        parts.append("stderr:\n" + truncate(res.stderr, settings.llm_result_max_chars // 2))
    if res.timed_out:
        parts.append(f"(timed out after {res.duration_ms} ms)")
    elif res.error:
        parts.append(f"(error: {res.error})")
    else:
        parts.append(f"(exit code {res.exit_code}, {res.duration_ms} ms)")
    return "\n".join(parts)


def _workspace_path(path: str) -> str:
    full = posixpath.normpath(posixpath.join(WORKSPACE, path))
    if full != WORKSPACE and not full.startswith(WORKSPACE + "/"):
        raise ValueError(f"path must stay under {WORKSPACE}")
    return full


def _timeout(value: int | None) -> int:
    return max(5, min(settings.exec_timeout_max_s, value or settings.exec_timeout_s))


@tool(approval_mode="never_require")
@traced
async def run_python(
    code: Annotated[str, Field(description="Python 3 code (standard library only) to run in the session sandbox")],
    timeout_s: Annotated[int, Field(description="Timeout in seconds")] = 120,
) -> str:
    """Run Python in this chat's session sandbox (a SPACE microVM). Files written there persist across calls."""
    code = _strip_fences(code)[: settings.max_code_chars]
    chat = _session()
    handle = await chat.session_sandbox()
    return _format(await chat.manager.run_python(handle, code, _timeout(timeout_s)))


@tool(approval_mode="never_require")
@traced
async def run_shell(
    command: Annotated[str, Field(description="Shell command to run in the session sandbox")],
    timeout_s: Annotated[int, Field(description="Timeout in seconds")] = 120,
) -> str:
    """Run a shell command in this chat's session sandbox (inside the microVM)."""
    chat = _session()
    handle = await chat.session_sandbox()
    command = command[: settings.max_code_chars]
    return _format(await chat.manager.exec(handle, command, "shell", command, _timeout(timeout_s)))


@tool(approval_mode="never_require")
@traced
async def write_file(
    path: Annotated[str, Field(description="Path relative to the workspace")],
    content: Annotated[str, Field(description="File content")],
) -> str:
    """Write a file in the session sandbox's workspace."""
    chat = _session()
    handle = await chat.session_sandbox()
    full = _workspace_path(path)
    await handle.sandbox.commands.run(f"mkdir -p {shlex.quote(posixpath.dirname(full))}", timeout=15)
    await chat.manager.write(handle, full, content[: settings.max_code_chars])
    return f"Wrote {len(content)} chars to {full}"


@tool(approval_mode="never_require")
@traced
async def read_file(path: Annotated[str, Field(description="Path relative to the workspace")]) -> str:
    """Read a file from the session sandbox's workspace."""
    chat = _session()
    handle = await chat.session_sandbox()
    text = await handle.sandbox.files.read(_workspace_path(path))
    return truncate(text, settings.llm_result_max_chars)


@tool(approval_mode="never_require")
@traced
async def list_files(path: Annotated[str, Field(description="Directory relative to the workspace")] = ".") -> str:
    """List files in the session sandbox's workspace."""
    chat = _session()
    handle = await chat.session_sandbox()
    entries = await handle.sandbox.files.list(_workspace_path(path))
    return "\n".join(f"{'dir ' if str(e.type).endswith('DIR') else 'file'} {e.name}" for e in entries) or "(empty)"


_TASK_HEADER = re.compile(r"^#\s*task:\s*(.+?)\s*$", re.MULTILINE)
_FENCE_LINE = re.compile(r"^\s*```[\w+-]*\s*$", re.MULTILINE)


def _split_tasks(scripts: str) -> list[tuple[str, str]]:
    """Scripts in one string, each starting with a '# task: <label>' line; code above
    the first one (imports, helpers) is shared by all. One string, not an array of
    objects: small models garble code nested two JSON levels deep."""
    scripts = _FENCE_LINE.sub("", scripts)
    heads = list(_TASK_HEADER.finditer(scripts))
    shared = scripts[: heads[0].start()].strip("\n") + "\n" if heads else ""
    pairs = []
    for n, head in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(scripts)
        code = scripts[head.end():end].strip("\n")
        if code.strip():
            pairs.append((head.group(1)[:40], (shared + code).lstrip("\n")[: settings.max_code_chars]))
    return pairs


@tool(approval_mode="never_require")
@traced
async def run_parallel(
    scripts: Annotated[str, Field(description="Python 3 scripts (standard library only), each starting with its "
                                              "own line '# task: <label>' and printing one 'RESULT: {json}' line. "
                                              "Code above the first '# task:' line is shared by every task")],
    timeout_s: Annotated[int, Field(description="Per-task timeout in seconds")] = 300,
) -> str:
    """Run different Python scripts in parallel, each in its own fresh SPACE microVM, then kill the microVMs.
    Use it when the variants cannot share one script; otherwise use run_sweep."""
    pairs = _split_tasks(scripts)[: settings.fanout_max_tasks]
    if not pairs:
        return "No tasks found: start each script with its own line '# task: <label>'."
    return await _fan_out(_session(), pairs, timeout_s)


@tool(approval_mode="never_require")
@traced
async def run_sweep(
    code: Annotated[str, Field(description="One Python 3 script (standard library only). It reads the variable "
                                           "PARAM, already defined (a number if the value is numeric, else a "
                                           "string), and prints one 'RESULT: {json}' line")],
    params: Annotated[list[str], BeforeValidator(_str_list),
                      Field(description="One PARAM value per microVM, e.g. seeds, sizes or names")],
    timeout_s: Annotated[int, Field(description="Per-task timeout in seconds")] = 300,
) -> str:
    """Run the same script in parallel, one fresh SPACE microVM per PARAM value, then kill the microVMs.
    Prefer it for parameter sweeps, seeds and benchmarks that can share one script."""
    if not params:
        return "No params given."
    code = _strip_fences(code)[: settings.max_code_chars]
    pairs = [(str(p)[:40], f"PARAM = {_param_literal(str(p))}\n{code}") for p in params[: settings.fanout_max_tasks]]
    return await _fan_out(_session(), pairs, timeout_s)


def _param_literal(value: str) -> str:
    """'4' becomes 4 and '0.5' becomes 0.5, so PARAM * 20 does what the model means; names stay strings."""
    try:
        return repr(json.loads(value))
    except ValueError:
        return repr(value)


async def _fan_out(chat, pairs: list[tuple[str, str]], timeout_s: int) -> str:
    rows, st = await chat.manager.fan_out(chat.id, pairs, _timeout(timeout_s))
    lines = [f"{len(rows)} microVMs ({st['ok']} ok): wall {st['wall_ms']} ms; boot {st['boot_min_ms']}-{st['boot_max_ms']} ms; "
             f"the code ran side by side in {st['run_wall_ms']} ms against {st['sum_run_ms']} ms one after another"
             + (f" ({st['speedup']}x)" if st["speedup"] else "")]
    budget = max(400, settings.llm_result_max_chars // max(1, len(rows)))
    for r in rows:
        status = r.get("error") or f"exit {r.get('exit_code')}"
        lines.append(f"\n## {r['label']} ({status}; create {r.get('create_ms')} ms, run {r.get('run_ms')} ms)")
        if r.get("result") is not None:
            lines.append("RESULT: " + json.dumps(r["result"]))
        elif r.get("output"):
            lines.append(truncate(r["output"], budget))
    return "\n".join(lines)


@tool(approval_mode="never_require")
@traced
async def sandbox_status() -> str:
    """Describe this chat's live sandboxes."""
    chat = _session()
    mine = [h for h in chat.manager.live.values() if h.session_id == chat.id]
    if not mine:
        return "No live sandboxes for this chat."
    now = time.monotonic()
    return "\n".join(f"{h.kind} {h.sandbox_id} up {int(now - h.created)} s kernel {h.info.get('kernel')}" for h in mine)


@tool(approval_mode="never_require")
@traced
async def reset_sandbox() -> str:
    """Kill this chat's session sandbox; the next call starts a fresh one."""
    chat = _session()
    await chat.kill_session_sandbox("reset")
    return "Session sandbox killed; the next call starts a fresh microVM."


TOOLS = [run_python, run_shell, write_file, read_file, list_files, run_sweep, run_parallel, sandbox_status,
         reset_sandbox]

# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""The Microsoft Agent Framework agent and one streamed turn."""

import asyncio
import time
import uuid

from agent_framework.openai import OpenAIChatCompletionClient

from .config import settings
from .events import RunContext, current, emit, scrub, truncate
from .tools import TOOLS

INSTRUCTIONS = f"""You are a coding agent. You run code in SPACE sandboxes: Firecracker microVMs, each
with its own Linux kernel, isolated from this cluster. They run Ubuntu with Python 3.14 and only the
standard library (no pip, no package installs), and cannot reach cluster services.

Tools:
- run_python, run_shell, write_file, read_file, list_files use this chat's session sandbox. Workspace
  files persist between calls; Python variables do not.
- run_sweep runs ONE script in N fresh microVMs at the same time, with a different PARAM string in
  each (seeds, sizes, algorithm names; numeric values arrive as numbers). Prefer it for parallel work.
- run_parallel runs DIFFERENT scripts, one per fresh microVM, for variants that cannot share a
  script. Pass them as one string; each script starts with its own line `# task: <label>`, and
  code above the first `# task:` line (imports, helpers) is shared by all of them.
- Parallel tools take up to {settings.fanout_max_tasks} tasks and kill their microVMs afterwards.

Rules:
- Call a tool right away. Never put code in your reply; code goes only in tool arguments.
- Do not guess results; run code.
- Every parallel task makes its own data with a fixed seed and prints exactly one line
  `RESULT: {{json}}` with its key numbers.
- After a parallel run, reply with a short markdown table and one sentence on the result.
- Keep replies short.
"""


def build_agent():
    client = OpenAIChatCompletionClient(base_url=settings.openai_base_url, api_key=settings.openai_api_key or "none",
                                        model=settings.model_name)
    # Tell the model why a call was rejected (e.g. bad arguments) so it can fix it.
    client.function_invocation_configuration["include_detailed_errors"] = True
    return client.as_agent(name="space-coding-agent", instructions=INSTRUCTIONS, tools=TOOLS)


def _rejected_call(ctx: RunContext, name: str, message: str) -> None:
    """MAF refused the call before the tool ran (bad arguments, unknown tool): show it on the timeline too."""
    ctx.call_id = f"t_{uuid.uuid4().hex[:8]}"
    emit("tool.start", name=name, args={})
    emit("tool.end", name=name, status="error", duration_ms=0, result_preview=truncate(scrub(message), 4000),
         result_chars=len(message))
    ctx.call_id = None


async def stream_turn(chat, text: str) -> None:
    """Run one user turn, publishing message deltas; tool events come from the tools."""
    run_id = f"r_{uuid.uuid4().hex[:10]}"
    chat.run_id = run_id
    ctx = RunContext(bus=chat.bus, run_id=run_id, chat=chat)
    token = current.set(ctx)
    started = time.monotonic()
    status, error, usage = "ok", None, {}
    chat.messages.append({"role": "user", "text": text})
    emit("run.start", model=settings.model_name, user_text=text)
    answer: list[str] = []
    segment = {"id": None, "after_tool": False}
    call_names: dict[str, str] = {}

    def delta(piece: str) -> None:
        # Text after a tool call starts a new bubble, so the chat reads in order with the timeline.
        if segment["id"] is None or segment["after_tool"]:
            if segment["id"]:
                emit("message.end", message_id=segment["id"])
            segment["id"], segment["after_tool"] = f"m_{uuid.uuid4().hex[:8]}", False
        answer.append(piece)
        emit("message.delta", message_id=segment["id"], text=piece)

    try:
        async with asyncio.timeout(settings.run_timeout_s):
            stream = chat.agent.run(text, session=chat.maf_session, stream=True)
            tools_seen = ctx.tool_calls
            async for update in stream:
                if ctx.tool_calls != tools_seen:  # a tool ran: start a new message bubble
                    tools_seen = ctx.tool_calls
                    segment["after_tool"] = True
                if update.text:
                    delta(update.text)
                for content in update.contents or []:
                    if content.type == "function_call" and content.call_id and content.name:
                        call_names[content.call_id] = content.name
                    elif content.type == "function_result" and content.exception:
                        _rejected_call(ctx, call_names.get(content.call_id, "tool"), str(content.result or ""))
                        segment["after_tool"] = True
                    elif content.type == "usage":
                        details = getattr(content, "usage_details", None) or {}
                        for key in ("input_token_count", "output_token_count"):
                            value = details.get(key) if isinstance(details, dict) else getattr(details, key, None)
                            if value:
                                usage[key] = usage.get(key, 0) + value
            await stream.get_final_response()
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except TimeoutError:
        status, error = "timeout", f"the request ran longer than {settings.run_timeout_s} s"
    except Exception as exc:
        status, error = "error", scrub(str(exc)) or type(exc).__name__
    finally:
        if segment["id"]:
            emit("message.end", message_id=segment["id"])
        reply = "".join(answer)
        if reply:
            chat.messages.append({"role": "assistant", "text": reply})
        emit("run.end", status=status, duration_ms=int((time.monotonic() - started) * 1000), usage=usage,
             error=error)
        current.reset(token)
        chat.run_id = None

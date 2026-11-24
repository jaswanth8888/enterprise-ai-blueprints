# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Settings from the environment, with every limit clamped to a sane range."""

import os
from dataclasses import dataclass


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # LLM (LiteLLM, OpenAI-compatible)
    openai_base_url: str
    openai_api_key: str
    model_name: str
    # SPACE through its E2B-compatible API
    e2b_api_key: str
    e2b_domain: str
    template: str
    client_id: str
    instance: str
    sandbox_internet: bool
    # App
    port: int
    app_auth_token: str
    # Limits
    max_sessions: int
    session_max_idle_s: int
    session_sandbox_ttl_s: int
    session_sandbox_idle_s: int
    fanout_concurrency: int
    fanout_max_tasks: int
    fanout_global_max: int
    create_timeout_s: int
    exec_timeout_s: int
    exec_timeout_max_s: int
    run_timeout_s: int
    max_tool_calls_per_run: int
    max_message_chars: int
    max_code_chars: int
    output_tail_bytes: int
    llm_result_max_chars: int


def load() -> Settings:
    return Settings(
        openai_base_url=os.getenv("OPENAI_BASE_URL", "http://litellm.litellm.svc.cluster.local:4000/v1"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        model_name=os.getenv("MODEL_NAME", "qwen3-coder-30b"),
        e2b_api_key=os.getenv("E2B_API_KEY", ""),
        e2b_domain=os.getenv("E2B_DOMAIN", ""),
        template=os.getenv("SPACE_TEMPLATE", "seed"),
        client_id=os.getenv("SPACE_CLIENT_ID", "space-coding-agent"),
        instance=os.getenv("SPACE_INSTANCE", "space-coding-agent"),
        sandbox_internet=_bool("SANDBOX_INTERNET", True),
        port=_int("PORT", 8090, 1, 65535),
        app_auth_token=os.getenv("APP_AUTH_TOKEN", ""),
        max_sessions=_int("MAX_SESSIONS", 10, 1, 100),
        session_max_idle_s=_int("SESSION_MAX_IDLE_S", 3600, 60, 86400),
        session_sandbox_ttl_s=_int("SESSION_SANDBOX_TTL_S", 900, 60, 21600),
        session_sandbox_idle_s=_int("SESSION_SANDBOX_IDLE_S", 600, 30, 21600),
        fanout_concurrency=_int("FANOUT_CONCURRENCY", 8, 1, 32),
        fanout_max_tasks=_int("FANOUT_MAX_TASKS", 16, 1, 64),
        fanout_global_max=_int("FANOUT_GLOBAL_MAX", 16, 1, 64),
        create_timeout_s=_int("CREATE_TIMEOUT_S", 120, 5, 600),
        exec_timeout_s=_int("EXEC_TIMEOUT_S", 120, 5, 600),
        exec_timeout_max_s=_int("EXEC_TIMEOUT_MAX_S", 600, 5, 3600),
        run_timeout_s=_int("RUN_TIMEOUT_S", 900, 30, 7200),
        max_tool_calls_per_run=_int("MAX_TOOL_CALLS_PER_RUN", 30, 1, 200),
        max_message_chars=_int("MAX_MESSAGE_CHARS", 16000, 100, 200000),
        max_code_chars=_int("MAX_CODE_CHARS", 100000, 100, 1000000),
        output_tail_bytes=_int("OUTPUT_TAIL_BYTES", 65536, 1024, 1048576),
        llm_result_max_chars=_int("LLM_RESULT_MAX_CHARS", 12000, 1000, 100000),
    )


settings = load()

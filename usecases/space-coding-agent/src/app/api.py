# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""HTTP API, SSE event stream and the static UI."""

import asyncio
import contextlib
import logging
import os
import platform
import re
import time

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, net
from .agent import build_agent, stream_turn
from .config import settings
from .events import sse
from .sandboxes import SandboxManager
from .sessions import SessionRegistry

log = logging.getLogger(__name__)
_SID = re.compile(r"^[0-9a-f]{32}$")
FRONTEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

state: dict = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    net.warn_if_uvloop()
    manager = SandboxManager()
    registry = SessionRegistry(manager, build_agent())
    state.update(manager=manager, registry=registry, status=None, status_at=0.0)
    swept = await manager.sweep()
    if swept:
        log.info("killed %d sandboxes left by a previous pod", swept)

    async def reaper():
        while True:
            await asyncio.sleep(30)
            try:
                await registry.reap()
            except Exception as exc:
                log.warning("reaper: %s", exc)

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        await registry.shutdown()


app = FastAPI(title="space-coding-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


_SECURITY_HEADERS = [
    (b"content-security-policy", b"default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; "
                                 b"img-src 'self' data:; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]
_REVALIDATE = (b"cache-control", b"no-cache")


class SecurityHeaders:
    """Pure ASGI, so streaming (SSE) responses pass through untouched."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        # UI files revalidate on every load (ETag), so a redeploy shows up without a hard refresh.
        extra = _SECURITY_HEADERS if scope["path"].startswith("/api/") else [*_SECURITY_HEADERS, _REVALIDATE]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


def _chat(sid: str):
    if not _SID.match(sid):
        raise HTTPException(404, "no such session")
    chat = state["registry"].get(sid)
    if chat is None:
        raise HTTPException(404, "no such session")
    return chat


class Login(BaseModel):
    token: str


class Message(BaseModel):
    text: str


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    client = request.client.host if request.client else "?"
    if auth.throttled(client):
        raise HTTPException(429, "too many attempts")
    if not auth.token_ok(body.token.strip()):
        auth.record_failure(client)
        raise HTTPException(401, "wrong token")
    response.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.COOKIE_TTL_S, httponly=True, secure=True,
                        samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(auth.require_auth)])
async def status():
    now = time.monotonic()
    if state["status"] is None or now - state["status_at"] > 10:
        llm = space = False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(settings.openai_base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {settings.openai_api_key}"})
                llm = r.status_code == 200
        except Exception as exc:
            log.debug("LLM check failed: %s", exc)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"https://api.{settings.e2b_domain}/sandboxes",
                                     headers={"X-API-Key": settings.e2b_api_key, "X-Client-ID": settings.client_id})
                space = r.status_code == 200
        except Exception as exc:
            log.debug("SPACE check failed: %s", exc)
        state.update(status={"llm": llm, "space": space}, status_at=now)
    manager: SandboxManager = state["manager"]
    return {**state["status"], "live_sandboxes": len(manager.live), "pod_kernel": platform.release(),
            "model": settings.model_name, "e2b_domain": settings.e2b_domain, "template": settings.template,
            "fanout_concurrency": settings.fanout_concurrency}


@app.post("/api/sessions", dependencies=[Depends(auth.require_auth)])
async def create_session():
    chat = await state["registry"].create()
    if chat is None:
        raise HTTPException(429, "too many active sessions")
    return chat.snapshot()


@app.get("/api/sessions/{sid}", dependencies=[Depends(auth.require_auth)])
async def get_session(sid: str):
    return _chat(sid).snapshot()


@app.post("/api/sessions/{sid}/messages", dependencies=[Depends(auth.require_auth)], status_code=202)
async def post_message(sid: str, body: Message):
    chat = _chat(sid)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    if len(text) > settings.max_message_chars:
        raise HTTPException(413, "message too long")
    if chat.run_task is not None and not chat.run_task.done():
        raise HTTPException(409, "a request is already running")
    chat.run_task = asyncio.create_task(stream_turn(chat, text))
    return {"accepted": True}


@app.post("/api/sessions/{sid}/cancel", dependencies=[Depends(auth.require_auth)])
async def cancel(sid: str):
    chat = _chat(sid)
    cancelled = await chat.cancel_run()
    await chat.kill_fanout("cancelled")
    return {"cancelled": cancelled}


@app.post("/api/sessions/{sid}/reset", dependencies=[Depends(auth.require_auth)])
async def reset(sid: str):
    chat = _chat(sid)
    await chat.reset()
    return chat.snapshot()


@app.delete("/api/sessions/{sid}", dependencies=[Depends(auth.require_auth)])
async def delete_session(sid: str):
    _chat(sid)
    await state["registry"].delete(sid)
    return {"deleted": True}


@app.post("/api/sessions/{sid}/sandbox", dependencies=[Depends(auth.require_auth)])
async def warm_sandbox(sid: str):
    handle = await _chat(sid).session_sandbox()
    return {"sandbox_id": handle.sandbox_id}


@app.delete("/api/sessions/{sid}/sandbox", dependencies=[Depends(auth.require_auth)])
async def kill_sandbox(sid: str):
    await _chat(sid).kill_session_sandbox("user")
    return {"killed": True}


@app.get("/api/sessions/{sid}/events", dependencies=[Depends(auth.require_auth)])
async def events(sid: str, request: Request):
    chat = _chat(sid)
    # The browser resends Last-Event-ID on its own retries; ?after= is for a reopened EventSource.
    try:
        last = max(0, int(request.headers.get("last-event-id") or request.query_params.get("after") or 0))
    except ValueError:
        last = 0

    async def stream():
        queue = chat.bus.subscribe()
        try:
            backlog = chat.bus.replay_after(last)
            if backlog is None:  # too old to replay: start over from a snapshot
                backlog = [chat.bus.snapshot_event(chat.snapshot())]
            for event in backlog:
                yield sse(event)
            seen = backlog[-1]["seq"] if backlog else last
            while chat.bus.is_subscribed(queue):
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event["seq"] > seen:
                    seen = event["seq"]
                    yield sse(event)
        finally:
            chat.bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="ui")

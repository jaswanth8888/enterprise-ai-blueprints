#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Send one prompt to space-coding-agent and print the live event timeline.

Standard library only. Examples:
  APP_AUTH_TOKEN=... ./chat_client.py --url https://space-coding-agent.solutions.ai --cacert ca.crt "Estimate pi in 8 parallel sandboxes"
  ./chat_client.py --url http://localhost:8090 --token dev "Run uname -a in your sandbox"
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request


def request(opener, method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with opener.open(req, timeout=30) as resp:  # nosec B310 - the URL scheme is checked in main()
        return json.loads(resp.read() or b"{}")


def describe(ev, started):
    d, t = ev["data"], ev["type"]
    stamp = f"{time.monotonic() - started:6.1f}s"
    if t == "tool.start":
        return f"{stamp}  ▶ {d['name']}"
    if t == "tool.end":
        return f"{stamp}  ◀ {d['name']} {d['status']} in {d['duration_ms']} ms"
    if t == "fanout.start":
        return f"{stamp}  ⇉ parallel run: {d['n']} microVMs, up to {d['concurrency']} at once"
    if t == "fanout.end":
        return (f"{stamp}  ⇇ parallel run done: {d['ok']} ok, {d['failed']} failed, wall {d['wall_ms']} ms, "
                f"boot {d['boot_min_ms']}-{d['boot_max_ms']} ms, run {d['run_wall_ms']} ms side by side vs "
                f"{d['sum_run_ms']} ms in turn ({d['speedup']}x)")
    if t == "sandbox.creating":
        return f"{stamp}    ◌ {d['label']} creating ({d['key']})"
    if t == "sandbox.ready":
        return f"{stamp}    ● {d['key']} ready: {d['sandbox_id']} in {d['create_ms']} ms"
    if t == "sandbox.info":
        return f"{stamp}      {d['key']} kernel {d.get('kernel')} python {d.get('python')} vcpu {d.get('nproc')}"
    if t == "sandbox.exec.end":
        result = f" RESULT {json.dumps(d['result_json'])}" if d.get("result_json") else ""
        return f"{stamp}      {d['key']} exit {d['exit_code']} in {d['duration_ms']} ms{result}"
    if t == "sandbox.killed":
        return f"{stamp}    ✕ {d['key']} killed ({d['reason']})"
    if t == "sandbox.error":
        return f"{stamp}    ! {d['key']} {d['phase']} error: {d['error']}"
    if t == "run.end":
        return f"{stamp}  run {d['status']} in {d['duration_ms']} ms {d.get('error') or ''}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt")
    parser.add_argument("--url", default=os.getenv("SCA_URL", "http://localhost:8090"))
    parser.add_argument("--token", default=os.getenv("APP_AUTH_TOKEN"))
    parser.add_argument("--cacert", help="CA bundle for the gateway certificate")
    parser.add_argument("--session", help="reuse a session id (keeps the chat and its sandbox)")
    parser.add_argument("--quiet", action="store_true", help="print only the answer")
    args = parser.parse_args()
    if not args.token:
        sys.exit("set APP_AUTH_TOKEN or pass --token")
    if not args.url.startswith(("http://", "https://")):
        sys.exit("--url must be http(s)")
    base = args.url.rstrip("/")
    context = ssl.create_default_context(cafile=args.cacert) if args.cacert else None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    if args.session:
        snap = request(opener, "GET", f"{base}/api/sessions/{args.session}", args.token)
    else:
        snap = request(opener, "POST", f"{base}/api/sessions", args.token)
    sid, seen = snap["session_id"], snap["last_seq"]
    print(f"session {sid}", file=sys.stderr)
    events = urllib.request.Request(f"{base}/api/sessions/{sid}/events?after={seen}",
                                    headers={"Authorization": f"Bearer {args.token}"})
    stream = opener.open(events, timeout=1800)  # nosec B310 - the URL scheme is checked above
    request(opener, "POST", f"{base}/api/sessions/{sid}/messages", args.token, {"text": args.prompt})
    started, answer = time.monotonic(), []
    for raw in stream:
        line = raw.decode().rstrip("\n")
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev["seq"] <= seen:
            continue
        seen = ev["seq"]
        if ev["type"] == "message.delta":
            answer.append(ev["data"]["text"])
        elif not args.quiet:
            text = describe(ev, started)
            if text:
                print(text, file=sys.stderr, flush=True)
        if ev["type"] == "run.end":
            break
    print("".join(answer))


if __name__ == "__main__":
    main()

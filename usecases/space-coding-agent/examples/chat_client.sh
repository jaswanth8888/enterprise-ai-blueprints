#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/chat_client.sh
# curl-based walk through the SPACE Coding Agent API: status, a session, one
# prompt, and its live event stream (SSE).
#
# Usage: APP_AUTH_TOKEN=... ./chat_client.sh [BASE_URL] ["prompt"]
#   BASE_URL default: http://localhost:8090
#   CURL_OPTS="--cacert ai-solutions-ca.crt" for the gateway's self-signed certificate

set -euo pipefail

AGENT="${1:-http://localhost:8090}"
PROMPT="${2:-Estimate pi with a Monte Carlo simulation in 4 parallel sandboxes, seeds 1 to 4, 1,000,000 samples each, and compare them in a table.}"
: "${APP_AUTH_TOKEN:?set APP_AUTH_TOKEN (printed by deploy.sh)}"
read -r -a EXTRA <<<"${CURL_OPTS:-}"
api() { curl -sS "${EXTRA[@]}" -H "Authorization: Bearer ${APP_AUTH_TOKEN}" "$@"; }

echo "=== SPACE Coding Agent ==="
echo "Target: ${AGENT}"
echo ""

echo "--- Health (public) ---"
curl -sS "${EXTRA[@]}" "${AGENT}/health"; echo ""

echo "--- Status: LLM and SPACE reachability, live microVMs ---"
api "${AGENT}/api/status" | python3 -m json.tool

echo "--- New session ---"
SID=$(api -X POST "${AGENT}/api/sessions" | python3 -c 'import json, sys; print(json.load(sys.stdin)["session_id"])')
echo "session_id: ${SID}"
echo ""

# Subscribe first, then send: the stream replays from the start of the session anyway.
echo "--- Prompt: ${PROMPT}"
PAYLOAD=$(PROMPT="${PROMPT}" python3 -c 'import json, os; print(json.dumps({"text": os.environ["PROMPT"]}))')
api -X POST "${AGENT}/api/sessions/${SID}/messages" -H "Content-Type: application/json" -d "${PAYLOAD}"; echo ""
echo ""

# -N prints events as they arrive; the filter stops at run.end.
echo "--- Events (SSE) ---"
api -N "${AGENT}/api/sessions/${SID}/events" | python3 -c '
import json, sys
answer = []
for line in sys.stdin:
    if not line.startswith("data: "):
        continue
    ev = json.loads(line[6:])
    t, d = ev["type"], ev["data"]
    if t == "message.delta":
        answer.append(d["text"])
    elif t in ("tool.start", "tool.end", "fanout.start", "fanout.end", "sandbox.ready", "sandbox.killed", "run.end"):
        brief = {k: v for k, v in d.items() if k in ("name", "status", "duration_ms", "n", "sandbox_id", "create_ms", "reason", "wall_ms", "speedup")}
        seq = ev["seq"]
        print(f"{seq:>5} {t:<16} {json.dumps(brief)}", flush=True)
    if t == "run.end":
        break
print("\n--- Answer ---\n" + "".join(answer))
'

echo "--- Clean up: delete the session (kills its sandbox) ---"
api -X DELETE "${AGENT}/api/sessions/${SID}"; echo ""

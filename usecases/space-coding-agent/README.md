# SPACE Coding Agent

A coding agent built with **Microsoft Agent Framework** that runs every line of code in
**SPACE** sandboxes: Firecracker microVMs, each with its own Linux kernel, created through
SPACE's E2B-compatible API. A live web UI shows the work as it happens:

- **Chat** in the middle, with example prompts.
- **Tool calls** on the left: each call with its arguments, duration and result.
- **Sandbox Board** on the right: one card per microVM, from *queued* and *creating*
  through *ready*, *running* (live stdout) and *done*, to *killed*, with its sandbox id,
  guest kernel, boot time and run time.

Each chat gets one **session sandbox** that keeps its files between calls. For independent
experiments, the agent **fans out**: it starts one fresh microVM per task, runs them all
at once, collects one `RESULT: {json}` line from each, kills them, and compares the results.

Compared with [coding-agent-maf](../coding-agent-maf/), this blueprint:
- runs code in microVMs (SPACE) rather than in Agent Sandbox pods;
- has a sandbox per chat, not one shared sandbox per process;
- runs real parallel fan-out across sandboxes;
- shows the sandbox lifecycle live instead of using the stock DevUI.

## Overview

```
 Browser ── https://space-coding-agent.<base> ──► eg-gateway ──► space-coding-agent pod
   chat · tool timeline · Sandbox Board  ◄── SSE ──┘                │
                                                                    ├─► LiteLLM (model, tool calls)
                                                                    │
                                                                    └─► eg-gateway Service (in-cluster)
                                                                          Host: api.e2b.<base>          → SPACE control plane
                                                                          Host: 49983-<id>.e2b.<base>   → envd in the microVM
                                                                                     │
                                                              SPACE node: Firecracker microVMs (template "seed")
```

| Piece | What it is |
|---|---|
| Agent | MAF `Agent` on LiteLLM's OpenAI API, streamed (`agent.run(..., stream=True)`), with a per-chat `AgentSession` for history |
| Tools | `run_python`, `run_shell`, `write_file`, `read_file`, `list_files` (session sandbox); `run_sweep` (one script × N `PARAM` values), `run_parallel` (N scripts in one string, each under a `# task: <label>` line, with shared code above the first); `sandbox_status`, `reset_sandbox` |
| Sandboxes | `e2b.AsyncSandbox` against SPACE: create, run in the background with streamed output, kill |
| UI | Static HTML/JS served by the app (no CDN, no build step); one `EventSource` per chat with `Last-Event-ID` replay |

### Why the app dials the gateway

SPACE routes E2B traffic **by Host header only** (`api.e2b.<base>` and
`<port>-<sandbox-id>.e2b.<base>`), so the SDK cannot be pointed at an in-cluster Service URL.
At startup the app therefore:
1. resolves `e2b.<base>` and `*.e2b.<base>` to the gateway's in-cluster Service (`SPACE_GATEWAY_ADDR`); every other name resolves normally;
2. trusts the stack CA along with the public roots (`SPACE_CA_FILE`).

This is why the app runs on the asyncio event loop: uvloop would bypass the resolver.
`deploy.sh` discovers the gateway Service and the CA certificate for you.

## Prerequisites

- Intel® Agent Toolkit installed with Toolkit Manager ([docs/toolkit-manager-setup.md](../../docs/toolkit-manager-setup.md)), with SPACE and its E2B API enabled in `env/<env>/config.agent-toolkit.yaml`:
  ```yaml
  space_enabled: true
  space_e2b_enabled: true
  ```
- A tool-calling model in LiteLLM (default `qwen3-coder-30b`; deploy it with `./model-manager deploy qwen3-coder-30b --wait`).
- Namespace `space-coding-agent` in `litellm_allowed_namespaces`. If it isn't, deploy with `PATCH_LITELLM_AUTHZ=true`.
- `kubectl`, `helm`, and `nerdctl` (or `docker`) on the machine you deploy from.

Agent Sandbox, Redis and KubeRay are **not** needed.

## Quick Start

```bash
cp config.env config.local.env && set -a && source config.local.env && set +a
PATCH_LITELLM_AUTHZ=true ./deploy-usecase.sh space-coding-agent
kubectl -n space-coding-agent exec deploy/space-coding-agent -- python -m app.selfcheck --n 4
```

Open `https://space-coding-agent.<base>` and sign in with the app token that `deploy.sh`
prints. The token is kept across upgrades. Read it back with:

```bash
kubectl -n space-coding-agent get secret space-coding-agent-secret -o jsonpath='{.data.APP_AUTH_TOKEN}' | base64 -d
```

`selfcheck` creates 4 microVMs at once from inside the pod, prints each one's boot time and
guest kernel, and kills them. It is the quickest proof that the pod → gateway → SPACE path works.

## Configuration

`deploy.sh` discovers everything marked *auto*. The chart values are in [helm-chart/values.yaml](helm-chart/values.yaml).

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_BASE_URL` | in-cluster LiteLLM | OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | *auto* (`litellm/litellm-master-key`) | LiteLLM key; prefer a virtual key |
| `MODEL_NAME` | `qwen3-coder-30b` | Tool-calling model in LiteLLM |
| `E2B_API_KEY`, `E2B_DOMAIN` | *auto* (`space/space-api-key`) | SPACE E2B key and domain (`e2b.<base>`) |
| `SPACE_GATEWAY_ADDR` | *auto* (eg-gateway Service) | Where `e2b.<base>` and `*.e2b.<base>` resolve inside the app |
| `SPACE_CA_FILE` | *auto* (`cert-manager/ai-solutions-ca`, `ca.crt`) | Stack CA, trusted in addition to the public roots |
| `APP_AUTH_TOKEN` | *auto* (kept, else generated) | UI sign-in and `Authorization: Bearer` for the API |
| `SPACE_TEMPLATE` | `seed` | SPACE template (Ubuntu, Python 3.14, standard library only) |
| `FANOUT_CONCURRENCY` | `8` | microVMs one parallel call creates at once; the rest show as *queued* |
| `FANOUT_MAX_TASKS` / `FANOUT_GLOBAL_MAX` | `16` / `16` | Tasks per parallel call / parallel microVMs across all chats |
| `MAX_SESSIONS` | `10` | Concurrent chats; the idlest is closed when full |
| `SESSION_SANDBOX_TTL_S` / `SESSION_SANDBOX_IDLE_S` | `900` / `600` | Session sandbox lifetime (renewed on use) / idle kill |
| `EXEC_TIMEOUT_S` / `EXEC_TIMEOUT_MAX_S` | `120` / `600` | Per-execution timeout, default / cap |
| `RUN_TIMEOUT_S` | `900` | One request, all tool calls included |
| `MAX_TOOL_CALLS_PER_RUN` | `30` | Tool budget per request |
| `LANGFUSE_*` | *auto* (`langfuse/langfuse-secrets`) | Optional tracing |
| `NAMESPACE` / `INGRESS_HOST` | `space-coding-agent` / `space-coding-agent.<base>` | Where it is deployed |

## API Reference

Everything except `/health` and `/api/login` needs the token, either as `Authorization: Bearer <token>` or as the cookie `/api/login` sets.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `POST` | `/api/login` `{"token"}` | Sets the HttpOnly session cookie (12 h) for the browser |
| `GET` | `/api/status` | LLM and SPACE reachability, live microVMs, pod kernel, model |
| `POST` | `/api/sessions` | New chat; returns `session_id` and `last_seq` |
| `GET` | `/api/sessions/{id}` | Snapshot: messages, live sandboxes, `running` |
| `POST` | `/api/sessions/{id}/messages` `{"text"}` | Start a request (202). 409 while one is running |
| `GET` | `/api/sessions/{id}/events` | SSE stream. Resumes after `Last-Event-ID` or `?after=<seq>` |
| `POST` | `/api/sessions/{id}/cancel` | Stop the request and kill its fan-out microVMs |
| `POST` | `/api/sessions/{id}/reset` | Cancel, kill the session sandbox, clear the history |
| `POST` / `DELETE` | `/api/sessions/{id}/sandbox` | Warm up / kill the session sandbox |
| `DELETE` | `/api/sessions/{id}` | Close the chat (kills its sandboxes) |

```bash
H="Authorization: Bearer $APP_AUTH_TOKEN"; URL=https://space-coding-agent.solutions.ai
SID=$(curl -s -H "$H" -X POST $URL/api/sessions | jq -r .session_id)
curl -sN -H "$H" "$URL/api/sessions/$SID/events" &            # live events
curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"text":"Run uname -a in your sandbox"}' $URL/api/sessions/$SID/messages
```

Every event is `{v, seq, ts, session_id, run_id, call_id, type, data}`. The types are:

| Group | Types |
|---|---|
| Session | `session.snapshot`, `session.reset` |
| Run | `run.start`, `run.end` |
| Messages | `message.delta`, `message.end` |
| Tools | `tool.start`, `tool.end` |
| Fan-out | `fanout.start`, `fanout.end` |
| Sandboxes | `sandbox.queued`, `sandbox.creating`, `sandbox.ready`, `sandbox.info`, `sandbox.exec.start`, `sandbox.exec.output`, `sandbox.exec.end`, `sandbox.killed`, `sandbox.error` |

`call_id` links a sandbox card to the tool call that created it.

## Running Examples

```bash
export APP_AUTH_TOKEN=...   # printed by deploy.sh
python3 examples/chat_client.py --url https://space-coding-agent.solutions.ai --cacert ai-solutions-ca.crt \
  "Estimate pi in 8 parallel sandboxes, seeds 1 to 8, 4,000,000 samples each"
CURL_OPTS="--cacert ai-solutions-ca.crt" ./examples/chat_client.sh https://space-coding-agent.solutions.ai
```

`chat_client.py` uses only the standard library and prints the timeline as it happens:

```
  77.4s  ▶ run_sweep
  77.4s  ⇉ parallel run: 8 microVMs, up to 8 at once
  99.8s    ● f_95b1… ready: 01a20bec-… in 22383 ms
  99.9s      f_95b1… kernel 6.1.155+ python 3.14.4 vcpu 2
 100.5s      f_95b1… exit 0 in 552 ms RESULT {"seed": 3, "pi_estimate": 3.140465}
 100.6s    ✕ f_95b1… killed (fanout_done)
```

[examples/prompts.md](examples/prompts.md) lists the demo prompts and what each one shows.

## Local Development

**Docker Compose**, against a running stack's gateway:

```bash
export GATEWAY_IP=<eg-gateway LB IP> BASE_DOMAIN=solutions.ai STACK_CA=/path/ai-solutions-ca.crt
export LITELLM_API_KEY=... E2B_API_KEY=... APP_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up --build    # http://localhost:8090
```

**Without Docker**, for example on the SPACE node, which runs containerd only:

```bash
cd src && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests                  # unit tests, no platform needed
E2B_API_KEY=... E2B_DOMAIN=e2b.solutions.ai SPACE_GATEWAY_ADDR=<gateway IP> \
  GATEWAY_RESOLVE_HOSTS=litellm.solutions.ai SPACE_CA_FILE=/path/ai-solutions-ca.crt \
  OPENAI_BASE_URL=https://litellm.solutions.ai/v1 OPENAI_API_KEY=... APP_AUTH_TOKEN=dev NO_PROXY='*' \
  .venv/bin/python main.py
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `deploy.sh`: *SPACE's E2B API was not found* | Set `space_enabled: true` and `space_e2b_enabled: true`, then `./es_auto_installer.sh install agent-toolkit` |
| SPACE dot red; `selfcheck` fails with `certificate verify failed` | The stack CA is not mounted: check ConfigMap `space-coding-agent-space-ca` and `SPACE_CA_FILE` |
| `selfcheck` fails with a 404 or with HTML | The request did not reach the gateway with the E2B Host header: check `SPACE_GATEWAY_ADDR`, and `kubectl get httproute -n space space-e2b-api` |
| LLM dot red, 403 in the logs | Namespace not allowed by LiteLLM: add it to `litellm_allowed_namespaces`, or redeploy with `PATCH_LITELLM_AUTHZ=true` |
| Cards wait in *creating* for 10–25 s when many start at once | SPACE boots cold microVMs one after another on a single node. One boots in about 4 s; 8 at once take about 22 s. The banner separates boot time from run time. A SPACE warm pool removes the wait |
| The answer takes minutes | The model generates at CPU speed (about 6 tokens/s for a 14B model on Xeon). Prompts that let the agent use `run_sweep` (one script, many params) need far fewer tokens than N separate scripts |
| A tool call shows *error in 0 ms* on the timeline | The framework rejected the arguments before the tool ran (shown with the reason, which the model also gets). Array arguments sent as JSON strings are accepted; code is never nested inside JSON arrays |
| `ModuleNotFoundError` in the sandbox | The `seed` template has the Python standard library only, with no pip. Ask for stdlib-only code |
| Code cannot reach cluster services from the sandbox | By design: SPACE sandboxes get public egress only |
| The UI shows the sign-in page again | The 12 h cookie expired, or the token changed on redeploy (it is kept unless you set `APP_AUTH_TOKEN`) |

## Limits

- One replica: chats, event buffers and sandbox handles live in memory. The Deployment uses `Recreate`, and a new pod kills the sandboxes the old one left (they carry an `instance` metadata tag).
- The template is `seed`, the only one SPACE ships today: stdlib-only Python 3.14, 2 vCPU, about 1 GB of RAM.
- SPACE's E2B key broker is evaluation-grade: one shared API key for the whole platform.

#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the SPACE Coding Agent use case
#
# A Microsoft Agent Framework coding agent that runs code in SPACE Firecracker
# microVMs (E2B-compatible API), with a live UI: chat, tool-call timeline and a
# Sandbox Board. Deploys onto Intel® Agent Toolkit with Helm.
#
# Prerequisites:
#   • Intel Agent Toolkit with SPACE and its E2B API enabled
#     (space_enabled: true, space_e2b_enabled: true in config.agent-toolkit.yaml)
#   • kubectl pointing at the cluster (env/<env>/kubeconfig.yaml), helm
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — auto-discovered from the cluster or prompted):
#   CLUSTER_URL         Toolkit Manager base_domain_name, e.g. solutions.ai (from eg-gateway)
#   OPENAI_API_KEY      LiteLLM key (default: litellm-master-key; prefer a virtual key)
#   MODEL_NAME          Tool-calling model in LiteLLM (default: qwen3-coder-30b)
#   OPENAI_BASE_URL     Override LiteLLM URL (default: in-cluster litellm.litellm:4000)
#   E2B_API_KEY         SPACE E2B key (default: Secret space/space-api-key)
#   E2B_DOMAIN          SPACE E2B domain (default: Secret space/space-api-key, e.g. e2b.solutions.ai)
#   TM_GATEWAY_SVC      Gateway Service the app dials for SPACE (default: discovered by label)
#   STACK_CA_B64        Stack CA, base64 PEM (default: cert-manager/ai-solutions-ca ca.crt)
#   APP_AUTH_TOKEN      UI/API token (default: kept from the existing release, else generated)
#   FANOUT_CONCURRENCY  microVMs one parallel call creates at once (default: 8)
#   LANGFUSE_*          Langfuse keys/host (default: langfuse/langfuse-secrets)
#   NAMESPACE           Kubernetes namespace (default: space-coding-agent)
#   INGRESS_HOST        HTTPRoute hostname (default: space-coding-agent.<CLUSTER_URL>)
#   PATCH_LITELLM_AUTHZ true = add NAMESPACE to LiteLLM's AuthorizationPolicy directly
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/helm-chart"
SRC_DIR="${SCRIPT_DIR}/src"
RELEASE="space-coding-agent"

# Tagged by source content: an unchanged tree keeps its tag (no restart), a change rolls the pod.
SRC_HASH="$(cd "${SRC_DIR}" && find . -type f ! -path './tests/*' ! -path '*/__pycache__/*' ! -path './.pytest_cache/*' -print0 \
  | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)"
AGENT_IMAGE="${AGENT_IMAGE:-space-coding-agent:${SRC_HASH}}"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# shellcheck source=scripts/toolkit-manager-lib.sh
source "${SCRIPT_DIR}/../../scripts/toolkit-manager-lib.sh"

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
for cmd in kubectl helm; do
  command -v "$cmd" &>/dev/null || die "Required tool '${cmd}' not found. Install it and re-run."
done

# ---------------------------------------------------------------------------
# Build the image into containerd's k8s.io namespace (nerdctl) so the kubelet
# uses it with pullPolicy: IfNotPresent. Skip with SKIP_BUILD=true + AGENT_IMAGE.
# ---------------------------------------------------------------------------
build_images() {
  if [[ "${SKIP_BUILD:-false}" == "true" ]]; then
    info "SKIP_BUILD=true — skipping image build."
    return
  fi

  local tool="${CONTAINER_TOOL:-}"
  if [[ -z "${tool}" ]]; then
    if command -v nerdctl &>/dev/null; then
      tool="nerdctl"
    elif command -v docker &>/dev/null; then
      tool="docker"
      warn "nerdctl not found; using docker. On a containerd-backed cluster the"
      warn "kubelet may not see docker-built images — push to a registry and set"
      warn "AGENT_IMAGE + SKIP_BUILD=true if the pod stays in ImagePullBackOff."
    else
      die "No container builder found. Install nerdctl (or docker), or set SKIP_BUILD=true."
    fi
  fi

  local build_cmd=("${tool}")
  local ns_args=()
  if [[ "${tool}" == "nerdctl" ]]; then
    ns_args=(--namespace k8s.io)
    [[ "$(id -u)" -ne 0 ]] && build_cmd=(sudo nerdctl)
    # Install/start buildkitd + pre-stage base images if needed.
    CONTAINER_TOOL="${tool}" "${SCRIPT_DIR}/../../scripts/ensure-image-builder.sh" "${SRC_DIR}/Dockerfile"
  fi

  local proxy_args=(
    --build-arg "http_proxy=${http_proxy:-}"
    --build-arg "https_proxy=${https_proxy:-}"
    --build-arg "no_proxy=${no_proxy:-}"
    --build-arg "HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}"
    --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}"
    --build-arg "NO_PROXY=${NO_PROXY:-${no_proxy:-}}"
  )

  info "Building agent image '${AGENT_IMAGE}' with ${build_cmd[*]}..."
  "${build_cmd[@]}" "${ns_args[@]}" build "${proxy_args[@]}" \
    -t "${AGENT_IMAGE}" -f "${SRC_DIR}/Dockerfile" "${SRC_DIR}"
  success "Built image: ${AGENT_IMAGE}"
}

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
prompt_if_empty() {
  local var_name="$1" prompt_text="$2" default="${3:-}"
  if [[ -z "${!var_name:-}" ]]; then
    if [[ -n "$default" ]]; then
      read -rp "  ${prompt_text} [${default}]: " input
      input="${input:-$default}"
    else
      read -rp "  ${prompt_text}: " input
    fi
    printf -v "$var_name" '%s' "$input"
  fi
}

prompt_secret_if_empty() {
  local var_name="$1" prompt_text="$2"
  if [[ -z "${!var_name:-}" ]]; then
    read -rsp "  ${prompt_text}: " input; echo ""
    printf -v "$var_name" '%s' "$input"
  fi
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  SPACE Coding Agent — Deployment"
echo "============================================================"
echo ""

tm_discover_base_domain
tm_discover_litellm_key
tm_discover_langfuse
tm_discover_space_e2b
tm_discover_gateway_service
tm_discover_stack_ca

prompt_if_empty CLUSTER_URL \
  "Toolkit Manager base_domain_name (e.g. solutions.ai)"
[[ -n "${CLUSTER_URL}" ]] || die "CLUSTER_URL (Toolkit Manager base_domain_name) is required."

prompt_secret_if_empty OPENAI_API_KEY \
  "LiteLLM API key (OPENAI_API_KEY)"

prompt_if_empty MODEL_NAME \
  "Model name registered in the gateway (must support tool calling)" "qwen3-coder-30b"

[[ -n "${E2B_API_KEY:-}" && -n "${E2B_DOMAIN:-}" ]] || die "SPACE's E2B API was not found (Secret space/space-api-key).
        Enable it in env/<env>/config.agent-toolkit.yaml (space_enabled: true, space_e2b_enabled: true),
        run ./es_auto_installer.sh install agent-toolkit, or set E2B_API_KEY and E2B_DOMAIN."
[[ -n "${TM_GATEWAY_SVC:-}" ]] || warn "Gateway Service not found: SPACE hostnames will use normal DNS inside the pod."
[[ -n "${STACK_CA_B64:-}" ]] || warn "Stack CA not found: the pod trusts public roots only (fine with a public certificate)."
tm_check_space_e2b

NAMESPACE="${NAMESPACE:-space-coding-agent}"
INGRESS_HOST="${INGRESS_HOST:-space-coding-agent.${CLUSTER_URL}}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-${TM_LITELLM_URL}}"
FANOUT_CONCURRENCY="${FANOUT_CONCURRENCY:-8}"
# Keep the token across upgrades so signed-in browsers and scripts keep working.
APP_AUTH_TOKEN="${APP_AUTH_TOKEN:-$(tm_secret_value "${NAMESPACE}" "${RELEASE}-secret" APP_AUTH_TOKEN)}"
APP_AUTH_TOKEN="${APP_AUTH_TOKEN:-$(openssl rand -hex 24)}"

echo ""
info "Deployment configuration:"
echo "  NAMESPACE          = ${NAMESPACE}"
echo "  INGRESS_HOST       = ${INGRESS_HOST}"
echo "  OPENAI_BASE_URL    = ${OPENAI_BASE_URL}"
echo "  MODEL_NAME         = ${MODEL_NAME}"
echo "  E2B_DOMAIN         = ${E2B_DOMAIN}"
echo "  SPACE gateway      = ${TM_GATEWAY_SVC:-(normal DNS)}"
echo "  Stack CA           = $([[ -n "${STACK_CA_B64:-}" ]] && echo 'trusted' || echo '(public roots only)')"
echo "  FANOUT_CONCURRENCY = ${FANOUT_CONCURRENCY}"
echo "  LANGFUSE_HOST      = ${LANGFUSE_HOST:-(tracing disabled)}"
echo ""

# ---------------------------------------------------------------------------
# Build image(s)
# ---------------------------------------------------------------------------
build_images

# ---------------------------------------------------------------------------
# Namespace (PSS restricted + Istio ambient) and LiteLLM access
# ---------------------------------------------------------------------------
tm_prepare_namespace "${NAMESPACE}"
tm_check_litellm_access "${NAMESPACE}"

# ---------------------------------------------------------------------------
# Helm deploy
# ---------------------------------------------------------------------------
AGENT_IMAGE_REPO="${AGENT_IMAGE%:*}"; AGENT_IMAGE_TAG="${AGENT_IMAGE##*:}"
[[ "${AGENT_IMAGE}" == *:* ]] || AGENT_IMAGE_TAG="latest"

HELM_ARGS=(
  upgrade --install "${RELEASE}" "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --set "agent.image.repository=${AGENT_IMAGE_REPO}"
  --set "agent.image.tag=${AGENT_IMAGE_TAG}"
  --set "agent.openaiBaseUrl=${OPENAI_BASE_URL}"
  --set "agent.openaiApiKey=${OPENAI_API_KEY}"
  --set "agent.modelName=${MODEL_NAME}"
  --set "agent.appAuthToken=${APP_AUTH_TOKEN}"
  --set "agent.limits.fanoutConcurrency=${FANOUT_CONCURRENCY}"
  --set "space.e2bApiKey=${E2B_API_KEY}"
  --set "space.e2bDomain=${E2B_DOMAIN}"
  --set "space.gatewayAddr=${TM_GATEWAY_SVC:-}"
  --set-string "space.caCertB64=${STACK_CA_B64:-}"
  --set "httpRoute.enabled=true"
  --set "httpRoute.host=${INGRESS_HOST}"
  --wait
  --timeout 5m
)

# Langfuse tracing (optional) — passed only when keys are provided.
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
  HELM_ARGS+=(
    --set "agent.langfusePublicKey=${LANGFUSE_PUBLIC_KEY}"
    --set "agent.langfuseSecretKey=${LANGFUSE_SECRET_KEY}"
    --set "agent.langfuseHost=${LANGFUSE_HOST:-}"
    --set "agent.langfuseEnableSensitiveData=${LANGFUSE_ENABLE_SENSITIVE_DATA:-true}"
  )
  info "Langfuse tracing enabled (host: ${LANGFUSE_HOST:-<default>})."
fi

info "Deploying SPACE Coding Agent via Helm..."
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# Post-deploy summary
# ---------------------------------------------------------------------------
success "SPACE Coding Agent deployed — namespace: ${NAMESPACE}"
echo ""
info "Open the UI and sign in with the app token:"
info "  https://${INGRESS_HOST}"
tm_print_gateway_hint "${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/${RELEASE} 8090:8090 → http://localhost:8090)"
info ""
info "  App token (UI sign-in, or 'Authorization: Bearer' for the API):"
info "  ${APP_AUTH_TOKEN}"
info ""
info "  Platform check (creates, probes and kills 4 microVMs):"
info "  kubectl -n ${NAMESPACE} exec deploy/${RELEASE} -- python -m app.selfcheck --n 4"
echo ""

# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""space-coding-agent: a MAF coding agent on SPACE microVMs, with a live 3-pane UI."""

import logging
import os

from app import net

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
for _noisy in ("httpx", "e2b", "e2b.api", "e2b.api.client_async"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# Kubernetes probes hit /health every few seconds; keep them out of the access log.
logging.getLogger("uvicorn.access").addFilter(lambda record: "/health" not in record.getMessage())
net.bootstrap()  # before httpx is imported: SPACE hostnames resolve via the gateway


def setup_observability() -> None:
    """Langfuse tracing through OpenTelemetry, when keys are configured."""
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return
    try:
        from agent_framework.observability import configure_otel_providers
        from langfuse import get_client

        if get_client().auth_check():
            configure_otel_providers(
                enable_sensitive_data=os.getenv("LANGFUSE_ENABLE_SENSITIVE_DATA", "true").lower() == "true")
            logging.info("Langfuse tracing enabled")
    except Exception as exc:
        logging.warning("Langfuse tracing disabled: %s", exc)


if __name__ == "__main__":
    import uvicorn

    setup_observability()
    uvicorn.run("app.api:app", host=os.getenv("HOST", "0.0.0.0"),  # nosec B104 - container listens on all interfaces
                port=int(os.getenv("PORT", "8090")), loop="asyncio", http="h11", proxy_headers=True,
                forwarded_allow_ips="*", log_level=os.getenv("LOG_LEVEL", "info").lower())

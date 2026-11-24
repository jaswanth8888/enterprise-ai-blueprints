# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Reach SPACE's E2B API through the platform gateway, from inside the cluster.

SPACE routes E2B traffic by Host header only (api.<domain> and
<port>-<sandbox-id>.<domain>), so the SDK has to dial the gateway with the real
hostnames. bootstrap() resolves those hostnames to SPACE_GATEWAY_ADDR (the
gateway's in-cluster Service) and trusts the stack CA. Nothing else is
redirected. Call it before httpx is imported.
"""

import logging
import os
import socket

log = logging.getLogger(__name__)

_CA_BUNDLE = "/tmp/ca-bundle.pem"  # nosec B108 - private to the container, read-only root fs elsewhere
_original_getaddrinfo = socket.getaddrinfo


def _suffixes() -> list[str]:
    domain = os.getenv("E2B_DOMAIN", "").strip().rstrip(".").lower()
    extra = [h.strip().rstrip(".").lower() for h in os.getenv("GATEWAY_RESOLVE_HOSTS", "").split(",")]
    return [h for h in [domain, *extra] if h]


def matches(host, suffixes: list[str]) -> bool:
    """True for a host equal to, or under, one of the suffixes."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.rstrip(".").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def install_resolver(gateway: str, suffixes: list[str]) -> None:
    def getaddrinfo(host, *args, **kwargs):
        return _original_getaddrinfo(gateway if matches(host, suffixes) else host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo


def build_ca_bundle(stack_ca: str) -> str | None:
    """Public roots plus the stack CA: SSL_CERT_FILE replaces the trust store."""
    if not stack_ca or not os.path.isfile(stack_ca):
        return None
    import certifi

    with open(certifi.where(), encoding="ascii") as public, open(stack_ca, encoding="ascii") as stack:
        bundle = public.read() + "\n" + stack.read()
    with open(_CA_BUNDLE, "w", encoding="ascii") as out:
        out.write(bundle)
    return _CA_BUNDLE


def bootstrap() -> None:
    gateway = os.getenv("SPACE_GATEWAY_ADDR", "").strip()
    suffixes = _suffixes()
    if gateway and suffixes:
        install_resolver(gateway, suffixes)
        log.info("resolving %s via gateway %s", ", ".join(suffixes), gateway)
    bundle = build_ca_bundle(os.getenv("SPACE_CA_FILE", "/etc/space-ca/ca.crt"))
    if bundle:
        os.environ["SSL_CERT_FILE"] = bundle
        log.info("trusting the stack CA (%s)", bundle)


def warn_if_uvloop() -> None:
    """uvloop resolves names in libuv, bypassing the resolver above."""
    import asyncio

    loop = asyncio.get_running_loop()
    if type(loop).__module__.startswith("uvloop"):
        log.warning("uvloop is active: SPACE hostnames are not resolved via the gateway")

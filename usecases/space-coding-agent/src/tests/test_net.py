# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import socket

import certifi

from app import net


def test_matches_domain_and_subdomains_only():
    suffixes = ["e2b.solutions.ai"]
    assert net.matches("e2b.solutions.ai", suffixes)
    assert net.matches("api.e2b.solutions.ai", suffixes)
    assert net.matches("49983-abc.E2B.solutions.ai.", suffixes)
    assert net.matches(b"api.e2b.solutions.ai", suffixes)
    assert not net.matches("xe2b.solutions.ai", suffixes)
    assert not net.matches("e2b.solutions.ai.evil.com", suffixes)
    assert not net.matches(None, suffixes)


def test_resolver_redirects_only_matching_hosts(monkeypatch):
    seen = []
    monkeypatch.setattr(net, "_original_getaddrinfo", lambda host, *a, **k: seen.append(host) or [])
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)  # restored after the test
    net.install_resolver("10.0.0.1", ["e2b.solutions.ai"])
    socket.getaddrinfo("api.e2b.solutions.ai", 443)
    socket.getaddrinfo("pypi.org", 443)
    assert seen == ["10.0.0.1", "pypi.org"]


def test_ca_bundle_keeps_public_roots(tmp_path, monkeypatch):
    stack = tmp_path / "ca.crt"
    stack.write_text("-----BEGIN CERTIFICATE-----\nSTACK\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(net, "_CA_BUNDLE", str(tmp_path / "bundle.pem"))
    path = net.build_ca_bundle(str(stack))
    text = open(path, encoding="ascii").read()
    assert "STACK" in text
    assert text.startswith(open(certifi.where(), encoding="ascii").read()[:200])
    assert net.build_ca_bundle(str(tmp_path / "missing.crt")) is None

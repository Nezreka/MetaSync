"""Shared fixtures and fakes for the relay test suite.

The token issuer here mirrors issueAccessToken in src/sin/logic.ts
byte-for-byte in shape: header {"alg": "EdDSA", "typ": "SIN"}, claims
{key_id, node_id, scopes, exp, iat} as compact JSON, Ed25519 signature
over the dotted signing input, base64url segments.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_relay.tokens import encode_base64url, node_id_for_public_key

FULL_SCOPES = ["report:write", "index:read", "playlists:read", "whoami:read", "meta:read"]


def raw_public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def issue_token(
    pool_private: Ed25519PrivateKey,
    key_id: str,
    node_id: str,
    scopes,
    now: float | None = None,
    ttl: int = 24 * 3600,
    header_extra: dict | None = None,
    header_override: dict | None = None,
) -> str:
    """Mirror of issueAccessToken (src/sin/logic.ts)."""
    iat = int(now if now is not None else time.time())
    claims = {
        "key_id": key_id,
        "node_id": node_id,
        "scopes": list(scopes),
        "exp": iat + ttl,
        "iat": iat,
    }
    header = {"alg": "EdDSA", "typ": "SIN"}
    if header_extra:
        header.update(header_extra)
    if header_override:
        header = header_override
    signing_input = (
        encode_base64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + encode_base64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    signature = pool_private.sign(signing_input.encode("utf-8"))
    return f"{signing_input}.{encode_base64url(signature)}"


class NodeIdentity:
    """A node's Ed25519 identity, shaped like the node's SIN identity."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public_raw = raw_public_bytes(self.private)
        self.node_id = node_id_for_public_key(self.public_raw)

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data)


@pytest.fixture(scope="session")
def pool_private() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture(scope="session")
def pool_public(pool_private):
    return pool_private.public_key()


@pytest.fixture()
def node_a() -> NodeIdentity:
    return NodeIdentity()


@pytest.fixture()
def node_b() -> NodeIdentity:
    return NodeIdentity()


@pytest.fixture()
def node_c() -> NodeIdentity:
    return NodeIdentity()

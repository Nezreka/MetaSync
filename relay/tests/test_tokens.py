"""Token verification: the Python mirror of verifyAccessToken."""

from __future__ import annotations

import hashlib
import time

import pytest
from conftest import FULL_SCOPES, issue_token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metasync_relay.tokens import (
    decode_base64url,
    encode_base64url,
    node_id_for_public_key,
    parse_pool_public_key,
    verify_access_token,
)

NODE = "ab" * 32


def test_valid_token_verifies(pool_private, pool_public):
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES)
    claims = verify_access_token(token, pool_public)
    assert claims is not None
    assert claims.key_id == "key-1"
    assert claims.node_id == NODE
    assert claims.scopes == tuple(FULL_SCOPES)
    assert claims.exp - claims.iat == 24 * 3600


def test_wrong_signing_key_rejected(pool_public):
    other = Ed25519PrivateKey.generate()
    token = issue_token(other, "key-1", NODE, FULL_SCOPES)
    assert verify_access_token(token, pool_public) is None


def test_tampered_payload_rejected(pool_private, pool_public):
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES)
    header, _payload, sig = token.split(".")
    forged_claims = encode_base64url(
        b'{"key_id":"key-1","node_id":"' + b"cd" * 32 + b'","scopes":[],"exp":9999999999,"iat":1}'
    )
    assert verify_access_token(f"{header}.{forged_claims}.{sig}", pool_public) is None


def test_expired_token_rejected(pool_private, pool_public):
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES, now=time.time() - 90000)
    assert verify_access_token(token, pool_public) is None


def test_exp_equal_to_now_rejected(pool_private, pool_public):
    now = time.time()
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES, now=now - 100, ttl=100)
    assert verify_access_token(token, pool_public, now=now) is None


def test_wrong_typ_rejected(pool_private, pool_public):
    token = issue_token(
        pool_private, "key-1", NODE, FULL_SCOPES,
        header_override={"alg": "EdDSA", "typ": "JWT"},
    )
    assert verify_access_token(token, pool_public) is None


def test_wrong_alg_rejected(pool_private, pool_public):
    token = issue_token(
        pool_private, "key-1", NODE, FULL_SCOPES,
        header_override={"alg": "HS256", "typ": "SIN"},
    )
    assert verify_access_token(token, pool_public) is None


def test_extra_header_key_tolerated_like_the_original(pool_private, pool_public):
    # verifyAccessToken checks alg/typ values only; parity is deliberate.
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES, header_extra={"kid": "pool-1"})
    assert verify_access_token(token, pool_public) is not None


@pytest.mark.parametrize("bad", [None, 42, {}, "onepart", "a.b", "a.b.c.d", "...."])
def test_malformed_shapes_rejected(pool_public, bad):
    assert verify_access_token(bad, pool_public) is None


def _mint_with_claims(pool_private, claims: dict) -> str:
    import json

    header = {"alg": "EdDSA", "typ": "SIN"}
    signing_input = (
        encode_base64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + encode_base64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    return f"{signing_input}.{encode_base64url(pool_private.sign(signing_input.encode()))}"


def _good_claims() -> dict:
    now = int(time.time())
    return {
        "key_id": "key-1",
        "node_id": NODE,
        "scopes": ["meta:read"],
        "exp": now + 3600,
        "iat": now,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(exp="tomorrow"),
        lambda c: c.update(exp=True),
        lambda c: c.update(iat=c["exp"] + 1),
        lambda c: c.update(scopes=["meta:read", 7]),
        lambda c: c.update(scopes="meta:read"),
        lambda c: c.update(key_id=""),
        lambda c: c.update(node_id=123),
        lambda c: c.pop("key_id"),
    ],
)
def test_bad_claim_shapes_rejected(pool_private, pool_public, mutate):
    claims = _good_claims()
    mutate(claims)
    assert verify_access_token(_mint_with_claims(pool_private, claims), pool_public) is None


def test_noncanonical_signature_encoding_rejected(pool_private, pool_public):
    token = issue_token(pool_private, "key-1", NODE, FULL_SCOPES)
    header, payload, sig = token.split(".")
    # Same bytes, but the final character carries non-zero padding bits:
    # the strict decoder must refuse what the lenient one would accept.
    raw = decode_base64url(sig, 64)
    assert raw is not None
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    mutated = sig[:-1] + alphabet[alphabet.index(sig[-1]) ^ 1]
    if decode_base64url(mutated, 64) is not None:
        pytest.skip("mutation did not produce a non-canonical encoding")
    assert verify_access_token(f"{header}.{payload}.{mutated}", pool_public) is None


def test_strict_decoder_roundtrip():
    assert decode_base64url(encode_base64url(b"\x00" * 32), 32) == b"\x00" * 32
    assert decode_base64url("not base64!!") is None
    assert decode_base64url(encode_base64url(b"\x00" * 32), 16) is None
    assert decode_base64url("A") is None  # length % 4 == 1


def test_node_id_is_sha256_hex_of_raw_key():
    raw = bytes(range(32))
    assert node_id_for_public_key(raw) == hashlib.sha256(raw).hexdigest()


def test_parse_pool_public_key_forms(pool_public):
    raw = pool_public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert parse_pool_public_key(encode_base64url(raw)) is not None
    pem = pool_public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    assert parse_pool_public_key(pem) is not None
    assert parse_pool_public_key("garbage") is None
    assert parse_pool_public_key("") is None

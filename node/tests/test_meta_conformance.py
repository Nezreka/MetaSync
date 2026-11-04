"""Conformance vectors, node side (DESIGN-METADATA.md §10; gate S7).

The signed texts of the metadata protocol are pinned BYTE FOR BYTE
here, as literals — not round-tripped through this package's own
functions. The relay package pins the same literals in
relay/tests/test_conformance.py, and the TypeScript portal issuer
(§10: it must pass byte for byte) tests against these. A mirror on
either side that drifts — a reordered field, a missing newline —
fails here instead of failing silently in production, where the
only symptom would be every signature verifying against nothing.

Vectors use fixed, obviously-synthetic inputs: issuer "issuer-1",
a relay identity of "wss://relay.example:443", the challenge
bytes(range(32)) (base64url AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBka
GxwdHh8), token "TOKEN", timestamps in late 2023.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.attestation import attestation_signing_text
from metasync_node.relay import hello_v2_signing_text, reauth_signing_text
from metasync_node.revocations import list_signing_text

CHALLENGE = bytes(range(32))


def test_attestation_signing_text_vector():
    assert attestation_signing_text(
        "pool-1", "NODEKEY", 1700000000, 1700000100, 1700604800
    ) == (
        b"metasync-attestation\n"
        b"pool-1\n"
        b"NODEKEY\n"
        b"1700000000\n"
        b"1700000100\n"
        b"1700604800\n"
    )


def test_revocation_list_signing_text_vector():
    # Keys are sorted and deduplicated inside the signed text.
    assert list_signing_text(
        "issuer-1", 42, 1700000000, ["b" * 64, "a" * 64, "a" * 64]
    ) == (
        b"metasync-revocations\n"
        b"issuer-1\n"
        b"42\n"
        b"1700000000\n"
        + b"a" * 64 + b"\n"
        + b"b" * 64 + b"\n"
    )


def test_hello_v2_signing_text_vector():
    assert hello_v2_signing_text(
        "wss://relay.example:443", "full", 1, 2, "TOKEN", 1700000000,
        CHALLENGE,
    ) == (
        b"metasync-relay-hello-v2\n"
        b"wss://relay.example:443\n"
        b"full\n"
        b"1\n"
        b"2\n"
        b"TOKEN\n"
        b"1700000000\n"
        b"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    )


def test_reauth_signing_text_vector():
    assert reauth_signing_text(
        "wss://relay.example:443", "TOKEN", 1700000000
    ) == (
        b"metasync-relay-reauth\n"
        b"wss://relay.example:443\n"
        b"TOKEN\n"
        b"1700000000"
    )

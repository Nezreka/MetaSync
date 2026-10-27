"""Conformance vectors, relay side (DESIGN-METADATA.md section 10;
gate S7).

The signed texts of the metadata protocol are pinned BYTE FOR BYTE
here, as literals — the SAME literals the node package pins in
node/tests/test_meta_conformance.py. A mirror on either side that
drifts fails here instead of failing silently in production. (The
attestation text is pinned node-side only: the relay never handles
attestations, and the TypeScript portal issuer tests against the
node package's vector.)

Vectors use fixed, obviously-synthetic inputs: issuer "issuer-1",
a relay identity of "wss://relay.example:443", the challenge
bytes(range(32)) (base64url AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBka
GxwdHh8), token "TOKEN", timestamps in late 2023.
"""

from __future__ import annotations

from metasync_relay.revocations import list_signing_text
from metasync_relay.server import hello_v2_signing_text, reauth_signing_text

CHALLENGE = bytes(range(32))


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
        relay_identity="wss://relay.example:443",
        mode="full",
        version_min=1,
        version_max=2,
        token="TOKEN",
        timestamp=1700000000,
        challenge=CHALLENGE,
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
        relay_identity="wss://relay.example:443",
        token="TOKEN",
        timestamp=1700000000,
    ) == (
        b"metasync-relay-reauth\n"
        b"wss://relay.example:443\n"
        b"TOKEN\n"
        b"1700000000"
    )

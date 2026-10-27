"""Pool-signed binding attestations, node side (DESIGN-METADATA.md §9).

An attestation is the pool's signed statement about one node key:
``{issuer, node_key, bound_at, issued_at, expires, sig}``. It is
issued and renewed at the token exchange (§10) — the exchange
response carries it alongside the token — and the node embeds its
current one in every batch header it signs (§6), so the attestation
travels with the batch for the batch's whole life and asking nodes
can judge standing offline, against the pool key, without any live
lookup.

The attestation carries TWO different times doing two different
jobs, and keeping them apart is the whole shape:

- ``bound_at`` is when the key was BOUND. It never moves across
  renewals, and it is used for exactly one thing: standing
  (``asserted_at − bound_at ≥ 30 days``, §9).
- ``issued_at``..``expires`` is THIS attestation's validity window —
  7 days from issuance. A batch counts its attestation only when
  the batch's ``asserted_at`` falls inside the window.

An earlier shape ran the window from ``bound_at``, which made the
two rules contradict each other: a 7-day window starting at the
binding can never contain an assertion made 30 days after it, so no
key could ever have standing. The window therefore runs from
issuance, and ``bound_at`` is checked against nothing but the
standing rule. The ``issuer`` field names the signing pool (the
same issuer id the revocation lists carry, §10): it is inside the
signature, and it is what lets an asker pick the revocation list
that governs this attestation when several issuers are trusted.

THE CANONICAL SIGNED BYTES — pinned here, byte for byte, because the
portal-side PR that issues attestations must reproduce them exactly.
The signature is Ed25519 over the UTF-8 encoding of six lines, each
terminated by a newline (including the last)::

    metasync-attestation
    <issuer>
    <node_key>
    <bound_at>
    <issued_at>
    <expires>

where ``node_key`` is the base64url (unpadded) encoding of the raw
32-byte Ed25519 public key — the same string a batch header carries
as ``node`` — and the times are decimal Unix seconds with no
padding. ``sig`` is the base64url (unpadded) encoding of the raw
64-byte signature over exactly those bytes. The context line
domain-separates these signatures from revocation lists
(``metasync-revocations``) and from SIN request signing, in the same
style. node/tests/test_meta_conformance.py pins these bytes
literally.

Verification doctrine mirrors the revocation list's (§10): a missing
issuer key, a malformed shape, a bad signature, or an attestation
naming any key but this node's own all mean the attestation is
DROPPED — never stored, never embedded. An unverifiable attestation
is no attestation at all.
"""
from __future__ import annotations

from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .identity import b64url_decode

SIGNING_CONTEXT = "metasync-attestation"

# §9: the attestation lifetime is 7 days FROM ISSUANCE. Shape
# validation accepts a window (issued_at..expires) up to twice that,
# so a pool that ever lengthens its lifetime modestly does not
# strand its nodes' minting; anything wider is not an attestation
# this node will carry. bound_at is NOT part of the window: a key
# bound years ago still gets a 7-day attestation at each renewal.
MAX_WINDOW_SECONDS = 14 * 24 * 3600


def attestation_signing_text(
    issuer: str,
    node_key: str,
    bound_at: int,
    issued_at: int,
    expires: int,
) -> bytes:
    """The canonical bytes the pool signs for one attestation."""
    lines = [
        SIGNING_CONTEXT,
        issuer,
        node_key,
        str(bound_at),
        str(issued_at),
        str(expires),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _decode_b64url(value: object, expected_length: int) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        raw = b64url_decode(value)
    except Exception:  # noqa: BLE001 - malformed input is a refusal
        return None
    return raw if len(raw) == expected_length else None


def validate_attestation_shape(obj: object) -> dict[str, Any] | None:
    """The wire shape, without any cryptography: a dict whose issuer
    is a non-empty string, whose node_key is base64url of 32 raw
    bytes, whose times are sane Unix-second ints — bound_at no later
    than issued_at (a key is bound before an attestation can be
    issued for it), and a validity window issued_at..expires no
    wider than MAX_WINDOW_SECONDS — and whose sig is base64url of
    64 raw bytes. Returns the normalized dict (exactly the six wire
    fields) or None. Minting refuses anything else (claims.py), so a
    batch can never carry a header field shaped like an attestation
    but built from unchecked parts."""
    if not isinstance(obj, dict):
        return None
    issuer = obj.get("issuer")
    node_key = obj.get("node_key")
    bound_at = obj.get("bound_at")
    issued_at = obj.get("issued_at")
    expires = obj.get("expires")
    sig = obj.get("sig")
    if not isinstance(issuer, str) or not issuer:
        return None
    if _decode_b64url(node_key, 32) is None:
        return None
    if not _is_int(bound_at) or not _is_int(issued_at) \
            or not _is_int(expires):
        return None
    if bound_at < 0 or issued_at < 0 or expires <= issued_at \
            or expires - issued_at > MAX_WINDOW_SECONDS:
        return None
    if bound_at > issued_at:
        return None
    if _decode_b64url(sig, 64) is None:
        return None
    return {
        "issuer": issuer,
        "node_key": node_key,
        "bound_at": int(bound_at),  # type: ignore[arg-type]
        "issued_at": int(issued_at),  # type: ignore[arg-type]
        "expires": int(expires),  # type: ignore[arg-type]
        "sig": sig,
    }


def verify_attestation(
    obj: object,
    issuer_public_key: Ed25519PublicKey | None,
    own_node_key_b64: str,
) -> dict[str, Any] | None:
    """Validate AND verify one attestation handed to this node:
    shape, the pool signature over the canonical bytes, and that it
    names THIS node's key (compared on the raw bytes, so encoding
    variants cannot smuggle a foreign key past the check). Returns
    the normalized attestation dict, or None for ANY failure — a
    missing issuer key included."""
    parsed = validate_attestation_shape(obj)
    if parsed is None or issuer_public_key is None:
        return None
    own_raw = _decode_b64url(own_node_key_b64, 32)
    if own_raw is None or _decode_b64url(parsed["node_key"], 32) != own_raw:
        return None
    signature = _decode_b64url(parsed["sig"], 64)
    if signature is None:
        return None
    try:
        issuer_public_key.verify(
            signature,
            attestation_signing_text(
                parsed["issuer"], parsed["node_key"], parsed["bound_at"],
                parsed["issued_at"], parsed["expires"]),
        )
    except InvalidSignature:
        return None
    except Exception:  # noqa: BLE001 - any crypto failure drops it
        return None
    return parsed

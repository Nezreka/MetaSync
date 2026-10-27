"""SIN access-token verification, a byte-level mirror of verifyAccessToken
in src/sin/logic.ts.

Token shape (PORTAL-DESIGN section 3A.4): a compact 3-part base64url JWS,
header {"alg": "EdDSA", "typ": "SIN"}, claims exactly the set
{key_id, node_id, scopes, iat, exp} signed by the pool key. Every failure
returns None so the caller collapses all of them into one rejection: no
oracle about which check failed.

Parity notes with the TypeScript original:
- the signature segment is decoded with the strict decoder (canonical
  base64url only, exactly 64 bytes), like decodeBase64Url(part, 64);
- header and payload are decoded leniently for JSON parsing, exactly as
  the original does with Buffer.from(part, "base64url"); the signature
  covers the literal segments, so non-canonical encodings cannot verify
  unless the pool itself signed them;
- header alg/typ values are checked, extra header keys are tolerated;
- claims fields are type-checked; exp/iat must be safe integers
  (booleans are not numbers), iat must not exceed exp, exp must be in
  the future.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_B64URL_SHAPE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_MAX_SAFE_INTEGER = 2**53 - 1


def decode_base64url(value: object, expected_length: int | None = None) -> bytes | None:
    """Strict base64url decode, mirroring decodeBase64Url in logic.ts:
    canonical form only (input must re-encode to itself, unpadded)."""
    if not isinstance(value, str) or not _B64URL_SHAPE.match(value):
        return None
    unpadded = value.rstrip("=")
    if len(unpadded) % 4 == 1:
        return None
    try:
        raw = base64.urlsafe_b64decode(unpadded + "=" * (-len(unpadded) % 4))
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != unpadded:
        return None
    if expected_length is not None and len(raw) != expected_length:
        return None
    return raw


def encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_segment_lenient(part: str) -> bytes | None:
    """Lenient base64url decode for the JSON segments, mirroring
    Buffer.from(part, "base64url") in the original verifier."""
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError):
        return None


def parse_pool_public_key(value: str) -> Ed25519PublicKey | None:
    """Parse RELAY_POOL_PUBLIC_KEY: base64url of the raw 32-byte Ed25519
    public key (the form portal_keys and the SIN key endpoints use), or a
    PEM public key. Returns None when neither parses."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("-----BEGIN"):
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            key = load_pem_public_key(value.encode("utf-8"))
        except Exception:  # noqa: BLE001 - any parse failure means not a key
            return None
        return key if isinstance(key, Ed25519PublicKey) else None
    raw = decode_base64url(value, 32)
    if raw is None:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return None


def node_id_for_public_key(public_key_raw: bytes) -> str:
    """node_id (section 3A.2): SHA-256 hex of the raw Ed25519 public key."""
    return hashlib.sha256(public_key_raw).hexdigest()


@dataclass(frozen=True)
class TokenClaims:
    key_id: str
    node_id: str
    scopes: tuple[str, ...]
    iat: int
    exp: int


def _is_safe_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= _MAX_SAFE_INTEGER
    if isinstance(value, float):
        return value.is_integer() and abs(value) <= _MAX_SAFE_INTEGER
    return False


def verify_access_token(
    token: object,
    pool_public_key: Ed25519PublicKey,
    now: float | None = None,
) -> TokenClaims | None:
    """Verify a compact SIN access token against the pool public key.
    Returns the claims, or None for any failure (no oracle)."""
    if now is None:
        now = time.time()
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_part, payload_part, signature_part = parts
    signature = decode_base64url(signature_part, 64)
    if signature is None:
        return None
    header_raw = _decode_segment_lenient(header_part)
    payload_raw = _decode_segment_lenient(payload_part)
    if header_raw is None or payload_raw is None:
        return None
    try:
        header = json.loads(header_raw)
        payload = json.loads(payload_raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    if header.get("alg") != "EdDSA" or header.get("typ") != "SIN":
        return None
    try:
        pool_public_key.verify(
            signature, f"{header_part}.{payload_part}".encode()
        )
    except InvalidSignature:
        return None
    except Exception:  # noqa: BLE001 - mirror: every verify failure is None
        return None
    if not isinstance(payload, dict):
        return None
    key_id = payload.get("key_id")
    node_id = payload.get("node_id")
    scopes = payload.get("scopes")
    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(key_id, str) or not key_id:
        return None
    if not isinstance(node_id, str) or not node_id:
        return None
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        return None
    if not _is_safe_integer(exp) or not _is_safe_integer(iat):
        return None
    if int(iat) > int(exp):
        return None
    if int(exp) <= int(now):
        return None
    return TokenClaims(
        key_id=key_id,
        node_id=node_id,
        scopes=tuple(scopes),
        iat=int(iat),
        exp=int(exp),
    )

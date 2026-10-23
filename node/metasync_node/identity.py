"""Persistent node identity and SIN request signing.

On first run a random 32-byte root key is generated and stored in the
node's OWN state dir (never in SoulSync, never sent anywhere). It has
three separate jobs:

* derive the rotating weekly dedupe token,
  ``token(week) = sha256("<node_key>:<week>")``;
* derive, through HKDF-SHA256 with a domain-separated info label, the
  Ed25519 seed used to sign pool requests; and
* identify this node by the public key derived from that seed.

The root key never leaves the machine. Only the Ed25519 public key and
its SHA-256-derived ``node_id`` are public.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_KEY_SHAPE = re.compile(r"^[0-9a-f]{64}$")
_SIGNING_INFO = b"metasync-sin-request-signing-v1"


def b64url_encode(data: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decode padded or unpadded base64url."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_request_string(method: str, path: str, timestamp: str | int, nonce: str, body: bytes) -> str:
    """Return the exact version-1 canonical string signed for a pool request.

    The format is deliberately only what the MetaSync/SIN contract names:
    method, path, Unix timestamp, nonce, and the lowercase hex SHA-256 of
    the raw request body, separated by newlines.
    """
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = bytes(body)
    return "\n".join((
        method.upper(),
        path,
        str(timestamp),
        nonce,
        hashlib.sha256(body_bytes).hexdigest(),
    ))


@dataclass(frozen=True)
class NodeIdentity:
    key: str  # hex root secret; never leaves the machine

    @property
    def _root_key_bytes(self) -> bytes:
        return bytes.fromhex(self.key)

    @property
    def signing_seed(self) -> bytes:
        """Domain-separated Ed25519 seed derived from the root identity."""
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_SIGNING_INFO,
        )
        return hkdf.derive(self._root_key_bytes)

    @property
    def signing_private_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.signing_seed)

    @property
    def signing_public_key(self):
        return self.signing_private_key.public_key()

    @property
    def public_key_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.signing_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def public_key(self) -> str:
        """Ed25519 public key, unpadded base64url."""
        return b64url_encode(self.public_key_bytes)

    # A descriptive alias; ``public_key`` is the shortest API used by the
    # pool client and pairing responses.
    @property
    def public_key_b64url(self) -> str:
        return self.public_key

    @property
    def node_id(self) -> str:
        """Stable public node id: SHA-256 of the raw Ed25519 public key."""
        return hashlib.sha256(self.public_key_bytes).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Short public fingerprint shown for human comparison."""
        return self.node_id[:12]

    def sign(self, message: str | bytes) -> str:
        """Sign bytes/text and return the signature as unpadded base64url."""
        data = message.encode("utf-8") if isinstance(message, str) else bytes(message)
        return b64url_encode(self.signing_private_key.sign(data))

    def sign_request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        timestamp: str | int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        """Return the four node-signature headers for one pool request."""
        timestamp_value = str(int(time.time()) if timestamp is None else timestamp)
        nonce_value = nonce or b64url_encode(secrets.token_bytes(16))
        canonical = canonical_request_string(method, path, timestamp_value, nonce_value, body)
        return {
            "X-Node-Id": self.node_id,
            "X-Node-Signature": self.sign(canonical),
            "X-Node-Timestamp": timestamp_value,
            "X-Node-Nonce": nonce_value,
        }

    # Backwards-friendly name used by callers that describe these as the
    # complete signed header set rather than a signature alone.
    def signed_headers(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        timestamp: str | int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        return self.sign_request(method, path, body, timestamp=timestamp, nonce=nonce)


def load_or_create_identity(state_dir: str | Path) -> NodeIdentity:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    file = state_dir / "node_key"
    if file.exists():
        key = file.read_text(encoding="utf-8").strip()
        if not _KEY_SHAPE.match(key):
            raise ValueError(f"node_key file at {file} is corrupt (expected 64 hex chars)")
        return NodeIdentity(key=key)
    key = secrets.token_hex(32)
    file.write_text(key + "\n", encoding="utf-8")
    os.chmod(file, 0o600)
    return NodeIdentity(key=key)


def token_for_week(identity: NodeIdentity, week: str) -> str:
    return hashlib.sha256(f"{identity.key}:{week}".encode()).hexdigest()

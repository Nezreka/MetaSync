"""Node identity — an Ed25519 keypair that signs this node's observations.

The public key is the only thing that identifies a node on the network. It is
deliberately NOT tied to a SoulSync profile, username, or install id: the point
of corroboration is counting distinct independent observers, not knowing who
they are. See docs/privacy.md for the rotation story.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def b64e(raw: bytes) -> str:
    """Unpadded URL-safe base64 — the only binary encoding on the wire."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class NodeIdentity:
    """This node's signing key."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key
        self.public_key = private_key.public_key()

    @property
    def node_id(self) -> str:
        """Base64 public key — the value that appears in a claim's ``node`` field."""
        return b64e(public_key_bytes(self.public_key))

    def sign(self, message: bytes) -> str:
        return b64e(self._private.sign(message))

    @classmethod
    def generate(cls) -> "NodeIdentity":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "NodeIdentity":
        """Read the node key from ``path``, creating it 0600 if absent."""
        path = Path(path)
        if path.exists():
            return cls(Ed25519PrivateKey.from_private_bytes(b64d(path.read_text().strip())))
        identity = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions *before* writing the secret.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as handle:
            handle.write(b64e(identity.private_bytes()))
        return identity

    def private_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization
        return self._private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )


def public_key_bytes(key: Ed25519PublicKey) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def public_key_from_id(node_id: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b64d(node_id))

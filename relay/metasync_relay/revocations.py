"""The issuer-signed revocation list (DESIGN-METADATA.md sections 9/10).

The list names node keys — revoked, unbound, and deleted — and nothing
else: key status only, never an account. It is signed by the issuer
(the pool key), so the relay transports it and cannot forge or edit
it, and nodes verify it offline against the issuer public key. The
relay's own copy is fetched from a configured issuer URL on an
interval; when no URL is configured the relay's list is empty and it
says so (welcome carries no list), which is honest v1 behaviour for a
relay whose operator has not wired the feed.

The canonical signing form is a fixed text block (the house style of
the hello signature): a context line, then issuer, sequence, issue
time, and the sorted node ids, one per line. The node package carries
a mirror of this form (node/metasync_node/revocations.py); the two
must never drift, and both sides' conformance tests pin the same
literal bytes (relay/tests/test_conformance.py,
node/tests/test_meta_conformance.py).

Sequences are per issuer and monotonic: a list is applied only when
its sequence exceeds the highest seen from that issuer, so anyone —
a third-party relay included — can withhold a newer list but can
never roll one back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .tokens import decode_base64url

SIGNING_CONTEXT = "metasync-revocations"
_NODE_ID_SHAPE = re.compile(r"^[0-9a-f]{64}$")
_MAX_KEYS = 100_000


def list_signing_text(issuer: str, seq: int, issued_at: int, keys) -> bytes:
    """The canonical bytes the issuer signs for one list."""
    lines = [
        SIGNING_CONTEXT,
        issuer,
        str(seq),
        str(issued_at),
        *sorted(set(keys)),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class RevocationList:
    issuer: str
    seq: int
    issued_at: int
    keys: frozenset
    sig: str  # base64url Ed25519 signature over list_signing_text

    def to_message(self) -> dict:
        """The wire form: a `revocations` message / welcome field."""
        return {
            "issuer": self.issuer,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "keys": sorted(self.keys),
            "sig": self.sig,
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_revocation_list(obj: object, issuer_public_key: Ed25519PublicKey,
                          expected_issuer: str | None = None) -> RevocationList | None:
    """Validate and verify one list object. Returns None for ANY
    failure — an unsigned or badly signed list is no list at all
    (section 10), and the caller keeps whatever it already holds."""
    if not isinstance(obj, dict):
        return None
    issuer = obj.get("issuer")
    seq = obj.get("seq")
    issued_at = obj.get("issued_at")
    keys = obj.get("keys")
    sig = obj.get("sig")
    if not isinstance(issuer, str) or not issuer:
        return None
    if expected_issuer is not None and issuer != expected_issuer:
        return None
    if not _is_int(seq) or seq < 0:
        return None
    if not _is_int(issued_at) or issued_at < 0:
        return None
    if (
        not isinstance(keys, list)
        or len(keys) > _MAX_KEYS
        or not all(isinstance(k, str) and _NODE_ID_SHAPE.match(k) for k in keys)
    ):
        return None
    signature = decode_base64url(sig, 64)
    if signature is None:
        return None
    try:
        issuer_public_key.verify(
            signature, list_signing_text(issuer, seq, issued_at, keys)
        )
    except InvalidSignature:
        return None
    except Exception:  # noqa: BLE001 - any crypto failure rejects the list
        return None
    return RevocationList(
        issuer=issuer,
        seq=int(seq),
        issued_at=int(issued_at),
        keys=frozenset(keys),
        sig=sig,  # type: ignore[arg-type]
    )


class RevocationFeed:
    """The relay's held copy of the revocation list, refreshed from the
    issuer. ``fetch`` returns the raw response body bytes for the
    configured URL; it is injectable so tests never touch a network,
    and the default fetcher is a plain HTTPS GET with a short timeout.
    """

    def __init__(
        self,
        issuer: str,
        issuer_public_key: Ed25519PublicKey,
        url: str = "",
        fetch=None,
    ) -> None:
        self.issuer = issuer
        self._key = issuer_public_key
        self.url = url
        self._fetch = fetch or self._default_fetch
        self.current: RevocationList | None = None

    def _default_fetch(self, url: str) -> bytes:
        import urllib.request

        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(4 * 1024 * 1024)

    def refresh(self) -> bool:
        """Fetch and apply a newer list. Returns True when the held
        list changed. Any failure — network, shape, signature, stale
        sequence — keeps the current list and returns False."""
        if not self.url:
            return False
        try:
            raw = self._fetch(self.url)
            obj = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - a bad feed is an unchanged feed
            return False
        return self.apply(obj)

    def apply(self, obj: object) -> bool:
        """Verify and apply one list object (from the feed, or handed
        in directly). Sequence-gated per issuer."""
        parsed = parse_revocation_list(obj, self._key, self.issuer or None)
        if parsed is None:
            return False
        if self.current is not None and parsed.seq <= self.current.seq:
            return False
        self.current = parsed
        return True


def signed_list_message(issuer: str, seq: int, issued_at: int, keys,
                        sig: str) -> dict:
    """Assemble a wire list object from parts (issuer/test tooling)."""
    return {
        "issuer": issuer,
        "seq": seq,
        "issued_at": issued_at,
        "keys": sorted(set(keys)),
        "sig": sig,
    }


__all__ = [
    "RevocationFeed",
    "RevocationList",
    "list_signing_text",
    "parse_revocation_list",
    "signed_list_message",
]

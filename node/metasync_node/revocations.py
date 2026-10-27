"""The issuer-signed revocation list, node side (DESIGN-METADATA.md
sections 9/10).

Mirror of relay/metasync_relay/revocations.py: the two packages are
separate deployments and cannot import each other, so the canonical
signing form is duplicated here — the context line
``metasync-revocations`` then issuer, sequence, issue time, and the
sorted node ids, one per line. The two must never drift; both sides'
conformance tests pin the same literal bytes
(node/tests/test_meta_conformance.py,
relay/tests/test_conformance.py).

A list is applied only when it verifies against the issuer public key
AND its sequence exceeds the highest seen from that issuer: anyone
transporting a list — a relay included — can withhold a newer one but
can never roll one back. The highest sequence seen is DURABLE, and
so is the signed list that earned it (both kept in a small JSON file
in the node's state dir when a path is given): an in-memory-only
mark would hand a withholding relay the rollback the sequence exists
to prevent the moment the node restarts or the network is toggled
off and on (each builds a fresh client) — and a mark WITHOUT its
list would leave enforcement at zero after a restart until some
channel re-delivers one, which a withholding relay can delay
indefinitely (gate re-pass S-a). Persisted lists are re-verified
against the issuer key before they enforce again — via the
constructor's key provider when one is wired, otherwise under the
key the next apply carries; a list that does not verify is dropped,
while its mark stays. Without a configured issuer key there is
nothing to verify against, and every list is refused: an
unverifiable list is no list at all (section 10).

Enforcement lives with the holders of this store: the meta store
refuses a revoked origin's batches at admission, filters them out
of re-serving, and purges them from the cache when a new list lands
(meta/store.py); the service wires ONE store through the relay
client, the pool clients, and the meta store so every delivery
channel — welcome, push, and the token exchange (§10: the channel
a withholding relay does not sit on) — feeds the same copy.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .identity import b64url_decode

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


def _decode_b64url(value: object, expected_length: int) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        raw = b64url_decode(value)
    except Exception:  # noqa: BLE001 - malformed input is a refusal
        return None
    return raw if len(raw) == expected_length else None


def parse_issuer_public_key(value: object) -> Ed25519PublicKey | None:
    """Parse an issuer public key given as base64url of the raw 32
    bytes (the portal_keys form) or a PEM block."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("-----BEGIN"):
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_public_key,
            )

            key = load_pem_public_key(value.encode("utf-8"))
        except Exception:  # noqa: BLE001 - any parse failure means not a key
            return None
        return key if isinstance(key, Ed25519PublicKey) else None
    raw = _decode_b64url(value, 32)
    if raw is None:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class RevocationList:
    issuer: str
    seq: int
    issued_at: int
    keys: frozenset
    sig: str

    def to_dict(self) -> dict:
        return {
            "issuer": self.issuer,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "keys": sorted(self.keys),
            "sig": self.sig,
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def verify_revocation_list(
    obj: object, issuer_public_key: Ed25519PublicKey | None
) -> RevocationList | None:
    """Validate and verify one list object against the issuer key.
    Returns None for ANY failure, a missing key included."""
    if issuer_public_key is None or not isinstance(obj, dict):
        return None
    issuer = obj.get("issuer")
    seq = obj.get("seq")
    issued_at = obj.get("issued_at")
    keys = obj.get("keys")
    sig = obj.get("sig")
    if not isinstance(issuer, str) or not issuer:
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
    signature = _decode_b64url(sig, 64)
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


class RevocationStore:
    """The node's held lists: the highest-sequence verified list per
    issuer. Thread-safe; fed from welcome messages, pushed
    ``revocations`` messages, and token exchanges (§10).

    ``persist_path`` keeps the per-issuer HIGH-WATER MARKS and the
    signed LISTS that earned them in a JSON file, reloaded at
    construction. The lists are the issuer's public signed
    statements — the node ids one names are exactly what it exists
    to publish; there is no key material in the file, the node's
    own or anyone's. On load the marks become the floor any
    delivery has to beat, and each list is re-verified against the
    issuer key before it enforces — via ``issuer_key_provider``
    when one is wired, otherwise deferred, still pending, until
    the first key-bearing apply. A persisted list that fails
    verification is dropped and never enforces; its mark remains,
    so dropping it cannot reopen the rollback."""

    def __init__(
        self,
        persist_path: str | Path | None = None,
        *,
        issuer_key_provider: (
            Callable[[], Ed25519PublicKey | None] | None) = None,
    ) -> None:
        self._lock = threading.Lock()
        self._by_issuer: dict[str, RevocationList] = {}
        self._high_water: dict[str, int] = {}
        self._pending_lists: dict[str, Any] = {}
        self._persist_path = Path(persist_path) if persist_path else None
        if self._persist_path is not None:
            self._load_persisted()
            if issuer_key_provider is not None:
                try:
                    key = issuer_key_provider()
                except Exception:  # noqa: BLE001 - broken provider defers
                    key = None
                if key is not None:
                    with self._lock:
                        self._restore_pending_locked(key)

    def _load_persisted(self) -> None:
        try:
            raw = json.loads(self._persist_path.read_text("utf-8"))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - missing/corrupt reads as "none seen"
            return
        if not isinstance(raw, dict):
            return
        marks: Any = raw
        lists: Any = None
        if "marks" in raw or "lists" in raw:
            marks = raw.get("marks")
            lists = raw.get("lists")
        # Otherwise the file IS the marks mapping ({issuer: seq}):
        # the legacy shape, written before lists were persisted.
        if isinstance(marks, dict):
            for issuer, seq in marks.items():
                if isinstance(issuer, str) and isinstance(seq, int) \
                        and not isinstance(seq, bool) and seq >= 0:
                    self._high_water[issuer] = seq
        if isinstance(lists, dict):
            for issuer, obj in lists.items():
                if isinstance(issuer, str) and isinstance(obj, dict):
                    self._pending_lists[issuer] = obj

    def _restore_pending_locked(
            self, issuer_public_key: Ed25519PublicKey) -> None:
        """Re-verify the lists reloaded from disk against the issuer
        key and install the ones that pass. Caller MUST hold the
        lock. A persisted list is a claim, not a fact: it enforces
        only after its signature checks out, its sequence reaches
        the durable floor, and no newer list is already held. One
        that fails any of that is discarded — a list signed by
        another key can never become valid under this one — while
        its high-water mark stays either way. With no key yet the
        lists stay pending untouched: the first key-bearing apply
        restores them before its own comparisons run."""
        for issuer, obj in list(self._pending_lists.items()):
            parsed = verify_revocation_list(obj, issuer_public_key)
            del self._pending_lists[issuer]
            if parsed is None or parsed.issuer != issuer:
                continue
            if parsed.seq < self._high_water.get(issuer, -1):
                continue
            held = self._by_issuer.get(issuer)
            if held is not None and parsed.seq <= held.seq:
                continue
            self._by_issuer[issuer] = parsed
            if parsed.seq > self._high_water.get(issuer, -1):
                self._high_water[issuer] = parsed.seq

    def _save_persisted(self) -> None:
        """Persist marks and held lists atomically. A failed write
        must not fail the apply that earned them — enforcement
        continues on the in-memory copies; only the restart-proofing
        degrades."""
        if self._persist_path is None:
            return
        try:
            payload = {
                "marks": dict(self._high_water),
                "lists": {
                    issuer: lst.to_dict()
                    for issuer, lst in self._by_issuer.items()
                },
            }
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True), "utf-8")
            os.replace(tmp, self._persist_path)
        except Exception:  # noqa: BLE001, S110 - see docstring
            pass

    def apply(
        self, obj: object, issuer_public_key: Ed25519PublicKey | None
    ) -> RevocationList | None:
        """Verify and apply one list object. Returns the applied list,
        or None when it was refused (unverifiable, malformed, or not
        newer than the held list — or the durable high-water mark —
        for its issuer). A list whose sequence EQUALS the high-water
        mark is accepted when no list is currently held: that is a
        restart re-delivering the current list, not a rollback."""
        parsed = verify_revocation_list(obj, issuer_public_key)
        if parsed is None:
            return None
        with self._lock:
            # A store restarted since its lists were persisted
            # restores them under this key BEFORE judging the new
            # arrival: the restored list is the held list the
            # sequence comparisons below are defined against, so a
            # re-delivery older than the persisted one is refused
            # against it, not merely against the bare mark.
            self._restore_pending_locked(issuer_public_key)
            held = self._by_issuer.get(parsed.issuer)
            if parsed.seq < self._high_water.get(parsed.issuer, -1):
                return None
            if held is not None and parsed.seq <= held.seq:
                return None
            self._by_issuer[parsed.issuer] = parsed
            if parsed.seq > self._high_water.get(parsed.issuer, -1):
                self._high_water[parsed.issuer] = parsed.seq
            self._save_persisted()
            return parsed

    def current(self, issuer: str | None = None) -> RevocationList | None:
        """The held list for one issuer, or — with a single issuer
        held, the v1 shape — that list when no issuer is named."""
        with self._lock:
            if issuer is not None:
                return self._by_issuer.get(issuer)
            if len(self._by_issuer) == 1:
                return next(iter(self._by_issuer.values()))
            return None

    def is_revoked(self, node_id: str, issuer: str | None = None) -> bool:
        """Is this node id named on a held list? Without an issuer,
        any held list naming the key revokes it — that union is the
        only form admission and the re-serve filter may use,
        because the issuer a batch CLAIMS (its header attestation)
        is unverified at receipt, and a revoked key would name a
        made-up one to escape (gate re-pass N1). The issuer-scoped
        form is for a caller holding a VERIFIED issuer — an
        attestation whose pool signature checked out against the
        configured key and whose issuer matches that key's id —
        and answers False when no list is held for it: §10 treats
        a batch whose (verified) issuer the node holds no list for
        as unrevoked for now, because a foreign list's staleness
        bound is a federation precondition, not a v1 mechanism."""
        with self._lock:
            if issuer is not None:
                held = self._by_issuer.get(issuer)
                return held is not None and node_id in held.keys
            return any(node_id in lst.keys for lst in self._by_issuer.values())

    def summary(self) -> dict:
        """Counts and sequences only, for status surfaces."""
        with self._lock:
            return {
                "issuers": {
                    issuer: {"seq": lst.seq, "keys": len(lst.keys)}
                    for issuer, lst in sorted(self._by_issuer.items())
                }
            }

"""Claim packs — a file of signed claims, handed from one node to another.

A pack is gzipped JSON Lines: a header, then one signed claim per line. It needs
no transport, no discovery and no trust in whoever handed it over, because the
signatures travel with the claims and are checked on import. Two people swapping
a file is a complete, if manual, instance of the network.

Packs are never re-signed. A relayed claim keeps its original observer's key and
signature, so passing a pack through ten hands still counts as one observation —
which is what keeps corroboration meaningful.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from .claim import InvalidClaim, SignedClaim
from .store import ORIGIN_LOCAL, ORIGIN_PEER, ClaimStore

FORMAT = "metasync-pack/1"


class BadPack(ValueError):
    """The file is not a readable MetaSync pack."""


@dataclass
class PackStats:
    claims: int = 0
    entities: Dict[str, int] = field(default_factory=dict)
    observers: int = 0
    bytes_written: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"claims": self.claims, "entities": dict(sorted(self.entities.items())),
                "observers": self.observers, "bytes": self.bytes_written}


@dataclass
class ImportStats:
    """What a pack taught this node — the number that matters is ``new_ids``."""

    lines: int = 0
    accepted: int = 0
    rejected_signature: int = 0
    rejected_invalid: int = 0
    superseded: int = 0            # an older claim from a node we already had
    new_keys: int = 0              # entities this node had never seen
    new_ids: int = 0               # (key, provider, value) triples that are new
    corroborated_ids: int = 0      # ids already held, now with another observer
    own_claims: int = 0            # our own claims handed back to us
    reasons: Dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        if self.reasons:
            out["reasons"] = dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])[:10])
        return out


def write_pack(store: ClaimStore, path: Path | str, origin: Optional[str] = ORIGIN_LOCAL,
               entity: Optional[str] = None) -> PackStats:
    """Write claims to a pack file.

    ``origin=ORIGIN_LOCAL`` (the default) packs only what this node observed
    itself. Pass ``origin=None`` to relay peer claims onward too — useful for
    propagation, and safe, because relayed claims keep their original signature.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = PackStats()

    where, params = [], []
    if origin:
        where.append("origin = ?")
        params.append(origin)
    if entity:
        where.append("entity = ?")
        params.append(entity)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = store._conn.execute(
        f"SELECT payload, node, sig, entity FROM claims {clause} ORDER BY entity, key", params)

    # Written to a temp file first: a half-written pack that looks complete is
    # worse than no pack, since the reader cannot tell.
    temp = path.with_suffix(path.suffix + ".partial")
    observers = set()
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "format": FORMAT,
            "created_on": datetime.now(timezone.utc).date().isoformat(),
        }) + "\n")
        for row in rows:
            handle.write(json.dumps({
                "claim": json.loads(row["payload"]), "node": row["node"], "sig": row["sig"],
            }, sort_keys=True) + "\n")
            stats.claims += 1
            stats.entities[row["entity"]] = stats.entities.get(row["entity"], 0) + 1
            observers.add(row["node"])

    stats.observers = len(observers)
    temp.replace(path)
    stats.bytes_written = path.stat().st_size
    return stats


def read_pack(path: Path | str) -> Iterator[SignedClaim]:
    """Yield signed claims from a pack. Does not verify — ``import_pack`` does."""
    path = Path(path)
    # gzip.open is lazy — a file that is not gzip raises on the first read, not
    # here — so the decode guard has to wrap the reads, not just the open.
    try:
        handle = gzip.open(path, "rt", encoding="utf-8")
    except OSError as exc:
        raise BadPack(f"cannot open {path}: {exc}") from exc

    with handle:
        try:
            first = handle.readline()
        except (OSError, UnicodeDecodeError) as exc:
            raise BadPack(f"{path} is not a gzipped pack: {exc}") from exc
        if not first:
            raise BadPack("pack is empty")
        try:
            header = json.loads(first)
        except json.JSONDecodeError as exc:
            raise BadPack("pack header is not JSON") from exc
        if header.get("format") != FORMAT:
            raise BadPack(f"unsupported pack format {header.get('format')!r}")

        number = 1
        while True:
            try:
                line = handle.readline()
            except (OSError, UnicodeDecodeError) as exc:
                raise BadPack(f"{path} is truncated or corrupt: {exc}") from exc
            if not line:
                return
            number += 1
            line = line.strip()
            if not line:
                continue
            try:
                yield SignedClaim.from_wire(json.loads(line))
            except (json.JSONDecodeError, InvalidClaim) as exc:
                raise BadPack(f"line {number}: {exc}") from exc


def import_pack(store: ClaimStore, path: Path | str,
                own_node: Optional[str] = None) -> ImportStats:
    """Verify and merge a pack, reporting what it actually added.

    Every claim's signature is checked against the key that signed it and its
    key is re-derived from its own normalized parts, so a pack from a stranger
    needs no more trust than a pack from a friend.
    """
    stats = ImportStats()
    conn = store._conn

    for signed in read_pack(path):
        stats.lines += 1
        try:
            signed.verify()
        except InvalidClaim as exc:
            message = str(exc)
            if "signature" in message:
                stats.rejected_signature += 1
            else:
                stats.rejected_invalid += 1
            stats.note(message[:70])
            continue

        payload = signed.payload
        entity, key = payload["entity"], payload["key"]
        if own_node and signed.node == own_node:
            stats.own_claims += 1

        had_key = conn.execute(
            "SELECT 1 FROM claims WHERE entity = ? AND key = ? LIMIT 1", (entity, key)
        ).fetchone() is not None

        # Measured before the write, or every id would look already-known.
        new_ids = corroborated = 0
        for provider, values in (payload.get("ids") or {}).items():
            for value in values:
                seen = conn.execute(
                    "SELECT 1 FROM claim_ids WHERE entity = ? AND key = ? AND provider = ? "
                    "AND value = ? LIMIT 1", (entity, key, provider, value)
                ).fetchone()
                if seen:
                    corroborated += 1
                else:
                    new_ids += 1

        if not store.put(signed, origin=ORIGIN_PEER):
            stats.superseded += 1
            continue

        stats.accepted += 1
        stats.new_ids += new_ids
        stats.corroborated_ids += corroborated
        if not had_key:
            stats.new_keys += 1

    return stats

"""Seed nodes — claims derived from a public corpus rather than from a library.

A seed is how the network gets useful before it has users. MusicBrainz publishes
its full database under CC0, including the ISRC→recording mapping; a library
that already carries ISRCs (SoulSync's does, on 97% of tracks) can turn that
into recording MBIDs for the half of its tracks that never got matched.

Seed claims are signed by their own node key and carry ``src="dataset"``. They
are deliberately NOT counted as independent observation: a corpus is one source
however many nodes pass it on, and if it accumulated support like a real library
would, a single dump could outvote every person on the network. ``resolve()``
reports the two separately for exactly this reason.

The ISRC join is exact — no fuzzy matching, no name normalization, no judgement
call. Either the recording carries that ISRC in MusicBrainz or it does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .claim import SRC_DATASET, Claim
from .identity import NodeIdentity
from .importer import today
from .schema import MAX_IDS_PER_PROVIDER
from .store import ORIGIN_PEER, ClaimStore

# mbdump/isrc columns: id, recording, isrc, edits_pending, created
ISRC_RECORDING_COLUMN = 1
ISRC_VALUE_COLUMN = 2
# mbdump/recording columns: id, gid, name, artist_credit, length, comment, ...
RECORDING_ID_COLUMN = 0
RECORDING_GID_COLUMN = 1


@dataclass
class SeedStats:
    wanted_isrcs: int = 0
    isrc_rows_scanned: int = 0
    isrc_matched: int = 0
    recording_rows_scanned: int = 0
    recordings_resolved: int = 0
    claims: int = 0
    keys_filled: int = 0
    already_known: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        if self.reasons:
            out["reasons"] = dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])[:8])
        return out


def _tsv(path: Path) -> Iterator[List[str]]:
    """MusicBrainz dumps are tab-separated with \\N for NULL and no header."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n").split("\t")


def wanted_isrcs(store: ClaimStore, only_missing: bool = True) -> Tuple[Dict[str, List[str]], Dict[str, dict]]:
    """ISRCs this node holds, mapped to the track keys carrying them.

    ``only_missing`` restricts the work to keys with no MusicBrainz id yet,
    which is the whole point of the exercise — a seed that re-asserts what the
    library already resolved teaches nothing and only inflates the store.
    """
    rows = store._conn.execute(
        "SELECT DISTINCT key, value FROM claim_ids "
        "WHERE entity = 'track' AND provider = 'isrc'").fetchall()

    known_mb: Set[str] = set()
    if only_missing:
        known_mb = {r["key"] for r in store._conn.execute(
            "SELECT DISTINCT key FROM claim_ids "
            "WHERE entity = 'track' AND provider = 'musicbrainz'")}

    by_isrc: Dict[str, List[str]] = {}
    keys: Set[str] = set()
    for row in rows:
        if row["key"] in known_mb:
            continue
        by_isrc.setdefault(row["value"].strip().upper(), []).append(row["key"])
        keys.add(row["key"])

    # The normalized parts each claim will need, so the seed's keys derive
    # correctly rather than being asserted.
    norms: Dict[str, dict] = {}
    for row in store._conn.execute(
            "SELECT key, payload FROM claims WHERE entity = 'track'"):
        if row["key"] in keys and row["key"] not in norms:
            norms[row["key"]] = json.loads(row["payload"]).get("norm", {})
    return by_isrc, norms


def build(store: ClaimStore, isrc_path: Path | str, recording_path: Path | str,
          identity: NodeIdentity, only_missing: bool = True,
          progress=None) -> SeedStats:
    """Turn MusicBrainz's ISRC table into dataset claims for this node's gaps."""
    stats = SeedStats()
    by_isrc, norms = wanted_isrcs(store, only_missing=only_missing)
    stats.wanted_isrcs = len(by_isrc)
    if not by_isrc:
        return stats

    # Pass 1: MusicBrainz recording ids for the ISRCs we care about.
    recording_to_keys: Dict[str, Set[str]] = {}
    for fields in _tsv(Path(isrc_path)):
        stats.isrc_rows_scanned += 1
        if len(fields) <= ISRC_VALUE_COLUMN:
            continue
        keys = by_isrc.get(fields[ISRC_VALUE_COLUMN].strip().upper())
        if keys:
            stats.isrc_matched += 1
            recording_to_keys.setdefault(fields[ISRC_RECORDING_COLUMN], set()).update(keys)
        if progress and stats.isrc_rows_scanned % 1_000_000 == 0:
            progress("isrc", stats)

    # Pass 2: their MBIDs. Only the ids pass 1 selected are kept, so the 35M-row
    # recording table streams through without being held in memory.
    key_to_mbids: Dict[str, Set[str]] = {}
    for fields in _tsv(Path(recording_path)):
        stats.recording_rows_scanned += 1
        if len(fields) <= RECORDING_GID_COLUMN:
            continue
        keys = recording_to_keys.get(fields[RECORDING_ID_COLUMN])
        if keys:
            stats.recordings_resolved += 1
            for key in keys:
                key_to_mbids.setdefault(key, set()).add(fields[RECORDING_GID_COLUMN])
        if progress and stats.recording_rows_scanned % 2_000_000 == 0:
            progress("recording", stats)

    # Emit.
    for key, mbids in key_to_mbids.items():
        norm = norms.get(key)
        if not norm:
            stats.reasons["no norm for key"] = stats.reasons.get("no norm for key", 0) + 1
            continue
        claim = Claim(
            entity="track",
            norm=norm,
            ids={"musicbrainz": tuple(sorted(mbids)[:MAX_IDS_PER_PROVIDER])},
            observed_on=today(),
            src=SRC_DATASET,
        )
        if claim.key != key:
            # The stored key must re-derive from its own parts, or the claim
            # would be unverifiable by anyone receiving it.
            stats.reasons["key did not re-derive"] = stats.reasons.get("key did not re-derive", 0) + 1
            continue
        if store.put(claim.sign(identity), origin=ORIGIN_PEER):
            stats.claims += 1
            stats.keys_filled += 1

    return stats

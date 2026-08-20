"""Local claim store — what this node knows, and who told it.

One row per (node, entity, key): a node's latest word on an entity replaces its
previous word, but never another node's. That is the whole corroboration model —
``resolve()`` counts *distinct observers*, so a claim relayed a thousand times
still counts once, while two independent libraries agreeing counts twice.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .claim import SRC_DATASET, SRC_OBSERVED, Claim, SignedClaim

ORIGIN_LOCAL = "local"
ORIGIN_PEER = "peer"

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    node        TEXT NOT NULL,
    entity      TEXT NOT NULL,
    key         TEXT NOT NULL,
    song_key    TEXT,
    payload     TEXT NOT NULL,
    sig         TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    origin      TEXT NOT NULL,
    stored_at   TEXT NOT NULL,
    src         TEXT NOT NULL DEFAULT 'observed',
    PRIMARY KEY (node, entity, key)
);
CREATE INDEX IF NOT EXISTS idx_claims_entity_key ON claims(entity, key);
CREATE INDEX IF NOT EXISTS idx_claims_song_key ON claims(song_key) WHERE song_key IS NOT NULL;

-- Flattened ids, rebuilt from the payload on every write. Exists so that
-- "who else says this?" is one indexed query rather than a JSON scan.
CREATE TABLE IF NOT EXISTS claim_ids (
    entity   TEXT NOT NULL,
    key      TEXT NOT NULL,
    provider TEXT NOT NULL,
    value    TEXT NOT NULL,
    node     TEXT NOT NULL,
    src      TEXT NOT NULL DEFAULT 'observed',
    PRIMARY KEY (entity, key, provider, value, node)
);
CREATE INDEX IF NOT EXISTS idx_claim_ids_lookup ON claim_ids(entity, key);
CREATE INDEX IF NOT EXISTS idx_claim_ids_reverse ON claim_ids(provider, value);

CREATE TABLE IF NOT EXISTS sync_state (
    entity      TEXT PRIMARY KEY,
    cursor      TEXT NOT NULL DEFAULT '',
    last_synced TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClaimStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns a store created by an earlier version lacks. CREATE TABLE
        IF NOT EXISTS never reshapes an existing table, so new columns arrive
        here — the same probe-then-ALTER pattern SoulSync uses."""
        for table in ("claims", "claim_ids"):
            columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "src" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN src TEXT NOT NULL DEFAULT 'observed'")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ClaimStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── writing ──────────────────────────────────────────────────────────

    def put(self, signed: SignedClaim, origin: str = ORIGIN_PEER) -> bool:
        """Store one verified claim. Returns False if a newer one is already held.

        Callers must have verified the signature (``SignedClaim.verify()``);
        the store does not re-verify on every write, but ``verify_all()`` can
        re-check the whole database.
        """
        payload = dict(signed.payload)
        entity, key = payload["entity"], payload["key"]
        observed_on = payload["observed_on"]
        src = payload.get("src", SRC_OBSERVED)

        existing = self._conn.execute(
            "SELECT observed_on FROM claims WHERE node = ? AND entity = ? AND key = ?",
            (signed.node, entity, key),
        ).fetchone()
        if existing and existing["observed_on"] > observed_on:
            return False

        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO claims "
                "(node, entity, key, song_key, payload, sig, observed_on, origin, stored_at, src) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (signed.node, entity, key, payload.get("song_key"),
                 json.dumps(payload, sort_keys=True), signed.sig,
                 observed_on, origin, _now(), src),
            )
            # Rebuild this observer's ids for this key — an id the node has
            # since retracted must not linger.
            self._conn.execute(
                "DELETE FROM claim_ids WHERE entity = ? AND key = ? AND node = ?",
                (entity, key, signed.node),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO claim_ids (entity, key, provider, value, node, src) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(entity, key, provider, value, signed.node, src)
                 for provider, values in (payload.get("ids") or {}).items()
                 for value in values],
            )
        return True

    def put_many(self, signed_claims: Iterable[SignedClaim], origin: str = ORIGIN_PEER) -> int:
        return sum(1 for claim in signed_claims if self.put(claim, origin))

    # ── reading ──────────────────────────────────────────────────────────

    def get(self, entity: str, key: str) -> List[SignedClaim]:
        rows = self._conn.execute(
            "SELECT payload, node, sig FROM claims WHERE entity = ? AND key = ? ORDER BY node",
            (entity, key),
        ).fetchall()
        return [SignedClaim(payload=json.loads(r["payload"]), node=r["node"], sig=r["sig"])
                for r in rows]

    def resolve(self, entity: str, key: str, min_observers: int = 1) -> Dict[str, List[Dict[str, Any]]]:
        """The id map for one key, each id with its support.

        ``observers`` counts nodes whose own library held the entity;
        ``datasets`` counts public corpora that assert it. They are reported
        separately and never summed into one score: a dataset is one source
        however many nodes redistribute it, so letting it accumulate like
        independent observation would let one dump outvote every real person.

        A provider maps to a *list*: SoulSync's normalization collapses editions
        and remasters, so several ids can be correct under one key at once.
        Ordered by observer count, so the most corroborated reading is first.
        """
        # `mine` first, always. Node identities are free to generate, so an
        # attacker with a handful of keypairs can out-count a sparse honest
        # majority — the simulation puts it at roughly one fake identity per
        # three honest nodes. No amount of peer agreement should be able to
        # override what this machine observed in its own library, so local
        # observation is a separate, higher rank rather than one more vote.
        rows = self._conn.execute(
            "SELECT ci.provider AS provider, ci.value AS value, "
            "  MAX(CASE WHEN c.origin = ? THEN 1 ELSE 0 END) AS mine, "
            "  COUNT(DISTINCT CASE WHEN ci.src = ? THEN ci.node END) AS observers, "
            "  COUNT(DISTINCT CASE WHEN ci.src = ? THEN ci.node END) AS datasets "
            "FROM claim_ids ci "
            "JOIN claims c ON c.node = ci.node AND c.entity = ci.entity AND c.key = ci.key "
            "WHERE ci.entity = ? AND ci.key = ? "
            "GROUP BY ci.provider, ci.value HAVING (observers + datasets) >= ? "
            "ORDER BY ci.provider, mine DESC, observers DESC, datasets DESC, ci.value",
            (ORIGIN_LOCAL, SRC_OBSERVED, SRC_DATASET, entity, key, min_observers),
        ).fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            entry = {"value": row["value"], "observers": row["observers"]}
            if row["datasets"]:
                entry["datasets"] = row["datasets"]
            if row["mine"]:
                entry["mine"] = True
            out.setdefault(row["provider"], []).append(entry)
        return out

    def find_by_id(self, provider: str, value: str) -> List[Tuple[str, str]]:
        """Reverse lookup: which keys is this provider id claimed under?"""
        rows = self._conn.execute(
            "SELECT DISTINCT entity, key FROM claim_ids WHERE provider = ? AND value = ?",
            (provider, value),
        ).fetchall()
        return [(r["entity"], r["key"]) for r in rows]

    def verify_all(self) -> Tuple[int, List[str]]:
        """Re-verify every stored signature. Returns (checked, failures)."""
        from .claim import InvalidClaim
        checked, failures = 0, []
        for row in self._conn.execute("SELECT payload, node, sig FROM claims"):
            checked += 1
            try:
                SignedClaim(payload=json.loads(row["payload"]), node=row["node"],
                            sig=row["sig"]).verify()
            except InvalidClaim as exc:
                failures.append(f"{row['node'][:12]}… {exc}")
        return checked, failures

    def stats(self) -> Dict[str, Any]:
        counts = {
            row["entity"]: row["n"] for row in self._conn.execute(
                "SELECT entity, COUNT(*) AS n FROM claims GROUP BY entity")
        }
        return {
            "claims": dict(sorted(counts.items())),
            "total": sum(counts.values()),
            "observers": self._conn.execute(
                "SELECT COUNT(DISTINCT node) AS n FROM claims WHERE src = ?",
                (SRC_OBSERVED,)).fetchone()["n"],
            "dataset_sources": self._conn.execute(
                "SELECT COUNT(DISTINCT node) AS n FROM claims WHERE src = ?",
                (SRC_DATASET,)).fetchone()["n"],
            "distinct_keys": self._conn.execute(
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT entity, key FROM claims)"
            ).fetchone()["n"],
            # Two different numbers, and conflating them overstates what a
            # peer taught you: `ids` is distinct (key, provider, value) triples
            # — actual knowledge — while `id_observations` counts rows, which
            # rises by one every time another node corroborates an id you
            # already held.
            "ids": self._conn.execute(
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT entity, key, provider, value "
                "FROM claim_ids)").fetchone()["n"],
            "id_observations": self._conn.execute(
                "SELECT COUNT(*) AS n FROM claim_ids").fetchone()["n"],
        }

    # ── sync bookkeeping ─────────────────────────────────────────────────

    def get_cursor(self, entity: str) -> str:
        row = self._conn.execute(
            "SELECT cursor FROM sync_state WHERE entity = ?", (entity,)).fetchone()
        return row["cursor"] if row else ""

    def set_cursor(self, entity: str, cursor: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_state (entity, cursor, last_synced) VALUES (?, ?, ?) "
                "ON CONFLICT(entity) DO UPDATE SET cursor = excluded.cursor, "
                "last_synced = excluded.last_synced",
                (entity, cursor, _now()),
            )

    def get_last_synced(self, entity: str) -> str:
        row = self._conn.execute(
            "SELECT last_synced FROM sync_state WHERE entity = ?", (entity,)).fetchone()
        return row["last_synced"] if row else ""

    def reset_cursor(self, entity: Optional[str] = None) -> None:
        with self._conn:
            if entity:
                self._conn.execute("UPDATE sync_state SET cursor = '' WHERE entity = ?", (entity,))
            else:
                self._conn.execute("UPDATE sync_state SET cursor = ''")

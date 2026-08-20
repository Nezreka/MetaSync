"""Local claim store — what this node knows, and who told it.

One row per (node, entity, key): a node's latest word on an entity replaces its
previous word, but never another node's. That is the whole corroboration model —
``resolve()`` counts *distinct observers*, so a claim relayed a thousand times
still counts once, while two independent libraries agreeing counts twice.

**Claims are stored decomposed, not as payloads.** Every node claiming the same
entity repeats the same ``key``, ``norm`` and ``song_key``; only the ids, facts
and date differ. Keeping the shared half once per entity rather than once per
claim is the difference between ~5 GB and ~80 GB at a hundred peers. The
canonical bytes are rebuilt from the parts when a signature needs checking,
which is safe because ``Claim.to_payload()`` is deterministic — the same parts
always produce the same bytes, so a signature made against the original payload
verifies against the reconstruction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .claim import SRC_DATASET, SRC_OBSERVED, Claim, SignedClaim

ORIGIN_LOCAL = "local"
ORIGIN_PEER = "peer"

SCHEMA = """
-- The half of a claim every observer of an entity repeats verbatim.
CREATE TABLE IF NOT EXISTS entities (
    entity   TEXT NOT NULL,
    key      TEXT NOT NULL,
    song_key TEXT,
    norm     TEXT NOT NULL,
    key_path TEXT,
    PRIMARY KEY (entity, key)
);
CREATE INDEX IF NOT EXISTS idx_entities_song_key ON entities(song_key) WHERE song_key IS NOT NULL;

-- The half that is one observer's own: what it resolved, and when.
CREATE TABLE IF NOT EXISTS claims (
    node        TEXT NOT NULL,
    entity      TEXT NOT NULL,
    key         TEXT NOT NULL,
    facts       TEXT NOT NULL DEFAULT '{}',
    sig         TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    origin      TEXT NOT NULL,
    src         TEXT NOT NULL DEFAULT 'observed',
    stored_at   TEXT NOT NULL,
    PRIMARY KEY (node, entity, key)
);
CREATE INDEX IF NOT EXISTS idx_claims_entity_key ON claims(entity, key);

-- Flattened ids: "who else says this?" as one indexed query.
CREATE TABLE IF NOT EXISTS claim_ids (
    entity   TEXT NOT NULL,
    key      TEXT NOT NULL,
    provider TEXT NOT NULL,
    value    TEXT NOT NULL,
    node     TEXT NOT NULL,
    src      TEXT NOT NULL DEFAULT 'observed',
    PRIMARY KEY (entity, key, provider, value, node)
);
-- No (entity, key) index: that is a prefix of the primary key, which SQLite
-- already maintains as a unique index, so a second one stores the same 2M
-- entity/key pairs again for nothing.
CREATE INDEX IF NOT EXISTS idx_claim_ids_reverse ON claim_ids(provider, value);
CREATE INDEX IF NOT EXISTS idx_claim_ids_node ON claim_ids(node, entity, key);

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
        self.path = Path(path) if path != ":memory:" else path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate_from_payload_schema()
        self._conn.executescript(SCHEMA)
        self._migrate_columns()
        self._conn.commit()

    # ── schema upkeep ────────────────────────────────────────────────────

    def _table_columns(self, table: str) -> set:
        return {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_columns(self) -> None:
        """Probe-then-ALTER for columns an older store lacks. CREATE TABLE IF
        NOT EXISTS never reshapes an existing table."""
        self._conn.execute("DROP INDEX IF EXISTS idx_claim_ids_lookup")
        for table in ("claims", "claim_ids"):
            if "src" not in self._table_columns(table):
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN src TEXT NOT NULL DEFAULT 'observed'")

    def _migrate_from_payload_schema(self) -> None:
        """Convert a store that kept a full payload JSON per claim.

        Signatures are preserved untouched: the payload is decomposed into the
        same parts ``to_payload()`` would rebuild it from, so verification is
        unaffected. Done in one transaction — a half-converted store would be
        unreadable by either version.
        """
        if "claims" not in {r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            return
        if "payload" not in self._table_columns("claims"):
            return

        # The old `claims` table blocks CREATE TABLE IF NOT EXISTS from making
        # the new one, so move it aside first and rebuild around it.
        rows = self._conn.execute(
            "SELECT node, entity, key, payload, sig, observed_on, origin, stored_at, "
            "COALESCE(src, 'observed') AS src FROM claims").fetchall()
        self._conn.execute("ALTER TABLE claims RENAME TO claims_legacy")
        self._conn.executescript(SCHEMA)
        with self._conn:
            for row in rows:
                payload = json.loads(row["payload"])
                self._conn.execute(
                    "INSERT OR REPLACE INTO entities (entity, key, song_key, norm, key_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["entity"], row["key"], payload.get("song_key"),
                     json.dumps(payload.get("norm", {}), sort_keys=True),
                     payload.get("key_path")))
                self._conn.execute(
                    "INSERT OR REPLACE INTO claims "
                    "(node, entity, key, facts, sig, observed_on, origin, src, stored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["node"], row["entity"], row["key"],
                     json.dumps(payload.get("facts", {}), sort_keys=True),
                     row["sig"], row["observed_on"], row["origin"], row["src"],
                     row["stored_at"]))
            self._conn.execute("DROP TABLE IF EXISTS claims_legacy")
        self._conn.commit()
        self._conn.execute("VACUUM")

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
                "INSERT OR REPLACE INTO entities (entity, key, song_key, norm, key_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity, key, payload.get("song_key"),
                 json.dumps(payload.get("norm", {}), sort_keys=True),
                 payload.get("key_path")))
            self._conn.execute(
                "INSERT OR REPLACE INTO claims "
                "(node, entity, key, facts, sig, observed_on, origin, src, stored_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (signed.node, entity, key,
                 json.dumps(payload.get("facts", {}), sort_keys=True),
                 signed.sig, observed_on, origin, src, _now()))
            # Rebuild this observer's ids for this key — an id the node has
            # since retracted must not linger.
            self._conn.execute(
                "DELETE FROM claim_ids WHERE entity = ? AND key = ? AND node = ?",
                (entity, key, signed.node))
            self._conn.executemany(
                "INSERT OR IGNORE INTO claim_ids (entity, key, provider, value, node, src) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(entity, key, provider, value, signed.node, src)
                 for provider, values in (payload.get("ids") or {}).items()
                 for value in values])
        return True

    def put_many(self, signed_claims: Iterable[SignedClaim], origin: str = ORIGIN_PEER) -> int:
        return sum(1 for claim in signed_claims if self.put(claim, origin))

    # ── reading ──────────────────────────────────────────────────────────

    def _rebuild(self, row) -> SignedClaim:
        """Reassemble a signed claim from its stored parts.

        ``Claim.to_payload()`` is deterministic, so this produces the exact
        bytes the signature was made over.
        """
        ids: Dict[str, List[str]] = {}
        for id_row in self._conn.execute(
                "SELECT provider, value FROM claim_ids "
                "WHERE node = ? AND entity = ? AND key = ?",
                (row["node"], row["entity"], row["key"])):
            ids.setdefault(id_row["provider"], []).append(id_row["value"])

        claim = Claim(
            entity=row["entity"],
            norm=json.loads(row["norm"]),
            ids={p: tuple(sorted(v)) for p, v in ids.items()},
            facts=json.loads(row["facts"]),
            observed_on=row["observed_on"],
            key_path=row["key_path"],
            src=row["src"],
        )
        return SignedClaim(payload=claim.to_payload(), node=row["node"], sig=row["sig"])

    _SELECT_CLAIM = (
        "SELECT c.node, c.entity, c.key, c.facts, c.sig, c.observed_on, c.src, "
        "       e.norm, e.key_path "
        "FROM claims c JOIN entities e ON e.entity = c.entity AND e.key = c.key ")

    def get(self, entity: str, key: str) -> List[SignedClaim]:
        rows = self._conn.execute(
            self._SELECT_CLAIM + "WHERE c.entity = ? AND c.key = ? ORDER BY c.node",
            (entity, key)).fetchall()
        return [self._rebuild(row) for row in rows]

    def iter_claims(self, entity: Optional[str] = None,
                    origin: Optional[str] = None) -> Iterable[SignedClaim]:
        """Every stored claim, rebuilt. Streams rather than materializing."""
        where, params = [], []
        if entity:
            where.append("c.entity = ?")
            params.append(entity)
        if origin:
            where.append("c.origin = ?")
            params.append(origin)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        cursor = self._conn.execute(
            self._SELECT_CLAIM + clause + "ORDER BY c.entity, c.key", params)
        for row in cursor:
            yield self._rebuild(row)

    def resolve(self, entity: str, key: str, min_observers: int = 1) -> Dict[str, List[Dict[str, Any]]]:
        """The id map for one key, each id with its support.

        ``observers`` counts nodes whose own library held the entity;
        ``datasets`` counts public corpora that assert it. They are reported
        separately and never summed into one score: a dataset is one source
        however many nodes redistribute it, so letting it accumulate like
        independent observation would let one dump outvote every real person.

        A provider maps to a *list*: SoulSync's normalization collapses editions
        and remasters, so several ids can be correct under one key at once.
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
            (provider, value)).fetchall()
        return [(r["entity"], r["key"]) for r in rows]

    def verify_all(self) -> Tuple[int, List[str]]:
        """Re-verify every stored signature against bytes rebuilt from parts.

        This also proves the decomposition is lossless: a reconstruction that
        differed anywhere would fail to verify.
        """
        from .claim import InvalidClaim
        checked, failures = 0, []
        for signed in self.iter_claims():
            checked += 1
            try:
                signed.verify()
            except InvalidClaim as exc:
                failures.append(f"{signed.node[:12]}… {exc}")
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
                "SELECT COUNT(*) AS n FROM entities").fetchone()["n"],
            # Two different numbers, and conflating them overstates what a peer
            # taught you: `ids` is distinct (key, provider, value) triples —
            # actual knowledge — while `id_observations` counts rows, which
            # rises by one every time another node corroborates an id you held.
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
                (entity, cursor, _now()))

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

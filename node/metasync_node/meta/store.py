"""The node's local claims store — DESIGN-METADATA.md §5, step 4.

One SQLite file (`meta_claims.sqlite3`) in the node's state dir,
holding everything the metadata network keeps on this machine:

- **Own-library claims** (origin `own`): built from the SoulSync
  export by meta/builder.py. The claims table is the truth; each
  subject's claims are also materialized as signed batches in the
  batches table (a pure function of the claims — re-minted whenever
  the subject's claims change), so "the batches your node holds
  for X" are always inspectable. The materialization is TWO
  batches, split by the §7.1 re-serve rule (`claim_reservable`):
  the claims that may travel (bare ID/link claims among them) in
  one, everything else in the other — a single mixed batch would
  make the whole subject unforwardable over one held-back claim.
  Answers, though, are minted per query by the responder, in the
  same two partitions, filtered to the subjects actually asked
  about: a stored build batch is a materialization, not a spray
  hose.
- **Answer cache** (origin `cache`): batches received from other
  nodes, stored FROZEN — envelope as received, origin signature
  intact (§5/§6: a re-served batch is the origin node's signed
  bundle, forwarded, never re-signed as one's own). Written by
  step 5's querier through `put_cached_batch`; step 4 only reads.
  The `answer_cache` table carries per-batch provenance for those
  rows (receipt time, §7 verification state); reads tolerate it
  being empty, or a cached batch having no row there.
- **Indexes**, derived per claim at write time and keyed by claim
  id, so they track claim rows exactly (a subject's own and cached
  claims index independently): `provider_index` maps
  "provider:external_id" -> subject (from provider-id claims and
  both ends of provider_link claims); `name_index` maps normalized
  name tokens -> subject (the soulid normalizer's family, so text
  queries and SoulIDs agree on what a name is).

Update semantics for own claims are per OWNER — the export row that
produced them (`artist:<soul_id>`, `album:<soul_id>`,
`track:<soul_id>`). Re-processing a row replaces exactly its claims,
wherever they sit: a track that moved albums takes its album_track
slot claim off the old album, because the claim's owner is the track
row while its subject is the album. A completed FULL walk sweeps
own claims whose owner it never saw (the row left the library);
incremental walks only upsert (§6: retractions are local — a node
simply stops asserting).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import claims as meta_claims
from . import soulid as _soulid

DB_FILENAME = "meta_claims.sqlite3"

# kv key for the node's current binding attestation (§9): the value
# every own batch minted from this store embeds in its header.
_ATTESTATION_KV = "meta_attestation"

# Claim sources a cached batch may carry if it is to be re-served
# (§7.1 terms caution): MusicBrainz data is CC0 and file_tags values
# are the contributor's own. Anything provider-sourced stays home
# until the terms check clears. The caution is FIELD-level, not only
# source-level (§7.1): contributor-sourced durations and ISRCs and
# provider display names are answered from a node's own library but
# not re-served from cache, even when their source is file_tags —
# they usually originated in provider data upstream of the
# contributor's files. Only a MusicBrainz-sourced value of those
# fields is re-servable.
RESERVABLE_SOURCES = ("file_tags", "musicbrainz")
# Fields the §7.1 caution bars from re-serving unless sourced from
# MusicBrainz, regardless of the source-level rule above.
_NON_RESERVABLE_FIELDS = ("duration_ms", "isrc", "display_name")
# Bare ID and link claims are exempt from the filter entirely:
# §7.1 names bare ID links ("SoulID X is Deezer track Y") as what
# travels — the safest layer — whatever source stamped them. The
# caution binds catalogue VALUES (durations, ISRCs, display names,
# provider-sourced scalars), never the links between identifiers.
_LINK_FIELDS = (tuple(meta_claims.PROVIDER_ID_FIELDS)
                + tuple(meta_claims.MBID_FIELDS) + ("provider_link",))


def claim_reservable(claim: dict[str, Any]) -> bool:
    """The §7.1 re-serve rule for ONE claim — the single predicate
    the own-batch split, the cache filter below, and the tests all
    share, so the three can never drift apart. A claim may travel
    (be re-served, or sit in a batch that is) when it is a bare
    ID/link claim, or a file_tags/MusicBrainz value that does not
    assert a caution field from a non-MusicBrainz source."""
    if claim["field"] in _LINK_FIELDS:
        return True
    if claim["source"] not in RESERVABLE_SOURCES:
        return False
    return not (claim["field"] in _NON_RESERVABLE_FIELDS
                and claim["source"] != "musicbrainz")


def batch_reservable(claims: list[dict[str, Any]]) -> bool:
    """A batch is re-servable when every claim in it is. (An empty
    claim list is vacuously re-servable; such batches are never
    minted or served — every read path joins on claim rows.)"""
    return all(claim_reservable(c) for c in claims)

_PROVIDER_ID_FIELD = {f"{p}_id": p for p in
                      ("spotify", "deezer", "tidal", "itunes", "discogs",
                       "hydrabase")}
_MBID_FIELDS = ("artist_mbid", "release_group_mbid", "recording_mbid")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    origin      TEXT NOT NULL,            -- 'own' | 'cache'
    origin_node TEXT NOT NULL,            -- asserting node's pubkey (b64url)
    subject     TEXT,                     -- own batches: their one subject
    asserted_at REAL NOT NULL,            -- the origin batch's signed asserted_at; cache rows cap it at receipt (§6)
    envelope    TEXT NOT NULL             -- canonical JSON of {batch, sig}
);
CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY,
    origin     TEXT NOT NULL,             -- 'own' | 'cache'
    owner      TEXT,                      -- own only: the export row id
    batch_id   TEXT,                      -- cache only: the frozen batch
    subject    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_subject ON claims(subject);
CREATE INDEX IF NOT EXISTS claims_owner ON claims(owner);
CREATE INDEX IF NOT EXISTS claims_batch ON claims(batch_id);
-- The slot lookup (album_track_claims_for_tracks) filters on the
-- track subject inside the claim JSON; this partial expression
-- index keeps that lookup off a full scan of the claims table.
CREATE INDEX IF NOT EXISTS claims_album_track_track
    ON claims(json_extract(value_json, '$.track'))
    WHERE field = 'album_track';
CREATE TABLE IF NOT EXISTS provider_index (
    ref      TEXT NOT NULL,               -- "provider:external_id"
    subject  TEXT NOT NULL,
    via      TEXT NOT NULL,               -- 'id' | 'link_from' | 'link_to'
    claim_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_index_ref ON provider_index(ref);
CREATE INDEX IF NOT EXISTS provider_index_subject ON provider_index(subject);
CREATE TABLE IF NOT EXISTS name_index (
    token    TEXT NOT NULL,
    subject  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    claim_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS name_index_token ON name_index(token);
CREATE INDEX IF NOT EXISTS name_index_subject ON name_index(subject);
CREATE TABLE IF NOT EXISTS answer_cache (
    batch_id      TEXT PRIMARY KEY,
    origin_node   TEXT NOT NULL,
    received_at   REAL NOT NULL,
    verified_tier TEXT,                   -- §7 state, written by step 5
    verified_at   REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                   -- JSON
);
CREATE TABLE IF NOT EXISTS walk_owners (
    walk_id TEXT NOT NULL,
    owner   TEXT NOT NULL,
    PRIMARY KEY (walk_id, owner)
);
"""


def name_tokens(text: str) -> set[str]:
    """Index tokens for one name part: the whole part normalized the
    SoulID way (words run together), plus each word normalized on its
    own — a query typed as separate words must find a name the
    normalizer fused, and vice versa."""
    out: set[str] = set()
    whole = _soulid.normalize_for_soul_id(text)
    if whole:
        out.add(whole)
    for word in re.findall(r"[^\W_]+", text, flags=re.UNICODE):
        token = _soulid.normalize_for_soul_id(word)
        if token:
            out.add(token)
    return out


def provider_refs(claim: dict[str, Any]) -> list[tuple[str, str]]:
    """(ref, via) pairs one claim contributes to the provider index."""
    field = claim["field"]
    value = claim["value"]
    if field in _PROVIDER_ID_FIELD and isinstance(value, str):
        return [(f"{_PROVIDER_ID_FIELD[field]}:{value}", "id")]
    if field in _MBID_FIELDS and isinstance(value, str):
        return [(f"musicbrainz:{value}", "id")]
    if field == "provider_link" and isinstance(value, dict):
        provider = value.get("provider")
        if isinstance(provider, str) and provider:
            out = []
            if isinstance(value.get("from_id"), str) and value["from_id"]:
                out.append((f"{provider}:{value['from_id']}", "link_from"))
            if isinstance(value.get("to_id"), str) and value["to_id"]:
                out.append((f"{provider}:{value['to_id']}", "link_to"))
            return out
    return []


def node_id_for_key(public_key_b64: str) -> str | None:
    """The revocation lists' name for a key: SHA-256 hex of the raw
    public key bytes (the same derivation the relay's tokens.py and
    the node's identity use for node ids). None when the key string
    does not decode — such a batch has already been refused by
    verify_batch before any caller here sees it."""
    try:
        raw = meta_claims.b64url_decode(public_key_b64)
    except Exception:  # noqa: BLE001 - undecodable is unnameable
        return None
    if len(raw) != 32:
        return None
    return hashlib.sha256(raw).hexdigest()


class MetaStore:
    """Thread-safe (one connection, one lock) claims store."""

    def __init__(self, state_dir: str | Path, *, revocations: Any = None):
        # The §9/§10 revocation gate: the node's verified revocation
        # lists (revocations.RevocationStore), wired by the service
        # and shared with the relay/pool clients. Admission refuses a
        # revoked origin's batches, every cache read filters them
        # out, and purge_revoked drops them when a list lands. None
        # (a store nobody wired) means no lists are held: nothing is
        # revoked, which is the honest state of an unwired node.
        self.revocations = revocations
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / DB_FILENAME
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Belt-and-braces alongside the single lock: if another
        # process or a second store instance ever opens this file,
        # a writer waits instead of failing the operation outright.
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- small key/value state (builder walk position, maps) -----------

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    # -- the node's current binding attestation (§9) ---------------------

    def attestation(self) -> dict[str, Any] | None:
        """The attestation own batches currently embed: the latest
        one the pool handed the node (verified by the pool client
        before it was noted here — attestation.py owns the shape,
        and a stored value that no longer parses as one reads as
        none). Kept in kv so the store, the builder, and the
        responder all mint from the same current value.

        Freshness is checked HERE, at the one read every mint path
        shares (SF-3): a stored attestation whose validity window —
        issued_at..expires (§9; bound_at is the binding moment and
        plays no part in the window) — does not cover now reads as
        NO attestation. Embedding an expired-window attestation
        would mint batches that count zero support under §9 with no
        way to tell from the node; treating it as absent is the
        failure mode the design already handles, and a renewal
        re-notes a live one through set_attestation."""
        raw = self.kv_get(_ATTESTATION_KV)
        if not isinstance(raw, dict):
            return None
        from ..attestation import validate_attestation_shape

        parsed = validate_attestation_shape(raw)
        if parsed is None:
            return None
        now = time.time()
        if not (parsed["issued_at"] <= now < parsed["expires"]):
            return None
        return parsed

    def set_attestation(self, attestation: dict[str, Any] | None) -> bool:
        """Note the node's current attestation. Returns True when it
        CHANGED (a renewal the stored batches have not caught up
        with — the caller refreshes their embeddings). A malformed
        value is refused (stored as nothing, no change reported):
        minting must never embed unchecked parts."""
        from ..attestation import validate_attestation_shape

        normalized = validate_attestation_shape(attestation) \
            if attestation is not None else None
        if normalized == self.attestation():
            return False
        with self._lock:
            if normalized is None:
                self._db.execute(
                    "DELETE FROM kv WHERE key = ?", (_ATTESTATION_KV,))
            else:
                self._db.execute(
                    "INSERT INTO kv(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_ATTESTATION_KV,
                     json.dumps(normalized, ensure_ascii=False)))
            self._db.commit()
        return True

    # -- claim insert/delete, with indexes kept in step -------------------

    def _insert_claim(self, cur: sqlite3.Cursor, origin: str,
                      owner: str | None, batch_id: str | None,
                      claim: dict[str, Any]) -> None:
        cur.execute(
            "INSERT INTO claims(origin, owner, batch_id, subject, kind,"
            " field, value_json, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (origin, owner, batch_id, claim["subject"], claim["kind"],
             claim["field"],
             json.dumps(claim["value"], sort_keys=True, ensure_ascii=False),
             claim["source"]))
        claim_id = cur.lastrowid
        for ref, via in provider_refs(claim):
            cur.execute(
                "INSERT INTO provider_index(ref, subject, via, claim_id) "
                "VALUES (?, ?, ?, ?)",
                (ref, claim["subject"], via, claim_id))
        if claim["field"] in ("name", "display_name"):
            tokens: set[str] = set()
            for part in claim["value"]:
                tokens |= name_tokens(str(part))
            for token in tokens:
                cur.execute(
                    "INSERT INTO name_index(token, subject, kind, claim_id) "
                    "VALUES (?, ?, ?, ?)",
                    (token, claim["subject"], claim["kind"], claim_id))

    def _delete_claims(self, cur: sqlite3.Cursor, where: str,
                       params: tuple) -> set[str]:
        """Delete claims matching `where`; drops their index rows and
        returns the affected subjects."""
        ids = [row[0] for row in cur.execute(
            f"SELECT id FROM claims WHERE {where}", params)]
        subjects = {row[0] for row in cur.execute(
            f"SELECT DISTINCT subject FROM claims WHERE {where}", params)}
        if ids:
            marks = ",".join("?" for _ in ids)
            cur.execute(f"DELETE FROM provider_index "
                        f"WHERE claim_id IN ({marks})", ids)
            cur.execute(f"DELETE FROM name_index "
                        f"WHERE claim_id IN ({marks})", ids)
            cur.execute(f"DELETE FROM claims WHERE id IN ({marks})", ids)
        return subjects

    def _remint_subject(self, cur: sqlite3.Cursor, subject: str,
                        pubkey_b64: str, private_key: Any) -> None:
        """Re-materialize one subject's own batches from its claims
        (or drop them when the subject has none left): the claims
        split by the §7.1 rule into a RE-SERVABLE batch (every claim
        in it passes `claim_reservable`) and an OWN-ONLY batch (the
        rest), each minted only when its partition is non-empty.
        The two batches' digests cannot collide — each carries its
        own random header nonce."""
        cur.execute("DELETE FROM batches WHERE origin = 'own' "
                    "AND subject = ?", (subject,))
        rows = cur.execute(
            "SELECT kind, field, value_json, source FROM claims "
            "WHERE origin = 'own' AND subject = ? ORDER BY id",
            (subject,)).fetchall()
        if not rows:
            return
        subject_claims = [
            {"subject": subject, "kind": kind, "field": field,
             "value": json.loads(value_json), "source": source,
             "soulid_version": meta_claims.SOULID_VERSION}
            for kind, field, value_json, source in rows
        ]
        partitions = (
            [c for c in subject_claims if claim_reservable(c)],
            [c for c in subject_claims if not claim_reservable(c)],
        )
        attestation = self.attestation()
        for part in partitions:
            if not part:
                continue
            batch = meta_claims.make_batch(
                pubkey_b64, part, attestation=attestation)
            envelope = meta_claims.sign_batch(private_key, batch)
            cur.execute(
                "INSERT INTO batches(batch_id, origin, origin_node,"
                " subject, asserted_at, envelope) "
                "VALUES (?, 'own', ?, ?, ?, ?)",
                (meta_claims.batch_digest(batch), pubkey_b64, subject,
                 batch["header"]["asserted_at"],
                 meta_claims.canonical_bytes(envelope).decode("utf-8")))

    # -- own claims ---------------------------------------------------------

    def _replace_owner_claims_cur(self, cur: sqlite3.Cursor, owner: str,
                                  new_claims: list[dict]) -> set[str]:
        """One owner's replacement on an open cursor: delete its old
        claims, insert the new set, return the affected subjects.
        Commits and re-minting are the caller's business."""
        affected = self._delete_claims(
            cur, "origin = 'own' AND owner = ?", (owner,))
        for claim in new_claims:
            self._insert_claim(cur, "own", owner, None, claim)
            affected.add(claim["subject"])
        return affected

    def replace_owner_claims(self, owner: str, new_claims: list[dict],
                             pubkey_b64: str, private_key: Any) -> None:
        """Replace one export row's own claims with its new set. The
        affected subjects (of the deleted and the new claims) get
        their stored batches re-minted; nothing else moves."""
        with self._lock:
            cur = self._db.cursor()
            affected = self._replace_owner_claims_cur(
                cur, owner, new_claims)
            for subject in affected:
                self._remint_subject(cur, subject, pubkey_b64, private_key)
            self._db.commit()

    def replace_owners_page(
            self, entries: list[tuple[str, list[dict]]],
            pubkey_b64: str, private_key: Any) -> None:
        """One export page's replacements in ONE commit: each
        owner's claims are replaced, and each affected subject's
        batch is re-minted once, however many rows touched it.
        Crash semantics: the page lands whole or not at all."""
        with self._lock:
            cur = self._db.cursor()
            affected: set[str] = set()
            for owner, new_claims in entries:
                affected |= self._replace_owner_claims_cur(
                    cur, owner, new_claims)
            for subject in affected:
                self._remint_subject(cur, subject, pubkey_b64, private_key)
            self._db.commit()

    def own_claims(self, subject: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, field, value_json, source FROM claims "
                "WHERE origin = 'own' AND subject = ? ORDER BY id",
                (subject,)).fetchall()
        return [
            {"subject": subject, "kind": kind, "field": field,
             "value": json.loads(value_json), "source": source,
             "soulid_version": meta_claims.SOULID_VERSION}
            for kind, field, value_json, source in rows
        ]

    def own_batches(self, subject: str) -> list[dict[str, Any]]:
        """The stored signed batches materializing this subject's
        own claims (what the last build produced; preview/audit
        surface): the re-servable partition first, then the
        own-only one — either may be absent, and a subject with no
        claims has none."""
        with self._lock:
            rows = self._db.execute(
                "SELECT envelope FROM batches WHERE origin = 'own' "
                "AND subject = ?", (subject,)).fetchall()
        envelopes = [json.loads(row[0]) for row in rows]

        def _rank(envelope: dict[str, Any]) -> tuple[int, str]:
            batch = envelope["batch"]
            return (0 if batch_reservable(batch["claims"]) else 1,
                    meta_claims.batch_digest(batch))

        return sorted(envelopes, key=_rank)

    def refresh_attestation_embeddings(self, pubkey_b64: str,
                                       private_key: Any) -> int:
        """Re-mint every own subject whose stored batches do not all
        embed the CURRENT attestation (§6 re-minting: batches are
        re-minted whenever the attestation renews, so what the node
        serves carries a live attestation rather than a fossil). An
        expired-window attestation embedded in a stored batch is
        replaced by the current one, never kept alongside it. One
        commit for the whole refresh; returns the number of subjects
        re-minted. Subjects whose claims are gone have no batches
        and are not visited — their next claim replacement re-mints
        them with the then-current attestation anyway."""
        current = self.attestation()
        with self._lock:
            rows = self._db.execute(
                "SELECT subject, envelope FROM batches "
                "WHERE origin = 'own'").fetchall()
            stale: set[str] = set()
            for subject, raw in rows:
                try:
                    embedded = json.loads(raw)["batch"]["header"].get(
                        "attestation")
                except (ValueError, KeyError, AttributeError):
                    embedded = None
                if embedded != current:
                    stale.add(subject)
            if not stale:
                return 0
            cur = self._db.cursor()
            for subject in sorted(stale):
                self._remint_subject(cur, subject, pubkey_b64, private_key)
            self._db.commit()
            return len(stale)

    # -- full-walk sweep ----------------------------------------------------

    def walk_note_owner(self, walk_id: str, owner: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO walk_owners(walk_id, owner) "
                "VALUES (?, ?)", (walk_id, owner))
            self._db.commit()

    def walk_note_owners(self, walk_id: str, owners: list[str]) -> None:
        """One export page's seen-owners in a single commit (the
        batched form of walk_note_owner)."""
        if not owners:
            return
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO walk_owners(walk_id, owner) "
                "VALUES (?, ?)", [(walk_id, owner) for owner in owners])
            self._db.commit()

    def walk_owner_count(self, walk_id: str) -> int:
        """Distinct owners this walk has noted as seen, across all
        its (possibly resumed) runs."""
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM walk_owners WHERE walk_id = ?",
                (walk_id,)).fetchone()[0])

    def walk_unseen_claims(self, walk_id: str) -> int:
        """How many own claims a full walk's sweep WOULD drop —
        the builder checks this before sweeping (a refused sweep
        must not have deleted anything)."""
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM claims WHERE origin = 'own' "
                "AND owner NOT IN (SELECT owner FROM walk_owners "
                "WHERE walk_id = ?)", (walk_id,)).fetchone()[0])

    def walk_sweep(self, walk_id: str, pubkey_b64: str,
                   private_key: Any) -> int:
        """End of a FULL walk: drop own claims whose owner the walk
        never saw (their rows left the library), re-minting the
        affected subjects from whatever claims remain. Returns the
        number of claims dropped."""
        with self._lock:
            cur = self._db.cursor()
            dropped = cur.execute(
                "SELECT COUNT(*) FROM claims WHERE origin = 'own' "
                "AND owner NOT IN (SELECT owner FROM walk_owners "
                "WHERE walk_id = ?)", (walk_id,)).fetchone()[0]
            affected = self._delete_claims(
                cur,
                "origin = 'own' AND owner NOT IN (SELECT owner FROM "
                "walk_owners WHERE walk_id = ?)", (walk_id,))
            cur.execute("DELETE FROM walk_owners WHERE walk_id = ?",
                        (walk_id,))
            for subject in affected:
                self._remint_subject(cur, subject, pubkey_b64, private_key)
            self._db.commit()
            return int(dropped)

    def walk_clear(self, walk_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM walk_owners WHERE walk_id = ?",
                             (walk_id,))
            self._db.commit()

    # -- answer cache (frozen batches; written by step 5) -------------------

    def _origin_revoked(self, header_node: str) -> bool:
        """The §9 revocation check for one batch origin: the origin
        key's node id against EVERY held list (the union). The
        batch's embedded attestation names an issuer, but that
        value is never verified at receipt — verify_batch checks
        the origin's signature over the batch, not the pool's
        signature over the attestation — so letting it select the
        governing list would let a revoked origin escape by naming
        a made-up issuer nobody holds a list for (gate re-pass
        N1). In v1 the node trusts one configured issuer, so the
        union over held lists is that issuer's verdict, and it
        agrees with purge_revoked, which likewise drops by key
        regardless of the issuer a batch claims. Issuer-scoped
        checking returns only when admission verifies the
        attestation against the configured issuer key and matches
        its issuer id. An origin that cannot be named cannot be
        checked — verify_batch has already proven the key decodes
        before this runs."""
        if self.revocations is None:
            return False
        node_id = node_id_for_key(header_node)
        if node_id is None:
            return False
        return bool(self.revocations.is_revoked(node_id))

    def _envelope_revoked(self, envelope: Any) -> bool:
        """_origin_revoked for a stored envelope being read back: a
        row too malformed to name its origin reads as unrevoked
        here — the read paths already skip such rows as corrupt."""
        if not isinstance(envelope, dict):
            return False
        batch = envelope.get("batch")
        if not isinstance(batch, dict):
            return False
        header = batch.get("header")
        if not isinstance(header, dict):
            return False
        node = header.get("node")
        if not isinstance(node, str) or not node:
            return False
        return self._origin_revoked(node)

    def purge_revoked(self, node_ids: set[str]) -> int:
        """Drop every CACHED batch whose origin key is named in
        `node_ids` (the revocation lists' hex node ids): §10 — when
        a list lands, the node drops the named keys' batches from
        its cache, so a revoked key's claims stop being re-served
        and stop feeding merges, not merely stop being admitted.
        Own batches are never touched (they are this node's own
        library, not the cache). Returns the batches dropped."""
        if not node_ids:
            return 0
        with self._lock:
            rows = self._db.execute(
                "SELECT batch_id, origin_node FROM batches "
                "WHERE origin = 'cache'").fetchall()
            doomed = [batch_id for batch_id, origin_node in rows
                      if node_id_for_key(origin_node) in node_ids]
            if not doomed:
                return 0
            cur = self._db.cursor()
            for batch_id in doomed:
                self._delete_claims(cur, "batch_id = ?", (batch_id,))
                cur.execute(
                    "DELETE FROM answer_cache WHERE batch_id = ?",
                    (batch_id,))
                cur.execute(
                    "DELETE FROM batches WHERE batch_id = ?", (batch_id,))
            self._db.commit()
            return len(doomed)

    def put_cached_batch(self, envelope: dict[str, Any],
                         received_at: float | None = None) -> str:
        """Store one received batch, frozen. Verifies the origin
        signature and every claim first — an invalid batch is refused
        (ClaimError), never repaired. Idempotent per batch digest.
        The envelope shape is validated BEFORE any indexing into
        it: a malformed envelope is a ClaimError, never a bare
        KeyError or TypeError.

        A batch whose ORIGIN key is named on a held revocation list
        is refused here too (§9: the revocation check runs at
        admission, on every batch, direct or re-served) — a
        forwarder re-serving an old copy of a revoked key's batch
        cannot resurrect it, however valid its attestation window
        still looks."""
        if not isinstance(envelope, dict):
            raise meta_claims.ClaimError("envelope must be an object")
        batch_obj = envelope.get("batch")
        if not isinstance(batch_obj, dict):
            raise meta_claims.ClaimError("envelope.batch must be an object")
        header = batch_obj.get("header")
        if not isinstance(header, dict):
            raise meta_claims.ClaimError(
                "envelope.batch.header must be an object")
        header_node = header.get("node")
        if not isinstance(header_node, str) or not header_node:
            raise meta_claims.ClaimError(
                "envelope.batch.header.node must name the origin node")
        if not isinstance(envelope.get("sig"), str) \
                or not envelope["sig"]:
            raise meta_claims.ClaimError("envelope.sig missing")
        batch = meta_claims.verify_batch(header_node, envelope)
        if self._origin_revoked(header_node):
            raise meta_claims.ClaimError(
                "origin key is named on a held revocation list: "
                "batch refused at admission (§9)")
        batch_id = meta_claims.batch_digest(batch)
        received = time.time() if received_at is None else received_at
        # §6: a cached batch ages from its ORIGIN's signed asserted_at,
        # capped at the moment of receipt — a future-dated batch cannot
        # extend its own life, and a forwarder re-serving an old batch
        # can never refresh its age by handing it over again.
        asserted = min(float(batch["header"]["asserted_at"]), received)
        with self._lock:
            cur = self._db.cursor()
            if cur.execute("SELECT 1 FROM batches WHERE batch_id = ?",
                           (batch_id,)).fetchone():
                return batch_id
            cur.execute(
                "INSERT INTO batches(batch_id, origin, origin_node,"
                " subject, asserted_at, envelope) "
                "VALUES (?, 'cache', ?, NULL, ?, ?)",
                (batch_id, header_node, asserted,
                 meta_claims.canonical_bytes(envelope).decode("utf-8")))
            for claim in batch["claims"]:
                self._insert_claim(cur, "cache", None, batch_id, claim)
            cur.execute(
                "INSERT OR IGNORE INTO answer_cache(batch_id, origin_node,"
                " received_at) VALUES (?, ?, ?)",
                (batch_id, header_node, received))
            self._db.commit()
        return batch_id

    def cached_batches_for_subjects(
            self, subjects: list[str], *, reservable_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Frozen cached envelopes asserting any of `subjects`, in
        deterministic (batch_id) order. With `reservable_only`, the
        §7.1 filter applies — the SQL form of `claim_reservable`,
        kept in lockstep with it: a claim blocks its batch when it
        is NOT a bare ID/link claim AND (its source is not
        file_tags/musicbrainz OR it asserts a §7.1-caution field —
        duration_ms, isrc, display_name — from a non-MusicBrainz
        source). A row whose stored envelope does not even parse is
        skipped, never served. A batch whose origin key is named on
        a held revocation list is likewise skipped, on every read,
        reservable or not (§9/§10)."""
        if not subjects:
            return []
        marks = ",".join("?" for _ in subjects)
        sql = (
            "SELECT DISTINCT b.batch_id, b.envelope FROM batches b "
            "JOIN claims c ON c.batch_id = b.batch_id "
            f"WHERE b.origin = 'cache' AND c.subject IN ({marks})"
        )
        params: list[Any] = list(subjects)
        if reservable_only:
            marks_link = ",".join("?" for _ in _LINK_FIELDS)
            marks_src = ",".join("?" for _ in RESERVABLE_SOURCES)
            marks_field = ",".join("?" for _ in _NON_RESERVABLE_FIELDS)
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM claims x "
                "WHERE x.batch_id = b.batch_id "
                f"AND x.field NOT IN ({marks_link}) "
                f"AND (x.source NOT IN ({marks_src}) "
                f"OR (x.field IN ({marks_field}) "
                "AND x.source <> 'musicbrainz')))"
            )
            params += list(_LINK_FIELDS)
            params += list(RESERVABLE_SOURCES)
            params += list(_NON_RESERVABLE_FIELDS)
        sql += " ORDER BY b.batch_id"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for _id, envelope in rows:
            try:
                parsed = json.loads(envelope)
            except (TypeError, ValueError):
                continue  # corrupted row: skipped, never served
            if self._envelope_revoked(parsed):
                # §9/§10: a revoked origin's batches are never
                # re-served — this filter is the belt to admission's
                # braces, covering batches cached BEFORE their key
                # was revoked and not yet purged.
                continue
            out.append(parsed)
        return out

    def album_track_claims_for_tracks(
            self, track_subjects: list[str]) -> list[dict[str, Any]]:
        """Own album_track claims placing any of these tracks. Their
        subject is the ALBUM, so a subject lookup cannot find them;
        an exact answer for a track needs its slot claim from here."""
        if not track_subjects:
            return []
        marks = ",".join("?" for _ in track_subjects)
        with self._lock:
            rows = self._db.execute(
                "SELECT subject, kind, field, value_json, source FROM claims "
                "WHERE origin = 'own' AND field = 'album_track' "
                f"AND json_extract(value_json, '$.track') IN ({marks})",
                list(track_subjects)).fetchall()
        return [
            {"subject": subject, "kind": kind, "field": field,
             "value": json.loads(value_json), "source": source,
             "soulid_version": meta_claims.SOULID_VERSION}
            for subject, kind, field, value_json, source in rows
        ]

    # -- lookups the responder runs ------------------------------------------

    def resolve_provider_ref(self, ref: str) -> list[str]:
        """Subjects a "provider:external_id" ref resolves to, direct
        id claims first, then link relations; deterministic order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT subject, via FROM provider_index WHERE ref = ?",
                (ref,)).fetchall()
        rank = {"id": 0, "link_from": 1, "link_to": 2}
        out: list[str] = []
        for subject, _via in sorted(rows, key=lambda r: (rank.get(r[1], 3),
                                                         r[0])):
            if subject not in out:
                out.append(subject)
        return out

    def subjects_with_token(self, token: str, kind: str | None) -> set[str]:
        sql = "SELECT DISTINCT subject FROM name_index WHERE token = ?"
        params: list[Any] = [token]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        with self._lock:
            return {row[0] for row in self._db.execute(sql, params)}

    def subjects_with_token_prefix(self, prefix: str, kind: str | None,
                                   limit: int = 500) -> set[str]:
        # Tokens hold only letters/digits (the soulid filter drops the
        # rest), so escaping the LIKE wildcards makes this a pure
        # prefix test.
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%") \
            .replace("_", "\\_")
        sql = ("SELECT DISTINCT subject FROM name_index "
               "WHERE token LIKE ? ESCAPE '\\'")
        params: list[Any] = [escaped + "%"]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(limit)
        with self._lock:
            return {row[0] for row in self._db.execute(sql, params)}

    def tokens_with_prefix(self, prefix: str, kind: str | None,
                           page_size: int = 5000) -> list[str]:
        """ALL distinct name tokens starting with `prefix`, in
        deterministic order. Callers bucket by the first TWO
        characters, and the scan pages through the whole bucket:
        there is no cap to truncate at, silently or otherwise — a
        bucket bigger than one page is read to the end. The lock is
        held across the pages so the offsets stay meaningful."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%") \
            .replace("_", "\\_")
        sql = ("SELECT DISTINCT token FROM name_index "
               "WHERE token LIKE ? ESCAPE '\\'")
        params: list[Any] = [escaped + "%"]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY token LIMIT ? OFFSET ?"
        out: list[str] = []
        offset = 0
        with self._lock:
            while True:
                rows = self._db.execute(
                    sql, [*params, page_size, offset]).fetchall()
                out.extend(row[0] for row in rows)
                if len(rows) < page_size:
                    return out
                offset += page_size

    # -- status ---------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self._lock:
            own_claims = self._db.execute(
                "SELECT COUNT(*) FROM claims WHERE origin = 'own'"
            ).fetchone()[0]
            own_subjects = self._db.execute(
                "SELECT COUNT(DISTINCT subject) FROM claims "
                "WHERE origin = 'own'").fetchone()[0]
            cache_batches = self._db.execute(
                "SELECT COUNT(*) FROM batches WHERE origin = 'cache'"
            ).fetchone()[0]
            cache_claims = self._db.execute(
                "SELECT COUNT(*) FROM claims WHERE origin = 'cache'"
            ).fetchone()[0]
        return {
            "own_claims": int(own_claims),
            "own_subjects": int(own_subjects),
            "cached_batches": int(cache_batches),
            "cached_claims": int(cache_claims),
        }

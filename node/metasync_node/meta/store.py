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
from datetime import UTC, datetime
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
    if claim["source"] == "network":
        # §6: a network-marked value never travels again, not even
        # as a bare ID link — the mark exists to stop a promoted
        # value laundering back into network truth.
        return False
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
    kind     TEXT,                        -- kind of the entity the REF names (gate minor); NULL on pre-fold rows
    subject_kind TEXT,                    -- kind of the SUBJECT the row resolves to (gate r2); NULL on pre-fold rows
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
-- Step 6 (the querier): legacy SoulID aliases (§6). A legacy id is
-- an alias entry, never a subject; a row lands here only when the
-- builder verified the mapping by recomputing the v2 id from the
-- export row's own name parts (meta/builder.py).
CREATE TABLE IF NOT EXISTS legacy_aliases (
    legacy     TEXT PRIMARY KEY,          -- the pre-v2 id, exactly as served
    subject    TEXT NOT NULL,             -- the verified kinded_v2 subject
    learned_at REAL NOT NULL
);
-- §9 admission record per cached batch: whether the origin met
-- standing (attested age >= 30 days) when the batch was admitted.
-- Support counting reads this, never the raw attestation — the
-- admission decision is made once, at the door.
CREATE TABLE IF NOT EXISTS batch_admission (
    batch_id    TEXT PRIMARY KEY,
    origin_node TEXT NOT NULL,
    standing    INTEGER NOT NULL,         -- 1 = origin met §9 standing
    issuer      TEXT,                     -- embedded attestation's issuer (unverified)
    bound_at    INTEGER,                  -- embedded attestation's bound_at
    admitted_at REAL NOT NULL
);
-- §7 verification verdicts, keyed by CLAIM IDENTITY (origin key,
-- subject, field, value) — never by batch id (§6: re-minting
-- changes a batch's id, never its claims' identities).
CREATE TABLE IF NOT EXISTS claim_verdicts (
    origin_node TEXT NOT NULL,
    subject     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value_key   TEXT NOT NULL,            -- canonical JSON of the value
    tier        TEXT NOT NULL,            -- 'plausible' | 'strong-verified'
    verified_at REAL NOT NULL,
    detail      TEXT,
    PRIMARY KEY (origin_node, subject, field, value_key)
);
CREATE INDEX IF NOT EXISTS claim_verdicts_subject
    ON claim_verdicts(subject);
-- §9 reputation: this node's LOCAL record of how an origin's
-- claims fared under §7 checks. Display ordering and the sanity
-- gate only — never support, never admission.
CREATE TABLE IF NOT EXISTS reputation (
    origin_node   TEXT PRIMARY KEY,
    checks_passed INTEGER NOT NULL DEFAULT 0,
    checks_failed INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
-- §8's bounded sanity rule: one consumed trigger per origin per
-- subject-field per ROLLING 24 h (R2-SF-8) — fired_at carries
-- the window; day is retained for the primary key's shape.
CREATE TABLE IF NOT EXISTS sanity_triggers (
    origin_node TEXT NOT NULL,
    subject     TEXT NOT NULL,
    field       TEXT NOT NULL,
    day         TEXT NOT NULL,            -- UTC YYYY-MM-DD
    fired_at    REAL,                     -- Unix seconds
    PRIMARY KEY (origin_node, subject, field, day)
);
-- §7.1 fetch cache: provider responses are cached 90 days; a
-- not_found row records the lookup outcome, not an absence proof
-- (absence needs the second-path test in meta/verify.py).
CREATE TABLE IF NOT EXISTS fetch_cache (
    provider   TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    not_found  INTEGER NOT NULL DEFAULT 0,
    payload    TEXT,                      -- JSON; NULL when not_found
    PRIMARY KEY (provider, cache_key)
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


def provider_refs(claim: dict[str, Any]) -> list[tuple[
        str, str, str | None, str | None]]:
    """(ref, via, kind, subject_kind) rows one claim contributes
    to the provider index. `kind` is the kind of the entity the
    REF names: for id claims and a link's from-end, the claim's
    own kind (the ref names the subject); for a link's to-end,
    the link's `to_kind` (it names the OTHER entity) — kept as a
    separate fact about the far end. `subject_kind` is the kind
    of the claim's OWN subject — the entity resolution returns —
    and is recorded on EVERY row (gate r2): resolution filters
    on it for all via types, so a link's to-end resolves only
    within its subject's kind. Providers number their kinds
    separately, so resolution can — and, when the caller knows
    the kind it is resolving for, must — match within kind."""
    field = claim["field"]
    value = claim["value"]
    kind = claim.get("kind")
    if field in _PROVIDER_ID_FIELD and isinstance(value, str):
        return [(f"{_PROVIDER_ID_FIELD[field]}:{value}", "id",
                 kind, kind)]
    if field in _MBID_FIELDS and isinstance(value, str):
        return [(f"musicbrainz:{value}", "id", kind, kind)]
    if field == "provider_link" and isinstance(value, dict):
        provider = value.get("provider")
        if isinstance(provider, str) and provider:
            out = []
            if isinstance(value.get("from_id"), str) and value["from_id"]:
                out.append((f"{provider}:{value['from_id']}",
                            "link_from", kind, kind))
            if isinstance(value.get("to_id"), str) and value["to_id"]:
                to_kind = value.get("to_kind")
                out.append((f"{provider}:{value['to_id']}", "link_to",
                            to_kind if to_kind in meta_claims.KINDS
                            else kind, kind))
            return out
    return []


def value_key(value: Any) -> str:
    """A claim value's identity string: byte-identical to the
    value_json the claims table stores (same dumps call), so a
    verdict row and a claim row for the same value always meet.
    Claim identity everywhere in step 6 — verdicts, merge
    provenance, the depot link (§6) — is (origin key, subject,
    field, this string)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


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
        # Rolling-24h sanity windows (R2-SF-8) need fired_at on
        # sanity_triggers; databases created before it lack the
        # column (their rows read as long-ago, which is safe).
        columns = {row[1] for row in self._db.execute(
            "PRAGMA table_info(sanity_triggers)")}
        if "fired_at" not in columns:
            self._db.execute(
                "ALTER TABLE sanity_triggers ADD COLUMN fired_at"
                " REAL")
        # Provider-index rows carry the kind of the entity their
        # ref names (gate minor: Deezer numbers artist, album and
        # track ids separately, so an artist id must never resolve
        # inside an album/track-scoped lookup). Rows written
        # before the column existed keep NULL and read as
        # kind-unknown — resolution treats them as matching any
        # kind (backwards-compatible reads; the index rebuilds
        # from batches on write, so kinds fill in as claims move).
        pi_columns = {row[1] for row in self._db.execute(
            "PRAGMA table_info(provider_index)")}
        if "kind" not in pi_columns:
            self._db.execute(
                "ALTER TABLE provider_index ADD COLUMN kind TEXT")
        # Every row also records the SUBJECT's kind (gate r2):
        # resolution filters on it for ALL via types, link_to
        # included — the kind column above stays as a fact about
        # the ref's far end. Pre-fold rows keep NULL and read as
        # kind-unknown, the same backwards-compatible convention
        # as the kind column.
        if "subject_kind" not in pi_columns:
            self._db.execute(
                "ALTER TABLE provider_index ADD COLUMN subject_kind"
                " TEXT")
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
        for ref, via, ref_kind, subj_kind in provider_refs(claim):
            cur.execute(
                "INSERT INTO provider_index(ref, subject, via, kind,"
                " subject_kind, claim_id) VALUES (?, ?, ?, ?, ?, ?)",
                (ref, claim["subject"], via, ref_kind, subj_kind,
                 claim_id))
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
            doomed_pairs = [(batch_id, origin_node)
                            for batch_id, origin_node in rows
                            if node_id_for_key(origin_node) in node_ids]
            if not doomed_pairs:
                return 0
            cur = self._db.cursor()
            for batch_id, origin_node in doomed_pairs:
                # The batch's §7 verdicts die with it: a verdict is
                # evidence ABOUT a claim this node holds, and a
                # purged claim must not leave a verdict behind that
                # a later identical claim could inherit (§6 keys
                # provenance by claim identity — the identity is
                # gone from this node's cache, so the verdict goes).
                cur.execute(
                    "DELETE FROM claim_verdicts WHERE origin_node = ? "
                    "AND (subject, field, value_key) IN "
                    "(SELECT subject, field, value_json FROM claims "
                    "WHERE batch_id = ?)",
                    (origin_node, batch_id))
                self._delete_claims(cur, "batch_id = ?", (batch_id,))
                cur.execute(
                    "DELETE FROM answer_cache WHERE batch_id = ?",
                    (batch_id,))
                cur.execute(
                    "DELETE FROM batch_admission WHERE batch_id = ?",
                    (batch_id,))
                cur.execute(
                    "DELETE FROM batches WHERE batch_id = ?", (batch_id,))
            self._db.commit()
            return len(doomed_pairs)

    def put_cached_batch(self, envelope: dict[str, Any],
                         received_at: float | None = None,
                         *, standing: bool = False,
                         attestation_issuer: str | None = None,
                         attestation_bound_at: int | None = None,
                         ) -> str:
        """Store one received batch, frozen. Verifies the origin
        signature and every claim first — an invalid batch is refused
        (ClaimError), never repaired. Idempotent per batch digest.
        The envelope shape is validated BEFORE any indexing into
        it: a malformed envelope is a ClaimError, never a bare
        KeyError or TypeError.

        The §9 admission outcome rides along (meta/admission.py
        computes it; this method only records it): `standing` says
        the origin met attested-age standing at the door, and the
        batch_admission row is written in the same commit as the
        batch itself, so support counting never sees a cached batch
        without its admission record. A direct caller that passes
        nothing records standing=False — zero support is the safe
        default for a batch nobody ran admission on. A repeat call
        for an already-stored digest still reconciles the admission
        row — upward only (R1-SF-1).

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
                # Re-admission of a known batch (R1-SF-1): the
                # claims are already stored, but the §9 verdict is
                # not frozen at first sight — a batch admitted
                # before this node could verify its attestation
                # (no issuer key configured yet, no trusted issuer
                # known) EARNS its standing when it arrives again
                # and the door verifies it. Upgrade only, ever: a
                # later unverifiable arrival cannot strip standing
                # the door once verified.
                if standing:
                    cur.execute(
                        "UPDATE batch_admission SET standing = 1,"
                        " issuer = ?, bound_at = ?"
                        " WHERE batch_id = ? AND standing = 0",
                        (attestation_issuer, attestation_bound_at,
                         batch_id))
                    self._db.commit()
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
            cur.execute(
                "INSERT OR IGNORE INTO batch_admission(batch_id,"
                " origin_node, standing, issuer, bound_at, admitted_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, header_node, 1 if standing else 0,
                 attestation_issuer, attestation_bound_at, received))
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
                "AND (x.source = 'network' "
                f"OR (x.field NOT IN ({marks_link}) "
                f"AND (x.source NOT IN ({marks_src}) "
                f"OR (x.field IN ({marks_field}) "
                "AND x.source <> 'musicbrainz')))))"
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

    def resolve_provider_ref(self, ref: str,
                             kind: str | None = None) -> list[str]:
        """Subjects a "provider:external_id" ref resolves to, direct
        id claims first, then link relations; deterministic order.

        With `kind`, EVERY row matches on the SUBJECT's kind —
        the entity resolution returns — whatever its via (gate
        r2): a "deezer:399" that is an ARTIST's id must not
        resolve inside a track-scoped lookup just because the
        numbers collide (Deezer numbers its kinds separately),
        and an album whose album_of points at artist 399 must not
        answer a TRACK ask for deezer:399 either — the old
        link_to exemption manufactured an album record under the
        asked kind, and nothing downstream checked the matched
        link against it. Traversal stays legal where it is
        honest: the subject of an album_of row IS an album, so an
        album ask still finds the album through the link's to-end
        (the link's `to_kind` stays recorded on the row as a fact
        about the far end; it just no longer exempts the row).
        Rows written before subject kinds were recorded (NULL)
        match any kind: the read stays backwards-compatible and
        the kinds fill in as claims are rewritten."""
        with self._lock:
            rows = self._db.execute(
                "SELECT subject, via, kind, subject_kind "
                "FROM provider_index WHERE ref = ?", (ref,)).fetchall()
        if kind is not None:
            rows = [row for row in rows
                    if row[3] is None or row[3] == kind]
        rank = {"id": 0, "link_from": 1, "link_to": 2}
        out: list[str] = []
        for subject, _via, _kind, _skind in sorted(
                rows, key=lambda r: (rank.get(r[1], 3), r[0])):
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

    # -- querier reads: cached claims with provenance (§6/§8/§9) -------

    def cached_claims(self, subjects: list[str]) -> list[dict[str, Any]]:
        """Cached claims for `subjects`, each row carrying its
        provenance: the asserting origin key, its batch id (evidence
        only — §6), the batch's origin age basis (the signed
        asserted_at, capped at receipt when the batch landed), and
        the §9 standing recorded at admission. Batches from origins
        named on a held revocation list are filtered on the way out
        (§9) — the belt to purge_revoked's braces, covering a list
        that landed without a purge pass."""
        if not subjects:
            return []
        marks = ",".join("?" for _ in subjects)
        with self._lock:
            rows = self._db.execute(
                "SELECT c.batch_id, b.origin_node, b.asserted_at,"
                " a.standing, c.subject, c.kind, c.field, c.value_json,"
                " c.source FROM claims c"
                " JOIN batches b ON b.batch_id = c.batch_id"
                " LEFT JOIN batch_admission a"
                " ON a.batch_id = c.batch_id"
                f" WHERE c.origin = 'cache' AND c.subject IN ({marks})"
                " ORDER BY c.id", list(subjects)).fetchall()
        out: list[dict[str, Any]] = []
        for (batch_id, origin_node, asserted_at, standing, subject,
             kind, field, value_json, source) in rows:
            if self._origin_revoked(origin_node):
                continue
            out.append({
                "batch_id": batch_id, "origin_node": origin_node,
                "asserted_at": float(asserted_at),
                "standing": bool(standing),
                "subject": subject, "kind": kind, "field": field,
                "value": json.loads(value_json), "source": source,
            })
        return out

    def cached_slot_claims(self, track_subjects: list[str],
                           ) -> list[dict[str, Any]]:
        """Cached album_track claims placing any of these tracks —
        the cache twin of album_track_claims_for_tracks (whose
        subject is the album, so a subject lookup cannot find
        them), with the same provenance shape as cached_claims."""
        if not track_subjects:
            return []
        marks = ",".join("?" for _ in track_subjects)
        with self._lock:
            rows = self._db.execute(
                "SELECT c.batch_id, b.origin_node, b.asserted_at,"
                " a.standing, c.subject, c.kind, c.field, c.value_json,"
                " c.source FROM claims c"
                " JOIN batches b ON b.batch_id = c.batch_id"
                " LEFT JOIN batch_admission a"
                " ON a.batch_id = c.batch_id"
                " WHERE c.origin = 'cache' AND c.field = 'album_track'"
                f" AND json_extract(c.value_json, '$.track')"
                f" IN ({marks}) ORDER BY c.id",
                list(track_subjects)).fetchall()
        out: list[dict[str, Any]] = []
        for (batch_id, origin_node, asserted_at, standing, subject,
             kind, field, value_json, source) in rows:
            if self._origin_revoked(origin_node):
                continue
            out.append({
                "batch_id": batch_id, "origin_node": origin_node,
                "asserted_at": float(asserted_at),
                "standing": bool(standing),
                "subject": subject, "kind": kind, "field": field,
                "value": json.loads(value_json), "source": source,
            })
        return out

    def admission_for(self, batch_ids: list[str]) -> dict[str, dict]:
        """The §9 admission records for these batches, by batch id."""
        if not batch_ids:
            return {}
        marks = ",".join("?" for _ in batch_ids)
        with self._lock:
            rows = self._db.execute(
                "SELECT batch_id, origin_node, standing, issuer,"
                f" bound_at, admitted_at FROM batch_admission"
                f" WHERE batch_id IN ({marks})", list(batch_ids)).fetchall()
        return {
            batch_id: {"origin_node": origin_node,
                       "standing": bool(standing), "issuer": issuer,
                       "bound_at": bound_at, "admitted_at": admitted_at}
            for batch_id, origin_node, standing, issuer, bound_at,
            admitted_at in rows
        }

    def prune_answer_cache(self, max_batches: int) -> int:
        """Evict cached batches down to `max_batches` (§13 step 6:
        eviction by reputation). The lowest-reputation origins'
        batches go first, oldest origin age first within an origin;
        a batch's admission row dies with it. Own batches are never
        touched. Returns the batches dropped.

        Verdicts are keyed by CLAIM IDENTITY, not by batch (SF-9):
        a verdict dies in a prune only when its identity is gone
        from EVERY retained batch — the same origin's re-minted or
        re-served batch may still hold the very claim, and evicting
        one copy must not strip the strong tier (and its expiry
        exemption, and the sanity protection) from the copy that
        stays. (purge_revoked is the different discipline: there
        the identity itself is banned, so its verdicts go with it.)
        """
        with self._lock:
            total = int(self._db.execute(
                "SELECT COUNT(*) FROM batches WHERE origin = 'cache'"
            ).fetchone()[0])
            if total <= max_batches:
                return 0
            rows = self._db.execute(
                "SELECT b.batch_id, b.origin_node, b.asserted_at,"
                " COALESCE(r.checks_passed, 0)"
                " - COALESCE(r.checks_failed, 0) AS score"
                " FROM batches b LEFT JOIN reputation r"
                " ON r.origin_node = b.origin_node"
                " WHERE b.origin = 'cache'"
                " ORDER BY score ASC, b.asserted_at ASC, b.batch_id ASC"
            ).fetchall()
            doomed = rows[:total - max_batches]
            cur = self._db.cursor()
            candidates: set[tuple] = set()
            for batch_id, origin_node, _asserted, _score in doomed:
                for subject, field, value_json in cur.execute(
                        "SELECT subject, field, value_json FROM claims "
                        "WHERE batch_id = ?", (batch_id,)):
                    candidates.add(
                        (origin_node, subject, field, value_json))
            for batch_id, _origin_node, _asserted, _score in doomed:
                self._delete_claims(cur, "batch_id = ?", (batch_id,))
                cur.execute(
                    "DELETE FROM answer_cache WHERE batch_id = ?",
                    (batch_id,))
                cur.execute(
                    "DELETE FROM batch_admission WHERE batch_id = ?",
                    (batch_id,))
                cur.execute(
                    "DELETE FROM batches WHERE batch_id = ?", (batch_id,))
            for origin_node, subject, field, value_json in candidates:
                held = cur.execute(
                    "SELECT 1 FROM claims c JOIN batches b"
                    " ON b.batch_id = c.batch_id"
                    " WHERE b.origin_node = ? AND c.subject = ?"
                    " AND c.field = ? AND c.value_json = ? LIMIT 1",
                    (origin_node, subject, field, value_json)).fetchone()
                if held is None:
                    cur.execute(
                        "DELETE FROM claim_verdicts WHERE origin_node = ?"
                        " AND subject = ? AND field = ?"
                        " AND value_key = ?",
                        (origin_node, subject, field, value_json))
            self._db.commit()
            return len(doomed)

    # -- legacy SoulID aliases (§6) --------------------------------------

    def learn_legacy_alias(self, legacy: str, subject: str,
                           learned_at: float | None = None) -> None:
        """Record one VERIFIED legacy -> v2 mapping (the builder
        verifies by recompute before calling; this method only
        stores). First writer wins: a conflicting re-learn for the
        same legacy id is ignored, never overwritten — a verified
        mapping is a fact about the id, and two different verified
        mappings for one legacy id cannot both be true."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO legacy_aliases(legacy, subject,"
                " learned_at) VALUES (?, ?, ?)",
                (legacy, subject,
                 time.time() if learned_at is None else learned_at))
            self._db.commit()

    def legacy_subject(self, legacy: str) -> str | None:
        """The v2 subject a legacy id aliases to, when this node
        holds a verified mapping for it."""
        with self._lock:
            row = self._db.execute(
                "SELECT subject FROM legacy_aliases WHERE legacy = ?",
                (legacy,)).fetchone()
        return row[0] if row else None

    def legacy_alias_count(self) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM legacy_aliases").fetchone()[0])

    # -- §7 verdicts, §9 reputation, §8 sanity triggers, fetch cache -----

    def set_verdict(self, origin_node: str, subject: str, field: str,
                    value: Any, tier: str, verified_at: float,
                    detail: str | None = None) -> None:
        """Record (or refresh) one claim identity's verdict tier.
        Tiers are 'plausible' and 'strong-verified'; 'unverified' is
        the ABSENCE of a row, never a row — a failed re-check
        deletes the verdict (clear_verdict), it does not stamp a
        negative one.

        Verdicts are MONOTONIC (R2-SF-1): a lesser grade never
        overwrites a stronger stored verdict — a re-check that
        merely finds weaker evidence than last time (one provider
        unreachable today, a thinner response) changes nothing.
        Only a CONTRADICTION clears or downgrades, and that runs
        through clear_verdict (the _fail path), never through here.
        A same-or-better grade lands, refreshing verified_at to
        the newer evidence time (never backwards)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO claim_verdicts(origin_node, subject,"
                " field, value_key, tier, verified_at, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(origin_node, subject, field, value_key)"
                " DO UPDATE SET tier = excluded.tier,"
                " verified_at = MAX(claim_verdicts.verified_at,"
                " excluded.verified_at),"
                " detail = excluded.detail"
                " WHERE (CASE excluded.tier"
                "   WHEN 'strong-verified' THEN 2"
                "   WHEN 'plausible' THEN 1 ELSE 0 END)"
                " >= (CASE claim_verdicts.tier"
                "   WHEN 'strong-verified' THEN 2"
                "   WHEN 'plausible' THEN 1 ELSE 0 END)",
                (origin_node, subject, field, value_key(value), tier,
                 verified_at, detail))
            self._db.commit()

    def clear_verdict(self, origin_node: str, subject: str, field: str,
                      value: Any) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM claim_verdicts WHERE origin_node = ?"
                " AND subject = ? AND field = ? AND value_key = ?",
                (origin_node, subject, field, value_key(value)))
            self._db.commit()

    def verdicts_for_subjects(self, subjects: list[str],
                              ) -> dict[tuple, dict[str, Any]]:
        """All verdicts for these subjects, keyed by claim identity
        (origin_node, subject, field, value_key)."""
        if not subjects:
            return {}
        marks = ",".join("?" for _ in subjects)
        with self._lock:
            rows = self._db.execute(
                "SELECT origin_node, subject, field, value_key, tier,"
                f" verified_at, detail FROM claim_verdicts"
                f" WHERE subject IN ({marks})", list(subjects)).fetchall()
        return {
            (origin_node, subject, field, vk): {
                "tier": tier, "verified_at": verified_at,
                "detail": detail}
            for origin_node, subject, field, vk, tier, verified_at,
            detail in rows
        }

    def record_check(self, origin_node: str, passed: bool,
                     now: float | None = None) -> None:
        """One §7 check outcome for one origin (§9 reputation)."""
        column = "checks_passed" if passed else "checks_failed"
        with self._lock:
            self._db.execute(
                "INSERT INTO reputation(origin_node, checks_passed,"
                " checks_failed, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(origin_node) DO UPDATE SET"
                f" {column} = {column} + 1,"
                " updated_at = excluded.updated_at",
                (origin_node, 1 if passed else 0,
                 0 if passed else 1,
                 time.time() if now is None else now))
            self._db.commit()

    def reputation_for(self, origin_node: str) -> dict[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT checks_passed, checks_failed FROM reputation"
                " WHERE origin_node = ?", (origin_node,)).fetchone()
        if row is None:
            return {"checks_passed": 0, "checks_failed": 0}
        return {"checks_passed": int(row[0]), "checks_failed": int(row[1])}

    def consume_sanity_trigger(self, origin_node: str, subject: str,
                               field: str, now: float) -> bool:
        """§8's bound as a ROLLING 24 h window (R2-SF-8): True
        when this (origin, subject, field) may fire at `now` —
        no trigger for the same triple in the last 24 hours.
        A calendar day was the wrong shape twice over: it allowed
        two triggers minutes apart across midnight, and blocked
        one 23 hours after the last on the same long day. Rows
        written before fired_at existed read as long-ago."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(fired_at) FROM sanity_triggers"
                " WHERE origin_node = ? AND subject = ?"
                " AND field = ?",
                (origin_node, subject, field)).fetchone()
            last = row[0] if row else None
            if last is not None and now - float(last) < 24 * 3600:
                return False
            day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
            self._db.execute(
                "INSERT OR REPLACE INTO sanity_triggers(origin_node,"
                " subject, field, day, fired_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (origin_node, subject, field, day, now))
            self._db.commit()
            return True

    def fetch_cached(self, provider: str, cache_key: str,
                     ) -> dict[str, Any] | None:
        """One cached provider response: {fetched_at, not_found,
        payload}. Freshness is the caller's call (§7.1's 90 days)."""
        with self._lock:
            row = self._db.execute(
                "SELECT fetched_at, not_found, payload FROM fetch_cache"
                " WHERE provider = ? AND cache_key = ?",
                (provider, cache_key)).fetchone()
        if row is None:
            return None
        return {
            "fetched_at": float(row[0]), "not_found": bool(row[1]),
            "payload": json.loads(row[2]) if row[2] is not None else None,
        }

    def fetch_store(self, provider: str, cache_key: str,
                    fetched_at: float, not_found: bool,
                    payload: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO fetch_cache(provider, cache_key,"
                " fetched_at, not_found, payload) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(provider, cache_key) DO UPDATE SET"
                " fetched_at = excluded.fetched_at,"
                " not_found = excluded.not_found,"
                " payload = excluded.payload",
                (provider, cache_key, fetched_at,
                 1 if not_found else 0,
                 json.dumps(payload, ensure_ascii=False)
                 if payload is not None else None))
            self._db.commit()

    def verification_backlog(self, now: float,
                             stale_after_s: float) -> dict[str, int]:
        """Status counts: cached claims carrying no verdict at all,
        and verdicts old enough that reliance needs a re-check (§6:
        a check older than 90 days is re-verified before reliance)."""
        with self._lock:
            unverified = self._db.execute(
                "SELECT COUNT(*) FROM claims c WHERE c.origin = 'cache'"
                " AND NOT EXISTS (SELECT 1 FROM claim_verdicts v"
                " WHERE v.origin_node ="
                "   (SELECT origin_node FROM batches"
                "    WHERE batch_id = c.batch_id)"
                " AND v.subject = c.subject AND v.field = c.field"
                " AND v.value_key = c.value_json)").fetchone()[0]
            stale = self._db.execute(
                "SELECT COUNT(*) FROM claim_verdicts"
                " WHERE verified_at < ?", (now - stale_after_s,)).fetchone()[0]
        return {"unverified_claims": int(unverified),
                "stale_verdicts": int(stale)}

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
            aliases = self._db.execute(
                "SELECT COUNT(*) FROM legacy_aliases").fetchone()[0]
        return {
            "own_claims": int(own_claims),
            "own_subjects": int(own_subjects),
            "cached_batches": int(cache_batches),
            "cached_claims": int(cache_claims),
            "legacy_aliases": int(aliases),
        }

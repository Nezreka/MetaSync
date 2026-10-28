-- 0012_meta_revocations.sql — Metadata network issuance
-- (DESIGN-METADATA.md §9/§10): the revocation feed's durable state.
--
-- HOW TO RUN (once, with the deploy that ships attestation issuance +
-- GET /v1/meta/revocations in src/sin — do NOT run from an agent
-- session without Boulder): migrations must use the DIRECT / unpooled
-- connection string (DATABASE_URL_UNPOOLED, hostname WITHOUT the
-- `-pooler` suffix), never the pooled DATABASE_URL. Apply BEFORE
-- deploying the matching backend: the feed sync writes these tables
-- on every read, and the exchange's revocation carriage uses the
-- same sync.
--
--   psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0012_meta_revocations.sql
--
-- Belongs to the MetaSync repo migration sequence and must land AFTER
-- 0010_report_populations.sql.
--
-- The feed serves an issuer-signed list of revoked node ids (lowercase
-- hex SHA-256 of a node's Ed25519 public key — key status only, never
-- account information). Two facts about portal_keys make durable
-- bookkeeping necessary:
--
--   * Unbinding a key CLEARS portal_keys.bound_* (portal unbindUpdate),
--     and deleting an account cascades the key row away entirely
--     (portal_keys.user_id ON DELETE CASCADE) — the node id of an
--     unbound or deleted binding exists nowhere afterwards unless it
--     was recorded while bound.
--   * The list's sequence must never regress across restarts: holders
--     apply a list only when its seq exceeds the highest they have
--     seen, so a reused seq is silently ignored by design.
--
-- meta_seen_bindings records every node id ever observed bound to a
-- portal key. A seen id that is no longer bound anywhere IS a revoked
-- id (unbound, or its key/account deleted); a seen id that binds
-- again leaves the list, because the set is recomputed from current
-- state on every sync.
--
-- meta_revoked_nodes is the materialized form of the computed
-- revoked set, maintained by the sync (inserts/deletes against the
-- set it computes from portal_keys + meta_seen_bindings on every
-- pass); the set itself is computed, never derived from this table.
--
-- meta_revocation_state is the per-issuer sequence: one row holding
-- the seq and issued_at of the currently served list plus a hash of
-- its sorted keys, so the sync can advance the sequence ONLY when the
-- served list changes.
--
-- BOOTSTRAP: the seed at the bottom records every binding that is
-- bound at the moment this migration runs, so bindings created
-- before the feed existed are covered from the first sync on. The
-- residual gap is unknowable by construction: a binding that was
-- unbound before this migration runs has already had its bound_*
-- cleared (and one whose account was deleted has no row at all), so
-- its node id exists nowhere the migration — or anything else — can
-- read. Such a node id can never enter meta_seen_bindings and can
-- therefore never appear on the revoked list. Attestations are only
-- issued by the backend that ships with this migration, and that
-- backend records the seen row before it signs, so the gap covers
-- only bindings that never received an attestation.

CREATE TABLE IF NOT EXISTS meta_seen_bindings (
  node_id  text PRIMARY KEY,
  key_id   text        NOT NULL,
  seen_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta_revoked_nodes (
  node_id    text PRIMARY KEY,
  revoked_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta_revocation_state (
  issuer    text   PRIMARY KEY,
  seq       integer NOT NULL,
  issued_at bigint  NOT NULL,
  keys_hash text    NOT NULL
);

-- Bootstrap seed (see BOOTSTRAP in the header): every node id bound
-- right now is recorded as seen, so a later unbind or deletion of
-- one of these pre-feed bindings still lands it on the revoked
-- list. The shape guard matches the sync's own filter
-- (NODE_ID_SHAPE: a node id is the lowercase hex SHA-256 of a public
-- key). Idempotent like the rest of this migration.
INSERT INTO meta_seen_bindings (node_id, key_id)
SELECT bound_node_id, id
FROM portal_keys
WHERE bound_node_id IS NOT NULL
  AND bound_node_id ~ '^[0-9a-f]{64}$'
ON CONFLICT (node_id) DO NOTHING;

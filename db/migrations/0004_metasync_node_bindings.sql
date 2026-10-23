-- 0004_metasync_node_bindings.sql — MetaSync-only access (§3A):
-- portal-key ↔ node bindings + the request-nonce replay store.
--
-- HOW TO RUN (once, with the deploy that ships the key exchange in
-- src/sin — do NOT run from an agent session without Boulder): migrations
-- must use the DIRECT / unpooled connection string (DATABASE_URL_UNPOOLED,
-- hostname WITHOUT the `-pooler` suffix), never the pooled DATABASE_URL.
--
--   psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0004_metasync_node_bindings.sql
--
-- Belongs to the MetaSync repo migration sequence and must land AFTER
-- 0003_portal_auth.sql. Written Oct 22, 2026 from PORTAL-DESIGN.md
-- §3A.3/§3A.2; NOT yet applied to the live database.
--
-- Binding columns live ON portal_keys (design decision, §3A.3 — not a
-- separate bindings table): one key is for one install, and one
-- approved MetaSync node per key is the launch rule. bound_* is written
-- only by the portal's Approve action; pending_* holds the candidate
-- node from a first exchange attempt until a person approves it. The
-- portal app's Prisma schema mirrors these columns; SIN's Drizzle
-- schema (src/sin/schema.ts) mirrors only what the door check needs.
--
-- Privacy (design §4, P7): bindings are authentication context. No SIN
-- data table gains a column from this migration, and the nonce table
-- below holds no request body, report content, or report identifier —
-- rows expire with the ±5-minute signature window.

ALTER TABLE portal_keys
  ADD COLUMN IF NOT EXISTS bound_node_id          text,
  ADD COLUMN IF NOT EXISTS bound_node_public_key  text,
  ADD COLUMN IF NOT EXISTS bound_at               timestamptz,
  ADD COLUMN IF NOT EXISTS pending_node_id        text,
  ADD COLUMN IF NOT EXISTS pending_node_public_key text,
  ADD COLUMN IF NOT EXISTS pending_requested_at   timestamptz;

-- One node can be the approved binding of at most one key at a time.
CREATE UNIQUE INDEX IF NOT EXISTS portal_keys_bound_node_public_key_uq
  ON portal_keys (bound_node_public_key)
  WHERE bound_node_public_key IS NOT NULL;

-- Replay defence for node-signed requests (§3A.2): one row per
-- (node_id, nonce_hash) seen inside the signature window. nonce_hash is
-- SHA-256 hex of the presented nonce. Rows are deleted opportunistically
-- once expired; the expires_at index serves that cleanup.
CREATE TABLE IF NOT EXISTS sin_request_nonces (
  node_id    text        NOT NULL,
  nonce_hash text        NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (node_id, nonce_hash)
);
CREATE INDEX IF NOT EXISTS sin_request_nonces_expires_at_idx
  ON sin_request_nonces (expires_at);

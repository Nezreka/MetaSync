-- 0011_metadata_claims.sql
--
-- The metadata network's ingest storage (DESIGN-METADATA.md §6/§9/§12).
-- Deliberately separate from every SIN table: no SIN table is joined to
-- any of these, ever (Q-rules). Claims are stored against an ACCOUNT
-- BLIND — HMAC-SHA256(META_BLIND_SECRET, portal_users.id) — never a raw
-- account id, so a leak of these tables does not name contributors
-- (the operator can still re-compute blinds; Q2 states that plainly).
--
-- meta_batches: one row per accepted signed batch. digest is the
-- SHA-256 of the canonical batch payload (the value the node signed);
-- it is the batch's identity — a re-sent batch conflicts and is a
-- duplicate, never a second helping of support.
--
-- meta_claims: one row per (subject, field, value, blind). value_hash
-- is the SHA-256 of the canonical JSON of the claim value; the unique
-- index makes re-assertion by the same account a no-op. Support counts
-- are always COUNT(DISTINCT account_blind) over these rows.
--
-- Retention (§9): published claims' blind mappings live 90 days from
-- received_at (the compiler freezes counts after that); the purge is
-- the compiler worker's job, not this migration's.

-- NOTE: no node_id column here, deliberately. A raw node id would let
-- anyone reading these tables walk batch -> portal_keys.bound_node_id
-- -> account with no blind secret at all. The blind is the only
-- identity the metadata tables hold.
CREATE TABLE IF NOT EXISTS meta_batches (
  digest       text PRIMARY KEY,
  account_blind text NOT NULL,
  claim_count  integer NOT NULL,
  accepted     integer NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS meta_batches_blind_received_idx
  ON meta_batches (account_blind, received_at);

CREATE TABLE IF NOT EXISTS meta_claims (
  id           bigserial PRIMARY KEY,
  subject      text NOT NULL,
  kind         text NOT NULL,
  field        text NOT NULL,
  value        jsonb NOT NULL,
  value_hash   text NOT NULL,
  source       text NOT NULL,
  account_blind text NOT NULL,
  batch_digest text NOT NULL REFERENCES meta_batches(digest),
  received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS meta_claims_identity_uq
  ON meta_claims (subject, field, value_hash, account_blind);
CREATE INDEX IF NOT EXISTS meta_claims_subject_field_idx
  ON meta_claims (subject, field);
CREATE INDEX IF NOT EXISTS meta_claims_blind_received_idx
  ON meta_claims (account_blind, received_at);
CREATE INDEX IF NOT EXISTS meta_claims_batch_digest_idx
  ON meta_claims (batch_digest);

-- The contributor gate (§5/§12): a per-account flag the OPERATOR sets
-- in the portal admin after manual review. Portal signup is open to
-- anyone, so this flag — not signup, not node binding — is the Sybil
-- wall for contributing. contributor_approved_at drives the backfill
-- allowance window (§6: 50k claims/day for the first 30 days after
-- approval, 5k/day steady state after).
ALTER TABLE portal_users
  ADD COLUMN IF NOT EXISTS contributor_approved boolean NOT NULL DEFAULT false;
ALTER TABLE portal_users
  ADD COLUMN IF NOT EXISTS contributor_approved_at timestamptz;

-- One node may be the approved binding of at most one LIVE key.
--
-- 0004 made the rule cover every key, revoked ones included. A revoked
-- key keeps its bound_* columns (the revocation list is computed from
-- them), so it kept holding its node:
--   * Regenerate, which carries the approved node onto the replacement
--     key, always failed for a key with an approved node;
--   * after revoking a key, the same MetaSync could not be approved on
--     a new one (the owner had no way to unbind a revoked key).
-- PORTAL-DESIGN.md and the Prisma schema comment always described the
-- rule as "on unrevoked keys"; this makes the index match.
--
-- Safe to run on a live database: the new rule allows everything the
-- old one allowed. Apply BEFORE deploying the matching portal (its
-- Regenerate revokes the old key and then creates the new one, which
-- only this index permits).

BEGIN;

DROP INDEX IF EXISTS portal_keys_bound_node_public_key_uq;

CREATE UNIQUE INDEX portal_keys_bound_node_public_key_uq
  ON portal_keys (bound_node_public_key)
  WHERE bound_node_public_key IS NOT NULL AND revoked_at IS NULL;

COMMIT;

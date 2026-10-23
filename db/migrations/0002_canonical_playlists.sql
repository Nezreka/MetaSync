-- 0002_canonical_playlists.sql — SIN canonical (shared) playlist storage.
--
-- HOW TO RUN (once, with the deploy that ships the §9 key split — do NOT
-- run from an agent session without Boulder): migrations must use the
-- DIRECT / unpooled connection string (DATABASE_URL_UNPOOLED, hostname
-- WITHOUT the `-pooler` suffix), never the pooled DATABASE_URL.
--
--   psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0002_canonical_playlists.sql
--
-- Matches the Drizzle schema in src/sin/schema.ts (canonicalPlaylists)
-- exactly. Written Oct 22, 2026 as part of the DESIGN-NODE.md §9 backend
-- follow-on; NOT yet applied to the live database.

CREATE TABLE IF NOT EXISTS canonical_playlists (
  week         text        NOT NULL,
  key          text        NOT NULL,
  name         text        NOT NULL,
  tracks       jsonb       NOT NULL,
  published_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week, key)
);

-- NOTE: Rows are written ONLY by POST /v1/playlists under the admin key
-- (the official MetaSync). User nodes read via GET /v1/playlists, which
-- serves the max (latest) published week.

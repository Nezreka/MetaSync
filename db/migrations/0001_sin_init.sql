-- 0001_sin_init.sql — SIN central aggregator schema.
--
-- HOW TO RUN (once Neon is signed in — do NOT run from an agent session
-- without Boulder): migrations must use the DIRECT / unpooled connection
-- string (DATABASE_URL_UNPOOLED, hostname WITHOUT the `-pooler` suffix),
-- never the pooled DATABASE_URL. Pooled connections go through PgBouncer in
-- transaction mode and break session-level migration behaviour.
--
--   psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0001_sin_init.sql
--
-- Matches the Drizzle schema in src/sin/schema.ts exactly.

CREATE TABLE IF NOT EXISTS raw_reports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  week        varchar(8)   NOT NULL,
  token_hash  varchar(128) NOT NULL,
  signal      varchar(32)  NOT NULL CHECK (signal IN
                ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand')),
  mbid        uuid         NOT NULL,
  received_at timestamptz  NOT NULL DEFAULT now()
);

-- The binary-contribution rule in the database: one node (token) can
-- contribute at most once per (week, signal, MBID).
CREATE UNIQUE INDEX IF NOT EXISTS raw_reports_binary_uq
  ON raw_reports (token_hash, week, signal, mbid);
CREATE INDEX IF NOT EXISTS raw_reports_received_at_idx
  ON raw_reports (received_at);
CREATE INDEX IF NOT EXISTS raw_reports_week_signal_mbid_idx
  ON raw_reports (week, signal, mbid);

CREATE TABLE IF NOT EXISTS aggregates (
  week       varchar(8)  NOT NULL,
  signal     varchar(32) NOT NULL CHECK (signal IN
               ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand')),
  mbid       uuid        NOT NULL,
  node_count integer     NOT NULL CHECK (node_count >= 0),
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week, signal, mbid)
);

CREATE TABLE IF NOT EXISTS index_publications (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  week             varchar(8)  NOT NULL,
  trigger          varchar(32) NOT NULL,
  aggregate_count  integer     NOT NULL,
  raw_deleted      integer     NOT NULL,
  published_at     timestamptz NOT NULL DEFAULT now()
);

-- NOTE: No MetaSync metadata tables in this migration. That schema is
-- scaffold-only (src/metasync/schema.ts) and gets its own migration when
-- the MetaSync build is commissioned.

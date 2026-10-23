-- 0008_count_on_arrival_and_listen.sql
--
-- Two changes (REVIEW: aggregation + listening, Oct 23, 2026):
--
-- 1. Count on arrival. Reports are no longer stored per node. A report's
--    token is claimed once per week in report_tokens (a second report for
--    the same week is refused), and its signals become +1s on the private
--    running counts in aggregate_counts, in the same transaction. The old
--    path (raw rows deleted 24h after receipt, then recounted) lost every
--    report that aged out between aggregation passes, so items reported
--    by k installs across more than a day never reached k.
--    raw_reports stays for now: the next aggregation pass drains any
--    leftover rows into aggregate_counts. Drop it once it is empty.
--
-- 2. The eighth signal type, listen_album (DESIGN-LISTEN-SIGNALS.md).

CREATE TABLE IF NOT EXISTS report_tokens (
  week        varchar(8)   NOT NULL,
  token_hash  varchar(128) NOT NULL,
  PRIMARY KEY (week, token_hash)
);

CREATE TABLE IF NOT EXISTS aggregate_counts (
  week        varchar(8)  NOT NULL,
  signal      varchar(32) NOT NULL,
  mbid        uuid        NOT NULL,
  node_count  integer     NOT NULL,
  PRIMARY KEY (week, signal, mbid)
);

ALTER TABLE raw_reports DROP CONSTRAINT IF EXISTS raw_reports_signal_check;
ALTER TABLE raw_reports ADD CONSTRAINT raw_reports_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add',
     'download_grab', 'download_churn', 'listen_album'));

ALTER TABLE aggregates DROP CONSTRAINT IF EXISTS aggregates_signal_check;
ALTER TABLE aggregates ADD CONSTRAINT aggregates_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add',
     'download_grab', 'download_churn', 'listen_album'));

ALTER TABLE aggregate_counts DROP CONSTRAINT IF EXISTS aggregate_counts_signal_check;
ALTER TABLE aggregate_counts ADD CONSTRAINT aggregate_counts_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add',
     'download_grab', 'download_churn', 'listen_album'));

-- 0007_download_signals.sql
--
-- Add the sixth and seventh signal types, download_grab and download_churn
-- (DESIGN-DOWNLOAD-SIGNALS.md), to the signal CHECK constraints rewritten
-- by 0005 on raw_reports and aggregates. Without this the extended
-- validation in src/sin/logic.ts accepts the new types but every insert
-- carrying one fails the database check.

ALTER TABLE raw_reports DROP CONSTRAINT IF EXISTS raw_reports_signal_check;
ALTER TABLE raw_reports ADD CONSTRAINT raw_reports_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add',
     'download_grab', 'download_churn'));

ALTER TABLE aggregates DROP CONSTRAINT IF EXISTS aggregates_signal_check;
ALTER TABLE aggregates ADD CONSTRAINT aggregates_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add',
     'download_grab', 'download_churn'));

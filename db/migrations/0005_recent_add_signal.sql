-- 0005_recent_add_signal.sql
--
-- Add the fifth signal type, recent_add (a release group added to a node's
-- library during the report week), to the signal CHECK constraints created
-- inline by 0001 on raw_reports and aggregates. Without this the extended
-- validation in src/sin/logic.ts accepts recent_add but every insert
-- carrying one fails the database check.

ALTER TABLE raw_reports DROP CONSTRAINT IF EXISTS raw_reports_signal_check;
ALTER TABLE raw_reports ADD CONSTRAINT raw_reports_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add'));

ALTER TABLE aggregates DROP CONSTRAINT IF EXISTS aggregates_signal_check;
ALTER TABLE aggregates ADD CONSTRAINT aggregates_signal_check
  CHECK (signal IN
    ('watchlist_add', 'wishlist_add', 'import_keep', 'hunt_demand', 'recent_add'));

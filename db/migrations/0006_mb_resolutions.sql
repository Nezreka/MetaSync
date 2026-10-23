-- 0006_mb_resolutions.sql
--
-- Composer resolution cache: MusicBrainz facts about charted MBIDs
-- (release groups and artists), fetched once and kept so the weekly
-- composer never asks MusicBrainz the same question twice. Public
-- catalogue data only; no user data is ever stored here.

CREATE TABLE IF NOT EXISTS mb_resolutions (
  mbid        text PRIMARY KEY,
  kind        text NOT NULL,
  bundle      jsonb NOT NULL,
  resolved_at timestamptz NOT NULL DEFAULT now()
);

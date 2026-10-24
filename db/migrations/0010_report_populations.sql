-- 0010_report_populations.sql
--
-- Loved & Kept subtracts drop signals (download_churn) from keeps
-- (import_keep). Keeps come from every reporting install, drops only from
-- installs that opted in to "Share drop signals", so the subtraction
-- undercounted drops by exactly that share. This table counts, per week,
-- how many reports arrived and how many carried churn_optin = true, so the
-- composer can scale drops up to the whole population.
--
-- Bare counts, +1 per accepted report in the same transaction that claims
-- its token. Nothing here says which install sent what.

CREATE TABLE IF NOT EXISTS report_populations (
  week         varchar(8) PRIMARY KEY,
  reports      integer    NOT NULL,
  churn_optin  integer    NOT NULL
);

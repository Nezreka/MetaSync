-- 0003_portal_auth.sql — Portal accounts + per-user API keys.
--
-- HOW TO RUN (once, with the deploy that ships portal-key support in
-- src/sin — do NOT run from an agent session without Boulder): migrations
-- must use the DIRECT / unpooled connection string (DATABASE_URL_UNPOOLED,
-- hostname WITHOUT the `-pooler` suffix), never the pooled DATABASE_URL.
--
--   psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0003_portal_auth.sql
--
-- Belongs to the MetaSync repo migration sequence and must land AFTER
-- 0002_canonical_playlists.sql. Written Oct 22, 2026 from
-- PORTAL-DESIGN.md §2.1–2.3; NOT yet applied to the live database.
--
-- These tables are the roll-call auth/key core (User, Account, Session,
-- VerificationToken, ApiKey) with billing/tiers/usage removed and names
-- prefixed `portal_*` because this database is shared with SIN. The
-- portal app's Prisma schema mirrors these tables (SQL here is the
-- source of truth, exactly as 0001/0002 are for src/sin). IDs are
-- app-generated cuids, hence plain text with no database default.
--
-- Privacy (PORTAL-DESIGN.md §4): this migration touches NO SIN table.
-- No column is added to raw_reports / aggregates referencing a key, an
-- account, an IP, or a user agent (rule P1) — a portal key authenticates
-- at the door and is never stored with a report.

CREATE TABLE IF NOT EXISTS portal_users (
  id             text PRIMARY KEY,
  email          text        NOT NULL,
  name           text,
  image          text,
  email_verified timestamptz,
  role           text        NOT NULL DEFAULT 'user',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS portal_users_email_uq
  ON portal_users (email);

CREATE TABLE IF NOT EXISTS portal_accounts (
  id                  text PRIMARY KEY,
  user_id             text NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
  type                text NOT NULL,
  provider            text NOT NULL,
  provider_account_id text NOT NULL,
  refresh_token       text,
  access_token        text,
  expires_at          integer,
  token_type          text,
  scope               text,
  id_token            text,
  session_state       text,
  UNIQUE (provider, provider_account_id)
);
CREATE INDEX IF NOT EXISTS portal_accounts_user_id_idx
  ON portal_accounts (user_id);

-- Exists because the Auth.js Prisma adapter shape includes it; the
-- portal's live sessions are JWT (roll-call does the same knowingly).
CREATE TABLE IF NOT EXISTS portal_sessions (
  id            text PRIMARY KEY,
  session_token text        NOT NULL,
  user_id       text        NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
  expires       timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS portal_sessions_token_uq
  ON portal_sessions (session_token);
CREATE INDEX IF NOT EXISTS portal_sessions_user_id_idx
  ON portal_sessions (user_id);

-- Ships even before any magic-link sign-in exists (design §5): it keeps
-- that decision reversible without a second migration.
CREATE TABLE IF NOT EXISTS portal_verification_tokens (
  identifier text        NOT NULL,
  token      text        NOT NULL,
  expires    timestamptz NOT NULL,
  PRIMARY KEY (identifier, token)
);
CREATE UNIQUE INDEX IF NOT EXISTS portal_verification_tokens_token_uq
  ON portal_verification_tokens (token);

-- Per-user pool keys (design §2.2, roll-call's ApiKey minus tiers/usage).
-- Only the HASH is stored: SHA-256 hex of API_KEY_PEPPER + raw key. The
-- raw key (sin_live_<43 chars>) exists only in the user's MetaSync and
-- in the one-time dashboard reveal. Validity rule, exactly roll-call's:
--   revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
-- There is intentionally NO is_active/status column — a single nullable
-- timestamp cannot contradict itself.
CREATE TABLE IF NOT EXISTS portal_keys (
  id           text PRIMARY KEY,
  user_id      text NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
  label        text        NOT NULL,
  key_hash     text        NOT NULL,
  key_prefix   text        NOT NULL,
  last_used_at timestamptz,
  expires_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS portal_keys_key_hash_uq
  ON portal_keys (key_hash);
CREATE INDEX IF NOT EXISTS portal_keys_user_id_idx
  ON portal_keys (user_id);

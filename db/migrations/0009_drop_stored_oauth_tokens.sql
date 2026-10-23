-- 0009_drop_stored_oauth_tokens.sql
--
-- The portal signs people in and never calls GitHub or Google for them
-- afterwards, so it no longer stores the OAuth tokens a provider returns
-- at sign-in (portal/src/lib/oauth-account.ts strips them before the
-- account row is written). This clears the tokens already stored. Only
-- the sign-in link (provider + provider account id) remains. Safe to run
-- more than once; touches no other table.

UPDATE portal_accounts
SET access_token  = NULL,
    refresh_token = NULL,
    id_token      = NULL,
    expires_at    = NULL,
    token_type    = NULL,
    scope         = NULL,
    session_state = NULL
WHERE access_token IS NOT NULL
   OR refresh_token IS NOT NULL
   OR id_token IS NOT NULL
   OR expires_at IS NOT NULL
   OR token_type IS NOT NULL
   OR scope IS NOT NULL
   OR session_state IS NOT NULL;

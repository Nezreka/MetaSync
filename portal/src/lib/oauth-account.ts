import type { AdapterAccount } from 'next-auth/adapters'

// The portal signs people in; it never calls GitHub or Google on their
// behalf afterwards. So the OAuth credentials a provider hands back at
// sign-in (access / refresh / id tokens) are dropped before the account
// row is written: only the link itself — provider + provider account id —
// is kept. A token the portal never uses is a credential nobody needs to
// guard, or to lose in a breach (DATA.md §10).
const TOKEN_FIELDS = [
  'access_token',
  'refresh_token',
  'id_token',
  'expires_at',
  'token_type',
  'scope',
  'session_state',
] as const

export function withoutOAuthTokens(account: AdapterAccount): AdapterAccount {
  const kept = { ...account } as Record<string, unknown>
  for (const field of TOKEN_FIELDS) delete kept[field]
  return kept as AdapterAccount
}

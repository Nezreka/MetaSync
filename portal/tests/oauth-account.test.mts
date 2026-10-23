import { test } from 'node:test'
import assert from 'node:assert/strict'

// oauth-account.ts is pure — no env or DB needed.
const { withoutOAuthTokens } = await import('../src/lib/oauth-account.ts')

test('only the sign-in link is stored, never the provider tokens', () => {
  const stored = withoutOAuthTokens({
    userId: 'user-1',
    type: 'oauth',
    provider: 'github',
    providerAccountId: '12345',
    access_token: 'gho_secret',
    refresh_token: 'ghr_secret',
    id_token: 'eyJ.secret',
    expires_at: 1790000000,
    token_type: 'bearer',
    scope: 'read:user user:email',
    session_state: 'state',
  })
  assert.deepEqual(stored, {
    userId: 'user-1',
    type: 'oauth',
    provider: 'github',
    providerAccountId: '12345',
  })
  assert.ok(!JSON.stringify(stored).includes('secret'))
})

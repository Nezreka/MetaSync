import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

// Dummy env MUST be set before importing the module under test.
process.env.API_KEY_PEPPER = 'unit-test-pepper'
process.env.DATABASE_URL =
  process.env.DATABASE_URL ?? 'postgresql://dummy:dummy@localhost:5432/dummy'

const {
  generateRawKey,
  hashRawKey,
  keyPrefix,
  keyRecordIsValid,
  validatePortalKey,
} = await import('../src/lib/api-keys.ts')

function sha256Hex(s: string): string {
  return createHash('sha256').update(s).digest('hex')
}

test('generateRawKey: sin_live_ prefix + 43-char base64url body, unique per call', () => {
  const a = generateRawKey()
  const b = generateRawKey()
  assert.match(a, /^sin_live_[A-Za-z0-9_-]{43}$/)
  assert.match(b, /^sin_live_[A-Za-z0-9_-]{43}$/)
  assert.notEqual(a, b)
})

test('keyPrefix: first 12 chars (sin_live_ + 3)', () => {
  const raw = generateRawKey()
  assert.equal(keyPrefix(raw), raw.slice(0, 12))
  assert.equal(keyPrefix(raw).length, 12)
  assert.ok(keyPrefix(raw).startsWith('sin_live_'))
})

test('hashRawKey: SHA-256 of pepper + raw key', () => {
  const raw = 'sin_live_example'
  assert.equal(hashRawKey(raw), sha256Hex('unit-test-pepper' + raw))
})

test('hashRawKey: different pepper produces a different hash', async () => {
  const raw = 'sin_live_example'
  const before = hashRawKey(raw)
  process.env.API_KEY_PEPPER = 'another-pepper'
  try {
    // Module caches nothing about the pepper (read per call), so this
    // reflects a changed env immediately.
    assert.equal(hashRawKey(raw), sha256Hex('another-pepper' + raw))
    assert.notEqual(hashRawKey(raw), before)
  } finally {
    process.env.API_KEY_PEPPER = 'unit-test-pepper'
  }
})

test('hashRawKey: production without a pepper fails fast', async () => {
  const prevPepper = process.env.API_KEY_PEPPER
  const prevEnv = process.env.NODE_ENV
  delete process.env.API_KEY_PEPPER
  process.env.NODE_ENV = 'production'
  try {
    assert.throws(() => hashRawKey('sin_live_x'), /API_KEY_PEPPER is required in production/)
  } finally {
    process.env.API_KEY_PEPPER = prevPepper
    process.env.NODE_ENV = prevEnv
  }
})

test('hashRawKey: outside production a missing pepper hashes with empty pepper (roll-call behavior)', () => {
  const prev = process.env.API_KEY_PEPPER
  delete process.env.API_KEY_PEPPER
  try {
    assert.equal(hashRawKey('sin_live_x'), sha256Hex('sin_live_x'))
  } finally {
    process.env.API_KEY_PEPPER = prev
  }
})

test('validity rule: active record (no revoke, no expiry) is valid', () => {
  assert.equal(keyRecordIsValid({ revokedAt: null, expiresAt: null }), true)
})

test('validity rule: revoked record is invalid', () => {
  assert.equal(keyRecordIsValid({ revokedAt: new Date(), expiresAt: null }), false)
})

test('validity rule: expired record is invalid, future expiry is valid', () => {
  const now = new Date('2026-10-22T00:00:00Z')
  assert.equal(
    keyRecordIsValid({ revokedAt: null, expiresAt: new Date('2026-10-21T00:00:00Z') }, now),
    false
  )
  assert.equal(
    keyRecordIsValid({ revokedAt: null, expiresAt: new Date('2026-10-23T00:00:00Z') }, now),
    true
  )
})

test('validatePortalKey: empty / missing input returns null without touching the DB', async () => {
  assert.equal(await validatePortalKey(''), null)
  assert.equal(await validatePortalKey(undefined as unknown as string), null)
  assert.equal(await validatePortalKey(null as unknown as string), null)
})

// The no-oracle rule (revoked vs expired vs unknown all collapse to the
// same null -> identical 401) is covered at the record level by the
// validity-rule tests above; exercising validatePortalKey's DB paths
// needs a live database and is intentionally not faked here.

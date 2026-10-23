import { createHash, randomBytes } from 'node:crypto'
import { prisma } from './prisma'

// Server-only: portal key generation, hashing, and validation.
// Keys are never stored in plaintext — only the SHA-256 hash (+ pepper).
// Copied from roll-call's src/lib/api-keys.ts, renamed for the portal:
// raw keys are `sin_live_…` (PORTAL-DESIGN.md §2.2) and the pepper env
// is API_KEY_PEPPER (the SAME value must be set in the SIN function
// env — both sides hash and compare against portal_keys.key_hash).

function pepperedHash(rawKey: string): string {
  const pepper = process.env.API_KEY_PEPPER
  // Fail fast in production: silently hashing without a pepper means adding
  // one later invalidates every existing key (full outage for all users).
  if (!pepper && process.env.NODE_ENV === 'production') {
    throw new Error('API_KEY_PEPPER is required in production')
  }
  return createHash('sha256').update((pepper ?? '') + rawKey).digest('hex')
}

/** Hash a raw key with the pepper. Exported for transactional rolls. */
export function hashRawKey(rawKey: string): string {
  return pepperedHash(rawKey)
}

/** Generate a new portal key. Returns the raw key ONCE — it cannot be recovered. */
export function generateRawKey(): string {
  const random = randomBytes(32).toString('base64url')
  return `sin_live_${random}`
}

export function keyPrefix(rawKey: string): string {
  // First 12 chars: enough to identify in the dashboard, useless for auth.
  return rawKey.slice(0, 12)
}

export async function createPortalKey(userId: string, label: string) {
  const rawKey = generateRawKey()
  const key = await prisma.portalKey.create({
    data: {
      userId,
      label,
      keyHash: pepperedHash(rawKey),
      keyPrefix: keyPrefix(rawKey),
    },
  })
  return { key, rawKey }
}

export interface KeyValidityRecord {
  revokedAt: Date | null
  expiresAt: Date | null
}

/**
 * The validity rule (PORTAL-DESIGN.md §2.2, exactly roll-call's):
 * valid iff revokedAt IS NULL AND (expiresAt IS NULL OR expiresAt > now()).
 * Pure and exported so the rule is unit-testable without a database.
 */
export function keyRecordIsValid(record: KeyValidityRecord, now: Date = new Date()): boolean {
  if (record.revokedAt) return false
  if (record.expiresAt && record.expiresAt <= now) return false
  return true
}

export interface ValidatedKey {
  id: string
  userId: string
  keyPrefix: string
}

/**
 * Validate a raw portal key. Returns the key record or null.
 * All failure modes return null — callers must return identical 401s
 * regardless of *why* validation failed (no oracle).
 */
export async function validatePortalKey(rawKey: string): Promise<ValidatedKey | null> {
  if (!rawKey || typeof rawKey !== 'string') return null
  const hash = pepperedHash(rawKey)
  const key = await prisma.portalKey.findUnique({
    where: { keyHash: hash },
    select: { id: true, userId: true, keyPrefix: true, revokedAt: true, expiresAt: true },
  })
  if (!key) return null
  if (!keyRecordIsValid(key)) return null
  return { id: key.id, userId: key.userId, keyPrefix: key.keyPrefix }
}

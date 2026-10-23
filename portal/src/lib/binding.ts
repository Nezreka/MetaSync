import { createHash } from 'node:crypto'

// Node binding state machine (PORTAL-DESIGN.md §3A.3) — pure helpers, no
// database access, so the transitions are unit-testable. The server
// actions load a key's binding fields, run them through these helpers,
// and persist the returned update payload.
//
// A key is in exactly one state:
//   unbound  — no node approved, none asking
//   pending  — a node presented its public key at exchange time and is
//              waiting for the account holder's approval (no passes
//              are issued while pending)
//   bound    — one approved node; the pool issues passes only to it
//
// The display fingerprint of a node is the first 12 characters of its
// node id, and the node id IS the SHA-256 hex of its Ed25519 public
// key (§3A.3), so the fingerprint a user sees here is directly
// comparable with the fingerprint MetaSync shows on its own screen
// (DESIGN-NODE.md §5: a 12-character hash fingerprint).

export interface KeyBindingRecord {
  boundNodeId: string | null
  boundNodePublicKey: string | null
  boundAt: Date | null
  pendingNodeId: string | null
  pendingNodePublicKey: string | null
  pendingRequestedAt: Date | null
}

export type BindingState = 'unbound' | 'pending' | 'bound'

export function bindingState(record: KeyBindingRecord): BindingState {
  if (record.boundNodePublicKey) return 'bound'
  if (record.pendingNodePublicKey) return 'pending'
  return 'unbound'
}

/** Node id for an Ed25519 public key (base64url): SHA-256 hex of the raw bytes. */
export function nodeIdForPublicKey(publicKeyBase64Url: string): string {
  const raw = Buffer.from(publicKeyBase64Url, 'base64url')
  return createHash('sha256').update(raw).digest('hex')
}

/**
 * The 12-character display fingerprint for a node. Accepts either the
 * node id (SHA-256 hex — the fingerprint is simply its first 12 chars)
 * or, via nodeIdForPublicKey, a public key. Returns null for no node.
 */
export function nodeFingerprint(nodeId: string | null | undefined): string | null {
  if (!nodeId) return null
  return nodeId.slice(0, 12)
}

export function fingerprintForPublicKey(
  publicKeyBase64Url: string | null | undefined
): string | null {
  if (!publicKeyBase64Url) return null
  return nodeFingerprint(nodeIdForPublicKey(publicKeyBase64Url))
}

const CLEAR_PENDING = {
  pendingNodeId: null,
  pendingNodePublicKey: null,
  pendingRequestedAt: null,
} as const

const CLEAR_BOUND = {
  boundNodeId: null,
  boundNodePublicKey: null,
  boundAt: null,
} as const

/**
 * Approve the pending node: pending_* moves to bound_* (boundAt stamped
 * now) and the pending fields clear. Throws if there is no pending
 * request — approving nothing must never silently "succeed".
 */
export function approveBindingUpdate(
  record: KeyBindingRecord,
  now: Date = new Date()
): {
  boundNodeId: string
  boundNodePublicKey: string
  boundAt: Date
  pendingNodeId: null
  pendingNodePublicKey: null
  pendingRequestedAt: null
} {
  if (!record.pendingNodePublicKey || !record.pendingNodeId) {
    throw new Error('No pending binding request to approve')
  }
  return {
    boundNodeId: record.pendingNodeId,
    boundNodePublicKey: record.pendingNodePublicKey,
    boundAt: now,
    ...CLEAR_PENDING,
  }
}

/** Reject the pending node: only the pending fields clear. */
export function rejectBindingUpdate(): typeof CLEAR_PENDING {
  return { ...CLEAR_PENDING }
}

/**
 * Unbind the approved node: only the bound fields clear. The next
 * exchange from any node becomes a fresh pending request that needs a
 * new approval (§3A.3 point 6 — there is no node-initiated rebinding).
 */
export function unbindUpdate(): typeof CLEAR_BOUND {
  return { ...CLEAR_BOUND }
}

/**
 * The fields a key roll carries onto the replacement key (§3A.3 point
 * 7): the APPROVED binding only, because the account holder rolled the
 * key for the same install. A pending request is NOT carried — it was
 * made against the old key and dies with it.
 */
export function bindingCarriedOnRoll(record: KeyBindingRecord): {
  boundNodeId: string | null
  boundNodePublicKey: string | null
  boundAt: Date | null
} {
  return {
    boundNodeId: record.boundNodeId,
    boundNodePublicKey: record.boundNodePublicKey,
    boundAt: record.boundAt,
  }
}

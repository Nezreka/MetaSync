import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash, generateKeyPairSync } from 'node:crypto'

// binding.ts is pure (node:crypto only) — no env or DB needed.
const {
  bindingState,
  nodeIdForPublicKey,
  nodeFingerprint,
  fingerprintForPublicKey,
  approveBindingUpdate,
  rejectBindingUpdate,
  unbindUpdate,
  bindingCarriedOnRoll,
} = await import('../src/lib/binding.ts')

// A real Ed25519 public key, exported the way the node presents it:
// raw 32-byte key, base64url (SPKI DER's last 32 bytes).
function makePublicKeyBase64Url(): string {
  const { publicKey } = generateKeyPairSync('ed25519')
  const der = publicKey.export({ type: 'spki', format: 'der' })
  return Buffer.from(der.subarray(der.length - 32)).toString('base64url')
}

const PUB = makePublicKeyBase64Url()
const NODE_ID = createHash('sha256')
  .update(Buffer.from(PUB, 'base64url'))
  .digest('hex')
const REQUESTED = new Date('2026-10-22T10:00:00Z')
const BOUND_AT = new Date('2026-10-20T10:00:00Z')

const EMPTY = {
  boundNodeId: null,
  boundNodePublicKey: null,
  boundAt: null,
  pendingNodeId: null,
  pendingNodePublicKey: null,
  pendingRequestedAt: null,
}
const PENDING = {
  ...EMPTY,
  pendingNodeId: NODE_ID,
  pendingNodePublicKey: PUB,
  pendingRequestedAt: REQUESTED,
}
const BOUND = {
  ...EMPTY,
  boundNodeId: NODE_ID,
  boundNodePublicKey: PUB,
  boundAt: BOUND_AT,
}

test('nodeIdForPublicKey: SHA-256 hex of the raw public key bytes', () => {
  assert.equal(nodeIdForPublicKey(PUB), NODE_ID)
  assert.match(nodeIdForPublicKey(PUB), /^[0-9a-f]{64}$/)
})

test('fingerprint: first 12 chars of the node id, from id or from key', () => {
  assert.equal(nodeFingerprint(NODE_ID), NODE_ID.slice(0, 12))
  assert.equal(nodeFingerprint(NODE_ID).length, 12)
  assert.equal(fingerprintForPublicKey(PUB), NODE_ID.slice(0, 12))
  // The whole point: what the portal shows equals what the node shows.
  assert.equal(fingerprintForPublicKey(PUB), nodeFingerprint(nodeIdForPublicKey(PUB)))
  assert.equal(nodeFingerprint(null), null)
  assert.equal(fingerprintForPublicKey(null), null)
})

test('bindingState: unbound / pending / bound, bound wins if both set', () => {
  assert.equal(bindingState(EMPTY), 'unbound')
  assert.equal(bindingState(PENDING), 'pending')
  assert.equal(bindingState(BOUND), 'bound')
  assert.equal(bindingState({ ...BOUND, ...PENDING, boundNodePublicKey: PUB }), 'bound')
})

test('approveBindingUpdate: pending moves to bound, pending clears, boundAt stamped', () => {
  const now = new Date('2026-10-22T12:00:00Z')
  const update = approveBindingUpdate(PENDING, now)
  assert.equal(update.boundNodeId, NODE_ID)
  assert.equal(update.boundNodePublicKey, PUB)
  assert.equal(update.boundAt, now)
  assert.equal(update.pendingNodeId, null)
  assert.equal(update.pendingNodePublicKey, null)
  assert.equal(update.pendingRequestedAt, null)
  // The resulting record reads as bound.
  assert.equal(bindingState({ ...PENDING, ...update }), 'bound')
})

test('approveBindingUpdate: throws when there is no pending request', () => {
  assert.throws(() => approveBindingUpdate(EMPTY), /No pending binding request/)
  assert.throws(() => approveBindingUpdate(BOUND), /No pending binding request/)
})

test('rejectBindingUpdate: clears pending only, bound untouched', () => {
  const update = rejectBindingUpdate()
  assert.deepEqual(update, {
    pendingNodeId: null,
    pendingNodePublicKey: null,
    pendingRequestedAt: null,
  })
  assert.equal(bindingState({ ...PENDING, ...update }), 'unbound')
  assert.equal(bindingState({ ...BOUND, ...update }), 'bound')
})

test('unbindUpdate: clears bound only, pending untouched', () => {
  const update = unbindUpdate()
  assert.deepEqual(update, {
    boundNodeId: null,
    boundNodePublicKey: null,
    boundAt: null,
  })
  assert.equal(bindingState({ ...BOUND, ...update }), 'unbound')
  assert.equal(bindingState({ ...PENDING, ...update }), 'pending')
})

test('bindingCarriedOnRoll: approved binding carries, pending does not', () => {
  assert.deepEqual(bindingCarriedOnRoll(BOUND), {
    boundNodeId: NODE_ID,
    boundNodePublicKey: PUB,
    boundAt: BOUND_AT,
  })
  assert.deepEqual(bindingCarriedOnRoll(PENDING), {
    boundNodeId: null,
    boundNodePublicKey: null,
    boundAt: null,
  })
  assert.deepEqual(bindingCarriedOnRoll(EMPTY), {
    boundNodeId: null,
    boundNodePublicKey: null,
    boundAt: null,
  })
})

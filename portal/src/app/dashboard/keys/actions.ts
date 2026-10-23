'use server'

import { auth } from '@/auth'
import { prisma } from '@/lib/prisma'
import { createPortalKey } from '@/lib/api-keys'
import { revalidatePath } from 'next/cache'

const REVALIDATE = '/dashboard/keys'

async function requireUserId(): Promise<string> {
  const session = await auth()
  const userId = session?.user?.id
  if (!userId) throw new Error('Unauthorized')
  return userId
}

type ActionResult = { rawKey?: string; newKeyId?: string; error?: string }

// PORTAL-DESIGN.md §1: one key per install, capped at 5 active per account
// (roll-call allows 20; a handful of installs is the real usage shape, and
// a low cap is cheap Sybil friction).
const MAX_KEYS_PER_USER = 5

export async function createKeyAction(label: string): Promise<ActionResult> {
  const userId = await requireUserId()
  const trimmed = label.trim().slice(0, 64)
  if (!trimmed) return { error: 'Give the key a label — e.g. “NAS” or “living-room PC”.' }

  const count = await prisma.portalKey.count({ where: { userId, revokedAt: null } })
  if (count >= MAX_KEYS_PER_USER) {
    return { error: `Key limit reached (${MAX_KEYS_PER_USER} active). Revoke an old key first.` }
  }

  const { rawKey } = await createPortalKey(userId, trimmed)
  revalidatePath(REVALIDATE)
  // The raw key is returned ONCE — it is never stored and cannot be recovered.
  return { rawKey }
}

export async function revokeKeyAction(keyId: string): Promise<void> {
  const userId = await requireUserId()
  await prisma.portalKey.updateMany({
    where: { id: keyId, userId, revokedAt: null },
    data: { revokedAt: new Date() },
  })
  revalidatePath(REVALIDATE)
}

export async function rollKeyAction(keyId: string): Promise<ActionResult> {
  const userId = await requireUserId()
  const existing = await prisma.portalKey.findFirst({
    where: { id: keyId, userId, revokedAt: null },
    select: {
      id: true,
      label: true,
      boundNodeId: true,
      boundNodePublicKey: true,
      boundAt: true,
      pendingNodeId: true,
      pendingNodePublicKey: true,
      pendingRequestedAt: true,
    },
  })
  if (!existing) return { error: 'Key not found or already revoked.' }

  // Atomic roll: create replacement + revoke old in one transaction.
  // Uses the SHARED key generation/hashing (generateRawKey + hashRawKey)
  // so the production pepper fail-fast applies here too — never reimplement.
  const { generateRawKey, hashRawKey, keyPrefix } = await import('@/lib/api-keys')
  const rawKey = generateRawKey()
  const keyHash = hashRawKey(rawKey)

  // §3A.3 point 7: the roll carries the APPROVED node binding to the
  // replacement key (same install, holder-initiated) — the bound
  // MetaSync keeps working with the new key and does not re-ask for
  // approval. A pending request is not carried; it dies with the old key.
  const { bindingCarriedOnRoll } = await import('@/lib/binding')
  const carriedBinding = bindingCarriedOnRoll(existing)

  const { newKeyId } = await prisma.$transaction(async (tx) => {
    const created = await tx.portalKey.create({
      data: {
        userId,
        label: existing.label,
        keyHash,
        keyPrefix: keyPrefix(rawKey),
        ...carriedBinding,
      },
      select: { id: true },
    })
    await tx.portalKey.update({
      where: { id: existing.id },
      data: { revokedAt: new Date() },
    })
    return { newKeyId: created.id }
  })

  // Do NOT revalidatePath here — it unmounts the KeyRow and destroys the
  // one-time raw key reveal. The client refreshes the list on dismiss.
  // Immediate revoke (v1): old key stops working when the new one is issued.
  return { rawKey, newKeyId }
}

// ── Node binding (PORTAL-DESIGN.md §3A.3) ─────────────────────────────
// Every query below is scoped by userId from the session: a user can
// only approve / reject / unbind bindings on their OWN keys. The pure
// transition payloads live in @/lib/binding (unit-tested).

export async function approveBindingAction(keyId: string): Promise<ActionResult> {
  const userId = await requireUserId()
  const key = await prisma.portalKey.findFirst({
    where: { id: keyId, userId },
    select: {
      boundNodeId: true,
      boundNodePublicKey: true,
      boundAt: true,
      pendingNodeId: true,
      pendingNodePublicKey: true,
      pendingRequestedAt: true,
    },
  })
  if (!key) return { error: 'Key not found.' }
  if (!key.pendingNodePublicKey) return { error: 'No pending binding request on this key.' }

  const { approveBindingUpdate } = await import('@/lib/binding')
  await prisma.portalKey.update({
    where: { id: keyId },
    data: approveBindingUpdate(key),
  })
  revalidatePath(REVALIDATE)
  return {}
}

export async function rejectBindingAction(keyId: string): Promise<ActionResult> {
  const userId = await requireUserId()
  const { rejectBindingUpdate } = await import('@/lib/binding')
  await prisma.portalKey.updateMany({
    where: { id: keyId, userId, pendingNodePublicKey: { not: null } },
    data: rejectBindingUpdate(),
  })
  revalidatePath(REVALIDATE)
  return {}
}

export async function unbindNodeAction(keyId: string): Promise<ActionResult> {
  const userId = await requireUserId()
  const { unbindUpdate } = await import('@/lib/binding')
  await prisma.portalKey.updateMany({
    where: { id: keyId, userId, boundNodePublicKey: { not: null } },
    data: unbindUpdate(),
  })
  revalidatePath(REVALIDATE)
  return {}
}

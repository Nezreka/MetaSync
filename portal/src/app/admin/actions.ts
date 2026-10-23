'use server'

import { prisma } from '@/lib/prisma'
import { requireAdmin } from '@/lib/admin'
import { revalidatePath } from 'next/cache'

/**
 * Admin revoke: same revoked_at mechanism as the owner's own revoke
 * (PORTAL-DESIGN.md §6 — the kill switch for an abusive key).
 * requireAdmin() runs at the top of the action itself: layouts protect
 * pages, but actions can be invoked directly and must never trust the
 * client (roll-call's rule, copied).
 */
export async function adminRevokeKeyAction(keyId: string): Promise<void> {
  await requireAdmin()
  await prisma.portalKey.updateMany({
    where: { id: keyId, revokedAt: null },
    data: { revokedAt: new Date() },
  })
  revalidatePath('/admin')
}

/**
 * Admin unbind: the SECOND abuse lever (PORTAL-DESIGN.md §3A.3 point
 * 7). Revoking kills a credential; unbinding kills a NODE — that
 * MetaSync can no longer exchange even under a fresh key until a
 * person signs in and approves a node again. Independent of revoke:
 * works on revoked keys too (frees the node slot / clears the record).
 */
export async function adminUnbindNodeAction(keyId: string): Promise<void> {
  await requireAdmin()
  const { unbindUpdate } = await import('@/lib/binding')
  await prisma.portalKey.updateMany({
    where: { id: keyId, boundNodePublicKey: { not: null } },
    data: unbindUpdate(),
  })
  revalidatePath('/admin')
}

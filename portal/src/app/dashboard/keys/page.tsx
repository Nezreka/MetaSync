import { redirect } from 'next/navigation'
import { auth } from '@/auth'
import { prisma } from '@/lib/prisma'
import { bindingState, nodeFingerprint } from '@/lib/binding'
import CreateKeyForm from './create-key-form'
import KeyRow, { type KeyRowData } from './key-row'

export const metadata = {
  title: 'API Keys — MetaSync Portal',
}

export default async function PortalKeysPage() {
  const session = await auth()
  if (!session?.user?.id) redirect('/login')

  const keys = await prisma.portalKey.findMany({
    where: { userId: session.user.id },
    orderBy: { createdAt: 'desc' },
    select: {
      id: true,
      label: true,
      keyPrefix: true,
      createdAt: true,
      lastUsedAt: true,
      revokedAt: true,
      expiresAt: true,
      boundNodeId: true,
      boundNodePublicKey: true,
      boundAt: true,
      pendingNodeId: true,
      pendingNodePublicKey: true,
      pendingRequestedAt: true,
    },
  })

  const now = new Date()
  const rows: KeyRowData[] = keys.map((k) => ({
    id: k.id,
    label: k.label,
    keyPrefix: k.keyPrefix,
    createdAt: k.createdAt,
    lastUsedAt: k.lastUsedAt,
    status: k.revokedAt ? 'revoked' : k.expiresAt && k.expiresAt <= now ? 'expired' : 'active',
    revokedAt: k.revokedAt,
    binding: {
      state: bindingState(k),
      boundFingerprint: nodeFingerprint(k.boundNodeId),
      boundAt: k.boundAt,
      pendingFingerprint: nodeFingerprint(k.pendingNodeId),
      pendingRequestedAt: k.pendingRequestedAt,
    },
  }))
  const activeCount = rows.filter((r) => r.status === 'active').length

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-white">API keys</h1>
        <p className="mt-1.5 text-sm text-white/50">
          Keys let your MetaSync program into the SIN pool. A full key is
          shown once, at creation, and never stored — only its fingerprint
          and the first 12 characters are kept, so you can tell your keys
          apart. Up to 5 active keys (one per install). Each key also
          binds to exactly one MetaSync: the first one that presents the
          key asks here, and nothing is issued to it until you approve
          its fingerprint.
        </p>
      </div>

      <CreateKeyForm />

      <div className="mt-10">
        <h2 className="kicker mb-4">
          Your keys ({activeCount} active)
        </h2>
        {rows.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-white/10 p-8 text-center">
            <p className="text-sm text-white/40">
              No keys yet. Create one above to get started.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {rows.map((k) => (
              <KeyRow key={k.id} keyData={k} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

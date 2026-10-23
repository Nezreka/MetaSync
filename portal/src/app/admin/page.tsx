import { redirect } from 'next/navigation'
import { auth } from '@/auth'
import { prisma } from '@/lib/prisma'
import { bindingState, nodeFingerprint } from '@/lib/binding'
import { AdminRevokeButton } from './revoke-button'
import { AdminUnbindButton } from './unbind-button'

export const metadata = { title: 'Admin — MetaSync Portal' }

function formatDate(d: Date): string {
  return new Date(d).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

export default async function AdminPage() {
  const session = await auth()
  if (!session?.user) redirect('/login')
  if (session.user.role !== 'admin') redirect('/dashboard')

  const [accounts, activeKeys, totalKeys] = await Promise.all([
    prisma.portalUser.findMany({
      orderBy: { createdAt: 'desc' },
      select: {
        id: true,
        email: true,
        name: true,
        role: true,
        createdAt: true,
        keys: {
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
        },
      },
    }),
    prisma.portalKey.count({ where: { revokedAt: null } }),
    prisma.portalKey.count(),
  ])

  const now = new Date()

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-10 text-white">
      <h1 className="text-2xl font-semibold tracking-tight">Admin</h1>
      <p className="mt-1.5 text-sm text-white/50">
        What the operator can see (PORTAL-DESIGN.md §4, rule P5): accounts
        exist, their keys exist and when they last checked in. No signals,
        no key values, nothing attributable — by design.
      </p>

      <div className="mt-8 grid grid-cols-3 gap-4">
        {[
          { label: 'Accounts', value: accounts.length },
          { label: 'Active keys', value: activeKeys },
          { label: 'Keys all-time', value: totalKeys },
        ].map((s) => (
          <div key={s.label} className="card-glass p-5">
            <p className="text-2xl font-semibold">{s.value}</p>
            <p className="mt-1 text-xs uppercase tracking-wider text-white/40">{s.label}</p>
          </div>
        ))}
      </div>

      <p className="mt-4 text-xs leading-relaxed text-white/35">
        Pool traffic totals (distinct reporting tokens, aggregate rows,
        publication state) live on the SIN side of the database, which this
        portal deliberately does not read — not shown here.
      </p>

      <h2 className="kicker mb-4 mt-10">
        Accounts ({accounts.length})
      </h2>
      <div className="space-y-4">
        {accounts.map((a) => (
          <div key={a.id} className="card-glass p-6">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="text-base font-semibold">
                {a.email}
                {a.role === 'admin' && (
                  <span className="ml-2 rounded-full bg-amber-400/10 px-2 py-0.5 text-[11px] font-medium text-amber-200">
                    admin
                  </span>
                )}
              </p>
              <p className="text-xs text-white/35">Joined {formatDate(a.createdAt)}</p>
            </div>
            {a.keys.length === 0 ? (
              <p className="mt-3 text-sm text-white/40">No keys.</p>
            ) : (
              <ul className="mt-3 space-y-2">
                {a.keys.map((k) => {
                  const isActive = !k.revokedAt && (!k.expiresAt || k.expiresAt > now)
                  const bState = bindingState(k)
                  const boundFp = nodeFingerprint(k.boundNodeId)
                  const pendingFp = nodeFingerprint(k.pendingNodeId)
                  return (
                    <li
                      key={k.id}
                      className="flex flex-wrap items-center justify-between gap-3 rounded-[10px] bg-black/30 px-3 py-2"
                    >
                      <span className="text-sm text-white/70">
                        <span className="font-medium text-white">{k.label}</span>{' '}
                        <span className="font-mono text-xs text-white/50">
                          {k.keyPrefix}••••••••
                        </span>{' '}
                        <span className="text-xs text-white/35">
                          {isActive ? 'active' : k.revokedAt ? 'revoked' : 'expired'} · created{' '}
                          {formatDate(k.createdAt)} ·{' '}
                          {k.lastUsedAt ? `last used ${formatDate(k.lastUsedAt)}` : 'never used'}
                          {' · '}
                          {bState === 'bound'
                            ? `bound to node ${boundFp} since ${k.boundAt ? formatDate(k.boundAt) : '—'}`
                            : bState === 'pending'
                              ? `binding pending from node ${pendingFp}`
                              : 'no node bound'}
                        </span>
                      </span>
                      <span className="flex gap-2">
                        {bState === 'bound' && (
                          <AdminUnbindButton keyId={k.id} label={k.label} fingerprint={boundFp} />
                        )}
                        {isActive && <AdminRevokeButton keyId={k.id} label={k.label} />}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        ))}
        {accounts.length === 0 && (
          <p className="text-sm text-white/40">No accounts yet.</p>
        )}
      </div>
    </div>
  )
}

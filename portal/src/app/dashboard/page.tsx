import Link from 'next/link'
import { redirect } from 'next/navigation'
import { auth } from '@/auth'
import { prisma } from '@/lib/prisma'
import { poolUrl } from '@/lib/pool'
import { CopyButton } from '@/components/copy-button'

export const metadata = { title: 'Dashboard — MetaSync Portal' }

function formatDate(d: Date): string {
  return new Date(d).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

export default async function DashboardPage() {
  const session = await auth()
  if (!session?.user?.id) redirect('/login')

  const keys = await prisma.portalKey.findMany({
    where: { userId: session.user.id },
    orderBy: { createdAt: 'desc' },
    select: { id: true, label: true, lastUsedAt: true, revokedAt: true, expiresAt: true },
  })

  const now = new Date()
  const active = keys.filter(
    (k) => !k.revokedAt && (!k.expiresAt || k.expiresAt > now)
  )
  const lastCheckIn = keys
    .map((k) => k.lastUsedAt)
    .filter((d): d is Date => d !== null)
    .sort((a, b) => b.getTime() - a.getTime())[0]

  const url = poolUrl()

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Your pool connection</h1>
      <p className="mt-1.5 text-sm text-white/50">
        Two values go into MetaSync on your machine: the pool URL below, and
        one of your keys. That is the whole connection.
      </p>

      <div className="card-glass mt-8 p-6">
        <h2 className="kicker">
          Pool URL (SIN)
        </h2>
        {url ? (
          <div className="mt-3 flex items-center gap-3">
            <code className="flex-1 break-all rounded-lg bg-black/50 px-3 py-2.5 font-mono text-sm text-white">
              {url}
            </code>
            <CopyButton text={url} />
          </div>
        ) : (
          <p className="mt-3 text-sm text-amber-200">
            The pool URL is not configured on this portal yet. Keys can still
            be created below; the address will appear here before public
            launch.
          </p>
        )}
        <p className="mt-3 text-xs leading-relaxed text-white/35">
          In MetaSync: Pools page → add pool → paste this URL and your key,
          role “node”. MetaSync shows you the exact weekly report before it
          sends anything.
        </p>
      </div>

      <div className="card-glass mt-6 p-6">
        <h2 className="kicker">
          Connection status
        </h2>
        {keys.length === 0 ? (
          <p className="mt-3 text-sm text-white/60">
            No keys yet.{' '}
            <Link href="/dashboard/keys" className="text-white underline">
              Create your first key
            </Link>{' '}
            to connect a MetaSync install.
          </p>
        ) : (
          <div className="mt-3 space-y-1.5 text-sm text-white/60">
            <p>
              <span className="text-white">{active.length}</span> active key
              {active.length === 1 ? '' : 's'} of {keys.length} total.
            </p>
            <p>
              {lastCheckIn
                ? `Last check-in ${formatDate(lastCheckIn)} — a key from this account reached the pool.`
                : 'No check-in yet — no key from this account has reached the pool. Paste a key into MetaSync and use its Test button.'}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

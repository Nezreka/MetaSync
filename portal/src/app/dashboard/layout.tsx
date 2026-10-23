import Link from 'next/link'
import { redirect } from 'next/navigation'
import { auth, signOut } from '@/auth'

const NAV = [
  { href: '/dashboard', label: 'Overview' },
  { href: '/dashboard/keys', label: 'API Keys' },
]

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const session = await auth()
  if (!session?.user) redirect('/login')

  const user = session.user

  return (
    <div className="min-h-screen text-white">
      <div className="flex min-h-screen">
        <aside className="sidebar-glass hidden w-60 shrink-0 flex-col md:flex">
          <div className="px-6 pb-4 pt-6">
            <Link href="/" className="text-lg font-semibold tracking-tight transition hover:text-white/80">
              ← MetaSync Portal
            </Link>
            <p className="mt-1 text-xs text-white/40">Your SIN pool keys</p>
          </div>

          <nav className="flex-1 space-y-1 px-3">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="block rounded-lg px-3 py-2 text-sm text-white/60 transition hover:bg-white/5 hover:text-white"
              >
                {item.label}
              </Link>
            ))}
            {user.role === 'admin' && (
              <div className="pt-2">
                <Link
                  href="/admin"
                  className="block rounded-lg border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-sm font-medium text-amber-200 transition hover:bg-amber-400/20"
                >
                  Admin
                </Link>
              </div>
            )}
          </nav>

          <div className="border-t border-white/10 px-6 py-4">
            <p className="truncate text-sm text-white/70">{user.name ?? user.email}</p>
            <p className="truncate text-xs text-white/35">{user.email}</p>
            <form
              action={async () => {
                'use server'
                await signOut({ redirectTo: '/' })
              }}
            >
              <button
                type="submit"
                className="btn-ghost mt-3 px-3 py-1.5 text-xs font-medium"
              >
                Sign out
              </button>
            </form>
          </div>
        </aside>

        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  )
}

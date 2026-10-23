import Link from 'next/link'
import { auth } from '@/auth'

export default async function LandingPage() {
  const session = await auth()

  return (
    <main className="min-h-screen px-4 py-16 text-white">
      <div className="mx-auto w-full max-w-2xl">
        <p className="kicker">
          MetaSync · SIN pool keys
        </p>
        <h1 className="mt-3 text-4xl font-semibold tracking-tight">
          Get your key. Plug your SoulSync into the network.
        </h1>
        <p className="mt-4 text-base leading-relaxed text-white/60">
          SIN — the SoulSync Intelligence Network — pools anonymous taste
          signals from SoulSync installs and turns them into global
          playlists and discovery for everyone connected. Your MetaSync
          program needs a personal API key to join the pool. This portal is
          where you get one.
        </p>

        <div className="card-glass mt-8 p-6">
          <h2 className="text-base font-semibold">How it works</h2>
          <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm leading-relaxed text-white/60">
            <li>Sign in with Google (or GitHub) — no new password.</li>
            <li>Create a key. You see it once; the portal stores only its fingerprint.</li>
            <li>Paste the pool URL + key into MetaSync on your machine. Done.</li>
          </ol>
          <p className="mt-4 text-sm leading-relaxed text-white/60">
            Your key is checked at the pool&apos;s door, then set aside —
            reports are stored under a nickname that rotates weekly and is
            never attached to your account. Revoke or regenerate your key
            any time; only the fingerprint lives in the database, so a lost
            key is replaced, never recovered.
          </p>
        </div>

        <div className="mt-8">
          {session?.user ? (
            <Link
              href="/dashboard"
              className="btn-primary inline-block px-5 py-2.5 text-sm font-semibold"
            >
              Go to your dashboard
            </Link>
          ) : (
            <Link
              href="/login"
              className="btn-primary inline-block px-5 py-2.5 text-sm font-semibold"
            >
              Sign in to get a key
            </Link>
          )}
        </div>

        <p className="mt-10 text-xs leading-relaxed text-white/30">
          New to MetaSync? The explainer lives at{' '}
          <a href="https://www.ssync.net/metasync/" className="underline transition hover:text-white/60">
            ssync.net/metasync
          </a>
          . This portal (metasync.ssync.net) is only for accounts and keys —
          ssync.net itself has no sign-in.
        </p>
      </div>
    </main>
  )
}

import Link from 'next/link'

export const metadata = { title: 'Terms of Service — MetaSync Portal' }

const h2 = 'mt-10 text-xl font-semibold tracking-tight'
const p = 'mt-3 text-sm leading-relaxed text-white/60'

export default function TermsPage() {
  return (
    <main className="min-h-screen px-4 py-16 text-white">
      <div className="mx-auto w-full max-w-2xl">
        <p className="kicker">MetaSync</p>
        <h1 className="mt-3 text-4xl font-semibold tracking-tight">Terms of Service</h1>
        <p className="mt-3 text-xs uppercase tracking-widest text-white/40">
          Effective date: October 29, 2026
        </p>
        <p className={p}>
          MetaSync is part of the SoulSync project: free, open-source software by
          BoulderBadgeDad. These terms cover the MetaSync portal
          (metasync.ssync.net), the SIN pool (the SoulSync Intelligence
          Network), and
          the metadata network relay. By creating a portal account or connecting
          a node, you agree to them.
        </p>

        <h2 className={h2}>The service</h2>
        <p className={p}>
          MetaSync is beta software. It is provided free and &quot;as is&quot;,
          without warranties of any kind. Features may change or be
          discontinued, and availability is not guaranteed.
        </p>

        <h2 className={h2}>Your account and keys</h2>
        <p className={p}>
          You are responsible for your portal account and for every key you
          create. A key is shown once, so store it somewhere safe: because the
          key itself is never stored, a lost key cannot be recovered, only
          revoked and replaced. If a key is compromised, revoke it in the
          portal; revocation takes effect on the next request.
        </p>

        <h2 className={h2}>Fair use of the networks</h2>
        <p className={p}>
          Do not abuse the pool or the relay. That includes flooding the
          services, attempting to distort the shared counts or the shared
          metadata, probing or attacking other nodes, and using the networks for
          anything unlawful. Rate limits apply to reporting, asking, and
          answering. We may revoke keys or nodes that abuse the service.
        </p>

        <h2 className={h2}>Your library stays yours</h2>
        <p className={p}>
          SoulSync and MetaSync run on your own machine, on your own library.
          Nothing in these terms gives us any claim on your music, your files,
          or your library data. The software is open source.
        </p>

        <h2 className={h2}>Liability</h2>
        <p className={p}>
          To the maximum extent permitted by law, MetaSync, the SoulSync
          project, and their author are not liable for any indirect, incidental,
          or consequential damages arising out of the use of this service or the
          software.
        </p>

        <h2 className={h2}>Changes to these terms</h2>
        <p className={p}>
          These terms may change. The current version will always be posted on
          this page with its effective date, and changes take effect when
          posted.
        </p>

        <h2 className={h2}>Contact</h2>
        <p className={p}>
          Questions about any of this:{' '}
          <a
            href="mailto:broque.n.roll@gmail.com"
            className="text-white underline transition hover:text-white/70"
          >
            broque.n.roll@gmail.com
          </a>
          .
        </p>

        <p className="mt-12 border-t border-white/10 pt-6 text-xs text-white/30">
          <Link href="/" className="underline transition hover:text-white/60">
            MetaSync Portal
          </Link>{' '}
          ·{' '}
          <Link href="/privacy" className="underline transition hover:text-white/60">
            Privacy Policy
          </Link>
        </p>
      </div>
    </main>
  )
}

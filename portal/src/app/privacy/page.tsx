import Link from 'next/link'

export const metadata = { title: 'Privacy Policy — MetaSync Portal' }

const h2 = 'mt-10 text-xl font-semibold tracking-tight'
const p = 'mt-3 text-sm leading-relaxed text-white/60'

export default function PrivacyPage() {
  return (
    <main className="min-h-screen px-4 py-16 text-white">
      <div className="mx-auto w-full max-w-2xl">
        <p className="kicker">MetaSync</p>
        <h1 className="mt-3 text-4xl font-semibold tracking-tight">Privacy Policy</h1>
        <p className="mt-3 text-xs uppercase tracking-widest text-white/40">
          Effective date: October 29, 2026
        </p>
        <p className={p}>
          MetaSync is part of the SoulSync project: free, open-source software by
          BoulderBadgeDad. This policy covers the online services: the MetaSync
          portal at metasync.ssync.net, SIN (the SoulSync Intelligence Network),
          and the metadata network relay. SoulSync and MetaSync themselves run on
          your own machine, under your control; this policy is about the services
          they can connect to.
        </p>

        <h2 className={h2}>Signing in to the portal</h2>
        <p className={p}>
          Sign-in is handled by Google or GitHub. When you sign in, that provider
          sends us your name, email address, and profile picture. We use them
          only to create and run your portal account and to show you your keys.
          We never receive or store your Google or GitHub password, and we do
          not store their access tokens.
        </p>

        <h2 className={h2}>API keys</h2>
        <p className={p}>
          When you create a key in the portal, the raw key is shown to you once.
          We store a SHA-256 fingerprint (a hash) of it, plus its first few
          characters so you can tell your keys apart; the key itself can never
          be read back or recovered from our database. You can revoke your keys
          in the portal at any time; revoking stops the key from working,
          though its fingerprint record is kept. A key works only from
          the node you bind it to, and unbinding a node removes that
          node&apos;s access, effective on its next request.
        </p>

        <h2 className={h2}>SIN: anonymous counts, opt-in only</h2>
        <p className={p}>
          Joining SIN is optional and off by default. A node that joins sends one
          report per week for the week that just ended: anonymous counts of
          library activity signals, for example how many releases were
          wishlisted, added, downloaded, kept, or listened to, identified by
          MusicBrainz IDs rather than names (listening counts are a separate
          opt-in, off by default). Reports are authenticated with your
          node&apos;s key
          so the pool knows a real node is reporting, but the counts are not
          attached to your portal account, and reports are counted on arrival
          rather than stored. A count is published only once at least 15
          different nodes have reported the same item, which keeps published
          data from pointing back at any single install. Nothing is sold, and nothing is
          shared with advertisers. SIN exists to build community charts that
          enrich SoulSync itself.
        </p>

        <h2 className={h2}>The metadata network</h2>
        <p className={p}>
          The metadata network&apos;s servers store no library metadata. The
          relay is a switchboard: it forwards signed questions between nodes
          that are online and returns answers that are sealed (encrypted to the
          node that asked), so the relay cannot read the answers and keeps no
          message content. The answers come from other users&apos; nodes, out of
          their own libraries, and your own node decides which questions it
          answers and which answers it trusts.
        </p>

        <h2 className={h2}>Cookies</h2>
        <p className={p}>
          The portal uses only the cookies needed to keep you signed in. There
          are no advertising cookies and no third-party trackers.
        </p>

        <h2 className={h2}>Keeping and deleting your data</h2>
        <p className={p}>
          Your portal account holds your sign-in details and, for each key,
          its label, its first characters, its fingerprint, and when it was
          last used. For a key bound to one of your nodes, or a node asking to
          bind to it, it also holds that node&apos;s public key and
          fingerprint. You can revoke your keys in
          the portal at any time. To delete your account entirely, email{' '}
          <a
            href="mailto:broque.n.roll@gmail.com"
            className="text-white underline transition hover:text-white/70"
          >
            broque.n.roll@gmail.com
          </a>{' '}
          and we will remove it. The anonymous SIN counts that have already
          been combined into past tallies cannot be separated back out; that
          is what makes them anonymous.
        </p>

        <h2 className={h2}>Changes to this policy</h2>
        <p className={p}>
          If this policy changes, the new version will be posted on this page
          with a new effective date.
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
          <Link href="/terms" className="underline transition hover:text-white/60">
            Terms of Service
          </Link>
        </p>
      </div>
    </main>
  )
}

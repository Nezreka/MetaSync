import NextAuth from 'next-auth'
import { PrismaAdapter } from '@auth/prisma-adapter'
import { prisma } from '@/lib/prisma'
import { authConfig } from './auth.config'
import { withoutOAuthTokens } from '@/lib/oauth-account'

// @auth/prisma-adapter hard-codes the DEFAULT model accessors
// (p.user / p.account / p.session / p.verificationToken). The portal's
// models are Portal* (PORTAL-DESIGN.md §2.1), so the adapter gets a
// delegate shim mapping its expected accessors onto ours. Field shapes
// are identical to roll-call's models, which is what the adapter was
// written against. (authenticator/WebAuthn delegates are not mapped —
// no WebAuthn provider is enabled.)
const adapterClient = {
  user: prisma.portalUser,
  account: prisma.portalAccount,
  session: prisma.portalSession,
  verificationToken: prisma.portalVerificationToken,
}

const baseAdapter = PrismaAdapter(adapterClient as Parameters<typeof PrismaAdapter>[0])

// Store the sign-in link, never the provider's tokens (lib/oauth-account.ts).
const adapter: typeof baseAdapter = {
  ...baseAdapter,
  linkAccount: (account) => baseAdapter.linkAccount!(withoutOAuthTokens(account)),
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  ...authConfig,
  adapter,
  // Use JWT sessions so the role rides in the token (edge-readable).
  // The adapter still persists users/accounts for OAuth linking.
  session: { strategy: 'jwt' },
  events: {
    // ADMIN_EMAILS bootstrap (roll-call's pattern): a first sign-in whose
    // email is on the list is promoted to role = "admin". No separate
    // admin login, no hardcoded account. (Roll-call also creates a free
    // subscription row here — the portal has no billing, so it doesn't.)
    async createUser({ user }) {
      const adminEmails = (process.env.ADMIN_EMAILS ?? '')
        .split(',')
        .map((e) => e.trim().toLowerCase())
        .filter(Boolean)
      const isAdmin = adminEmails.includes(user.email?.toLowerCase() ?? '')

      if (isAdmin) {
        await prisma.portalUser.update({
          where: { id: user.id },
          data: { role: 'admin' },
        })
      }
    },
  },
})

import type { NextAuthConfig } from 'next-auth'
import Google from 'next-auth/providers/google'
import GitHub from 'next-auth/providers/github'

// Edge-safe config: no PrismaAdapter, no DB imports. This file can run in
// middleware. The full config in auth.ts extends this with the adapter.
// (Copied from roll-call's src/auth.config.ts.)
//
// Boulder's sign-in decision (Oct 22, 2026): Google sign-in like
// roll-call. Roll-call's config enables Google + GitHub — both are wired
// here the same way, Google listed first (primary).
export const authConfig: NextAuthConfig = {
  // Required in production behind any proxy (Vercel, Railway, etc.).
  // Without this, every auth request throws UntrustedHost.
  trustHost: true,
  providers: [
    Google({
      // Identity only — no offline access, so no refresh tokens are stored.
      authorization: { params: { scope: 'openid email profile' } },
    }),
    GitHub({
      authorization: { params: { scope: 'read:user user:email' } },
    }),
    // NOTE: no allowDangerousEmailAccountLinking. GitHub doesn't guarantee
    // verified emails, so auto-linking on email match is an account-takeover
    // vector. Users who hit OAuthAccountNotLinked get a branded error page
    // telling them which provider they originally used.
  ],
  callbacks: {
    async jwt({ token, user }) {
      // Persist role in the JWT so edge middleware can read it without a DB hit.
      if (user) {
        token.role = (user as { role?: string }).role ?? 'user'
        token.id = user.id
      }
      return token
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.id = token.id as string
        session.user.role = (token.role as string) ?? 'user'
      }
      return session
    },
  },
  pages: {
    signIn: '/login',
    error: '/auth/error',
  },
}

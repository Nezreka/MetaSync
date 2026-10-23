import { authConfig } from './auth.config'
import NextAuth from 'next-auth'

// Edge-safe middleware: uses authConfig (no PrismaAdapter, no DB).
// Protects /dashboard/* and /admin/* — unauthenticated users redirect to /login.
// Admin-only gating (role === 'admin') happens in the admin layout itself,
// since the JWT carries the role and the layout is a server component.
export const { auth: middleware } = NextAuth(authConfig)

export const config = {
  matcher: ['/dashboard/:path*', '/admin/:path*'],
}

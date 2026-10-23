import { redirect } from 'next/navigation'
import { auth } from '@/auth'

/**
 * Assert the current session belongs to an admin.
 * Call this at the top of EVERY admin server action — layouts protect pages,
 * but actions can be invoked directly and must never trust the client.
 * Returns the user id on success; redirects otherwise.
 * (Copied from roll-call's src/lib/admin.ts.)
 */
export async function requireAdmin(): Promise<string> {
  const session = await auth()
  if (!session?.user) redirect('/login')
  if (session.user.role !== 'admin') redirect('/dashboard')
  return session.user.id
}

/** Is this email on the ADMIN_EMAILS bootstrap list? (Used by tests and docs.) */
export function isAdminEmail(email: string | null | undefined): boolean {
  if (!email) return false
  const adminEmails = (process.env.ADMIN_EMAILS ?? '')
    .split(',')
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean)
  return adminEmails.includes(email.toLowerCase())
}

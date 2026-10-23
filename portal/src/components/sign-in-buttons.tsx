'use client'

import { signIn } from 'next-auth/react'
import { useState } from 'react'

const ERROR_MESSAGES: Record<string, string> = {
  OAuthAccountNotLinked:
    'This email is already linked to a different sign-in method. Use the provider you originally signed up with.',
  OAuthSignin: 'Could not start sign-in. Please try again.',
  OAuthCallback: 'Sign-in failed. Please try again.',
  AccessDenied: 'Access denied.',
  Default: 'Something went wrong during sign-in. Please try again.',
}

export function SignInButtons({
  callbackUrl = '/dashboard',
  error,
}: {
  callbackUrl?: string
  error?: string | null
}) {
  const [pending, setPending] = useState<string | null>(null)

  return (
    <div>
      {error && (
        <p className="mb-4 rounded-lg border border-red-400/25 bg-red-400/10 px-3 py-2 text-sm text-red-300">
          {ERROR_MESSAGES[error] ?? ERROR_MESSAGES.Default}
        </p>
      )}
      <div className="space-y-3">
        <button
          type="button"
          disabled={pending !== null}
          onClick={() => {
            setPending('google')
            void signIn('google', { callbackUrl })
          }}
          className="btn-primary w-full px-4 py-2.5 text-sm font-semibold"
        >
          {pending === 'google' ? 'Redirecting…' : 'Sign in with Google'}
        </button>
        <button
          type="button"
          disabled={pending !== null}
          onClick={() => {
            setPending('github')
            void signIn('github', { callbackUrl })
          }}
          className="btn-ghost w-full px-4 py-2.5 text-sm font-medium"
        >
          {pending === 'github' ? 'Redirecting…' : 'Sign in with GitHub'}
        </button>
      </div>
      <p className="mt-4 text-center text-xs leading-relaxed text-white/35">
        Google is the primary sign-in — the same setup as roll-call. No new
        password is created or stored; the provider confirms who you are.
      </p>
    </div>
  )
}

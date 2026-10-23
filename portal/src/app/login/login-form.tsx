'use client'

import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import { SignInButtons } from '@/components/sign-in-buttons'

/** Full-page sign-in form, for direct /login visits and OAuth error recovery (?error=). */
export function LoginForm() {
  const searchParams = useSearchParams()
  const error = searchParams.get('error')
  const callbackUrl = searchParams.get('callbackUrl') ?? '/dashboard'

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-[400px]">
        <div className="card-glass p-8">
          <p className="text-lg font-semibold tracking-tight text-white">MetaSync Portal</p>

          <h1 className="mt-6 text-xl font-semibold tracking-tight text-white">Sign in</h1>
          <p className="mt-1.5 text-sm leading-relaxed text-white/50">
            Sign in to create and manage your SIN pool keys.
          </p>

          <div className="mt-6">
            <SignInButtons callbackUrl={callbackUrl} error={error} />
          </div>
        </div>

        <p className="mt-6 text-center text-xs text-white/30">
          <Link href="/" className="transition hover:text-white/60">
            ← Back to the MetaSync portal
          </Link>
        </p>
      </div>
    </main>
  )
}

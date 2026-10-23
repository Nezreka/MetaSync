import Link from 'next/link'

const MESSAGES: Record<string, { title: string; body: string }> = {
  OAuthAccountNotLinked: {
    title: 'Already signed up with a different provider',
    body: 'This email is linked to another sign-in method. Please use the provider you originally signed up with.',
  },
  OAuthSignin: {
    title: 'Could not start sign-in',
    body: 'The sign-in provider returned an error. Please try again.',
  },
  OAuthCallback: {
    title: 'Sign-in failed',
    body: 'Something went wrong during sign-in. Please try again.',
  },
  AccessDenied: {
    title: 'Access denied',
    body: 'You do not have permission to sign in.',
  },
  Verification: {
    title: 'Verification failed',
    body: 'The verification link is invalid or has expired.',
  },
  Default: {
    title: 'Something went wrong',
    body: 'An unexpected error occurred during sign-in. Please try again.',
  },
}

export default async function AuthErrorPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>
}) {
  const { error } = await searchParams
  const { title, body } = MESSAGES[error ?? ''] ?? MESSAGES.Default

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="card-glass w-full max-w-sm p-8 text-center">
        <h1 className="text-xl font-semibold tracking-tight text-white">{title}</h1>
        <p className="mt-2 text-sm text-white/50">{body}</p>
        <Link
          href="/login"
          className="btn-primary mt-6 inline-block px-4 py-2 text-sm font-semibold"
        >
          Back to sign-in
        </Link>
      </div>
    </main>
  )
}

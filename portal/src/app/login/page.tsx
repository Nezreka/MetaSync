import { Suspense } from 'react'
import { redirect } from 'next/navigation'
import { auth } from '@/auth'
import { LoginForm } from './login-form'

export const metadata = { title: 'Sign in — MetaSync Portal' }

export default async function LoginPage() {
  const session = await auth()
  if (session?.user) redirect('/dashboard')

  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  )
}

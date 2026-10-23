import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'MetaSync Portal — Get your SIN pool key',
  description:
    'Sign in to get your personal MetaSync API key for the SIN pool: anonymous community signals and global playlists for your SoulSync.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}

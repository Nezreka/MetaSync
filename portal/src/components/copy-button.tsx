'use client'

import { useState } from 'react'

export function CopyButton({
  text,
  label = 'Copy',
  onCopied,
}: {
  text: string
  label?: string
  onCopied?: () => void
}) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={async () => {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        onCopied?.()
        setTimeout(() => setCopied(false), 2000)
      }}
      className="btn-ghost px-3 py-1.5 text-xs font-medium"
    >
      {copied ? 'Copied ✓' : label}
    </button>
  )
}

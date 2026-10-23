'use client'

import { useState } from 'react'
import { createKeyAction } from './actions'
import { CopyButton } from '@/components/copy-button'

export function RawKeyReveal({ rawKey, onDismiss }: { rawKey: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="rounded-xl border border-amber-400/30 bg-amber-400/[0.06] p-5">
      <p className="text-sm font-medium text-amber-200">
        This is the only time you&apos;ll see this key. Store it in your
        MetaSync node now — lost it? Regenerate: the old one stops working.
      </p>
      <div className="mt-3 flex items-center gap-3">
        <code className="flex-1 break-all rounded-lg bg-black/50 px-3 py-2.5 font-mono text-sm text-white">
          {rawKey}
        </code>
        <CopyButton text={rawKey} label="Copy" onCopied={() => setCopied(true)} />
      </div>
      <button
        type="button"
        onClick={onDismiss}
        disabled={!copied}
        title={copied ? undefined : 'Copy the key first'}
        className="btn-primary mt-4 px-4 py-2 text-sm font-semibold"
      >
        I&apos;ve saved it
      </button>
      {!copied && (
        <p className="mt-2 text-xs text-white/40">Copy the key above to continue.</p>
      )}
    </div>
  )
}

export default function CreateKeyForm() {
  const [label, setLabel] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rawKey, setRawKey] = useState<string | null>(null)

  if (rawKey) {
    return <RawKeyReveal rawKey={rawKey} onDismiss={() => { setRawKey(null); setLabel('') }} />
  }

  return (
    <form
      className="card-glass p-6"
      onSubmit={async (e) => {
        e.preventDefault()
        setPending(true)
        setError(null)
        try {
          const result = await createKeyAction(label)
          if (result.error) setError(result.error)
          else if (result.rawKey) setRawKey(result.rawKey)
        } catch {
          setError('Something went wrong creating the key.')
        } finally {
          setPending(false)
        }
      }}
    >
      <h2 className="text-base font-semibold text-white">Create a new key</h2>
      <p className="mt-1 text-sm text-white/40">
        One key per MetaSync install — label it for the machine it lives on.
      </p>

      <div className="mt-4">
        <label htmlFor="key-label" className="block text-xs font-medium text-white/50">
          Key label
        </label>
        <input
          id="key-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. NAS, living-room PC"
          maxLength={64}
          className="mt-1.5 w-full rounded-[10px] border border-white/10 bg-black/40 px-3 py-2 text-sm text-white placeholder:text-white/25 focus:border-[#1ed760]/60 focus:outline-none"
        />
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

      <button
        type="submit"
        disabled={pending || !label.trim()}
        className="btn-primary mt-5 w-full px-4 py-2.5 text-sm font-semibold"
      >
        {pending ? 'Creating…' : 'Create key'}
      </button>
    </form>
  )
}

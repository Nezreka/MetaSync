'use client'

import { useState } from 'react'
import {
  approveBindingAction,
  rejectBindingAction,
  revokeKeyAction,
  rollKeyAction,
  unbindNodeAction,
} from './actions'
import { RawKeyReveal } from './create-key-form'

export interface KeyBindingData {
  state: 'unbound' | 'pending' | 'bound'
  boundFingerprint: string | null
  boundAt: Date | null
  pendingFingerprint: string | null
  pendingRequestedAt: Date | null
}

export interface KeyRowData {
  id: string
  label: string
  keyPrefix: string
  createdAt: Date
  lastUsedAt: Date | null
  status: 'active' | 'revoked' | 'expired'
  revokedAt: Date | null
  binding: KeyBindingData
}

function formatDate(d: Date): string {
  return new Date(d).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

type PendingAction = 'revoke' | 'roll' | 'approve' | 'reject' | 'unbind'

export default function KeyRow({ keyData }: { keyData: KeyRowData }) {
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [rolledKey, setRolledKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function runBindingAction(
    kind: 'approve' | 'reject' | 'unbind',
    fn: () => Promise<{ error?: string }>
  ) {
    setPending(kind)
    setError(null)
    try {
      const result = await fn()
      if (result.error) setError(result.error)
    } catch {
      setError('Something went wrong updating the binding.')
    } finally {
      setPending(null)
    }
  }

  if (rolledKey) {
    return (
      <div className="card-glass p-6">
        <p className="text-sm text-white/60">
          Old key revoked. Here&apos;s the replacement for{' '}
          <span className="font-medium text-white">{keyData.label}</span> — the
          MetaSync install using the old key will be refused until you paste
          this one in:
        </p>
        <div className="mt-4">
          <RawKeyReveal
            rawKey={rolledKey}
            onDismiss={() => {
              setRolledKey(null)
              // Refresh the list now — the old key is revoked, new one live.
              // (We skip revalidatePath in the action so the reveal survives.)
              window.location.reload()
            }}
          />
        </div>
      </div>
    )
  }

  const busy = pending !== null
  const isActive = keyData.status === 'active'

  return (
    <div className={`card-glass p-6 ${isActive ? '' : 'opacity-60'}`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h3 className="truncate text-base font-semibold text-white">{keyData.label}</h3>
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                keyData.status === 'active'
                  ? 'bg-emerald-400/10 text-emerald-300'
                  : keyData.status === 'revoked'
                    ? 'bg-red-400/10 text-red-300'
                    : 'bg-white/10 text-white/50'
              }`}
            >
              {keyData.status}
            </span>
          </div>
          <p className="mt-1.5 font-mono text-sm text-white/50">
            {keyData.keyPrefix}••••••••
          </p>
          <p className="mt-1.5 text-xs text-white/35">
            Created {formatDate(keyData.createdAt)}
            {' · '}
            {keyData.lastUsedAt ? `Last used ${formatDate(keyData.lastUsedAt)}` : 'Never used yet'}
            {keyData.revokedAt ? ` · Revoked ${formatDate(keyData.revokedAt)}` : ''}
          </p>
        </div>

        {isActive && (
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (
                  !window.confirm(
                    `Regenerate "${keyData.label}"?\n\nA new key will be issued and the old one revoked immediately. The MetaSync install using the old key stops working until you paste the new one in.`
                  )
                )
                  return
                setPending('roll')
                setError(null)
                try {
                  const result = await rollKeyAction(keyData.id)
                  if (result.error) setError(result.error)
                  else if (result.rawKey) setRolledKey(result.rawKey)
                } catch {
                  setError('Something went wrong regenerating the key.')
                } finally {
                  setPending(null)
                }
              }}
              className="btn-ghost px-3.5 py-1.5 text-sm font-medium"
            >
              {pending === 'roll' ? 'Regenerating…' : 'Regenerate'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (
                  !window.confirm(
                    `Revoke "${keyData.label}"?\n\nThis key will stop working immediately. This cannot be undone.`
                  )
                )
                  return
                setPending('revoke')
                setError(null)
                try {
                  await revokeKeyAction(keyData.id)
                } catch {
                  setError('Something went wrong revoking the key.')
                  setPending(null)
                }
              }}
              className="btn-danger px-3.5 py-1.5 text-sm font-medium"
            >
              {pending === 'revoke' ? 'Revoking…' : 'Revoke'}
            </button>
          </div>
        )}
      </div>
      {/* Node binding (PORTAL-DESIGN.md §3A.3): the pool only issues
          passes to the ONE MetaSync bound to this key. */}
      {keyData.binding.state === 'pending' ? (
        <div className="mt-4 rounded-xl border border-amber-400/30 bg-amber-400/[0.06] p-4">
          <p className="text-sm font-medium text-amber-200">
            A MetaSync is asking to bind to this key
          </p>
          <p className="mt-1.5 text-sm leading-relaxed text-white/60">
            Node fingerprint{' '}
            <span className="font-mono text-white">{keyData.binding.pendingFingerprint}</span>
            {keyData.binding.pendingRequestedAt
              ? ` · requested ${formatDate(keyData.binding.pendingRequestedAt)}`
              : ''}
            . Compare it with the fingerprint that MetaSync shows on its
            own screen — approve only if they match. Until you approve,
            the pool issues it nothing.
          </p>
          {isActive && (
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (
                    !window.confirm(
                      `Approve this MetaSync for "${keyData.label}"?\n\nFingerprint: ${keyData.binding.pendingFingerprint}\n\nOnly approve if this matches the fingerprint shown on that MetaSync's screen.`
                    )
                  )
                    return
                  void runBindingAction('approve', () => approveBindingAction(keyData.id))
                }}
                className="btn-primary px-3.5 py-1.5 text-sm font-semibold"
              >
                {pending === 'approve' ? 'Approving…' : 'Approve this MetaSync'}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void runBindingAction('reject', () => rejectBindingAction(keyData.id))}
                className="btn-ghost px-3.5 py-1.5 text-sm font-medium"
              >
                {pending === 'reject' ? 'Rejecting…' : 'Reject'}
              </button>
            </div>
          )}
        </div>
      ) : keyData.binding.state === 'bound' ? (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-black/30 px-4 py-3">
          <p className="text-sm text-white/60">
            <span className="text-[#4ade80]">●</span> MetaSync bound · fingerprint{' '}
            <span className="font-mono text-white">{keyData.binding.boundFingerprint}</span>
            {keyData.binding.boundAt ? ` · since ${formatDate(keyData.binding.boundAt)}` : ''}
          </p>
          {isActive && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                if (
                  !window.confirm(
                    `Unbind the MetaSync from "${keyData.label}"?\n\nThat MetaSync can no longer get pool passes — any outstanding pass dies on its next pool call. A replacement MetaSync will have to ask for approval again.`
                  )
                )
                  return
                void runBindingAction('unbind', () => unbindNodeAction(keyData.id))
              }}
              className="btn-ghost px-3 py-1 text-xs font-medium"
            >
              {pending === 'unbind' ? 'Unbinding…' : 'Unbind node'}
            </button>
          )}
        </div>
      ) : isActive ? (
        <p className="mt-4 text-xs leading-relaxed text-white/35">
          No MetaSync bound yet. The first MetaSync that presents this
          key at the pool will appear here for your approval — nothing is
          issued to it before that.
        </p>
      ) : null}

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    </div>
  )
}

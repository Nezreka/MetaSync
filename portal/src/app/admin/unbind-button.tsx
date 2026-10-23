'use client'

import { useState } from 'react'
import { adminUnbindNodeAction } from './actions'

export function AdminUnbindButton({
  keyId,
  label,
  fingerprint,
}: {
  keyId: string
  label: string
  fingerprint: string | null
}) {
  const [pending, setPending] = useState(false)
  return (
    <button
      type="button"
      disabled={pending}
      onClick={async () => {
        if (
          !window.confirm(
            `Unbind the node from "${label}" as admin?\n\nThe MetaSync bound to this key (fingerprint ${fingerprint ?? 'unknown'}) can no longer get pool passes, even under a new key, until the account holder signs in and approves a node again. The key itself stays as it is.`
          )
        )
          return
        setPending(true)
        try {
          await adminUnbindNodeAction(keyId)
        } finally {
          setPending(false)
        }
      }}
      className="btn-ghost px-3 py-1 text-xs font-medium"
    >
      {pending ? 'Unbinding…' : 'Unbind node'}
    </button>
  )
}

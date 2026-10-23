'use client'

import { useState } from 'react'
import { adminRevokeKeyAction } from './actions'

export function AdminRevokeButton({ keyId, label }: { keyId: string; label: string }) {
  const [pending, setPending] = useState(false)
  return (
    <button
      type="button"
      disabled={pending}
      onClick={async () => {
        if (
          !window.confirm(
            `Revoke "${label}" as admin?\n\nThis key stops working at the pool immediately. This cannot be undone.`
          )
        )
          return
        setPending(true)
        try {
          await adminRevokeKeyAction(keyId)
        } finally {
          setPending(false)
        }
      }}
      className="btn-danger px-3 py-1 text-xs font-medium"
    >
      {pending ? 'Revoking…' : 'Revoke'}
    </button>
  )
}

// The pool URL shown on the dashboard. This is configuration, not a
// constant: at launch it may be the raw Neon function URL; once the
// pool.ssync.net Cloudflare Worker exists (PORTAL-DESIGN.md §7.4) this
// env value becomes https://pool.ssync.net and every dashboard updates
// with it. Unset = not configured yet, and the UI says so honestly.
export function poolUrl(): string | null {
  const v = process.env.POOL_URL?.trim()
  return v ? v : null
}

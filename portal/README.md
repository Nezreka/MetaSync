# MetaSync Portal

The sign-in website where SoulSync users get their **personal SIN pool
API key**. Lives (when deployed) at **metasync.ssync.net**. Sign in with
Google (primary) or GitHub — the same NextAuth setup as roll-call —
create a key, paste it into the MetaSync program on your machine, done.

**Status: built locally, NOT deployed.** No migrations have been run
against the live database from this app, nothing is pushed.

Design docs (in the parent directory):

- `../PORTAL-EXPLAINED.md` — the plain-English version
- `../PORTAL-DESIGN.md` — the technical spec this app follows (§2 data
  model, §5 sign-in, §8 build order)

## What it does

- **Landing** (`/`) — what SIN is, how keys work, sign-in button.
- **Sign-in** (`/login`) — "Sign in with Google" / "Sign in with
  GitHub". No passwords are created or stored; the provider is the ID
  check. First sign-in creates the account; emails on `ADMIN_EMAILS`
  are promoted to admin (roll-call's bootstrap).
- **Dashboard** (`/dashboard`) — the pool URL (from `POOL_URL` env)
  with a copy button, plus connection status derived from your keys'
  `last_used_at` ("last check-in").
- **Keys** (`/dashboard/keys`) — list (label, prefix, status, created,
  last used), create with **show-once** reveal, revoke, and regenerate
  (atomic create + revoke). Max **5 active keys** per account — one per
  install. Each key also shows its **node binding**
  (PORTAL-DESIGN.md §3A.3): the first MetaSync that presents the key at
  the pool appears as a *pending* request with its node fingerprint;
  the owner approves (fingerprint compared against MetaSync's own
  screen) or rejects it, and can unbind a bound node at any time.
  Regenerating a key carries the approved binding to the replacement.
- **Admin** (`/admin`, admin role only) — accounts list with their
  keys and each key's binding state, two separate abuse levers
  (revoke-any-key, unbind-any-node), and portal counts. SIN traffic
  totals are deliberately *not* read by this app.

The UI follows the **SoulSync music dashboard theme** (Boulder's
rule): tokens adopted from SoulSync dev's `webui/static/tokens.css`
(accent `#1db954` / light `#1ed760`, font stacks, radii, status
colors), the body gradient from `webui/static/style.css`, and the
glass-card treatment from `webui/static/dashboard-premier.css` — see
the header comment in `src/app/globals.css`.

## Keys, in one paragraph

A key looks like `sin_live_` + 43 random characters. The database
stores **only its fingerprint** — SHA-256 of `API_KEY_PEPPER + raw
key`, hex — plus the first 12 characters for display. A key is valid
iff `revoked_at IS NULL AND (expires_at IS NULL OR expires_at >
now())`. The raw key is shown exactly once at creation; if it's lost,
regenerate. This is roll-call's `src/lib/api-keys.ts` design, copied.

## Data model

`prisma/schema.prisma` contains **only the portal models**
(`PortalUser`, `PortalAccount`, `PortalSession`,
`PortalVerificationToken`, `PortalKey` → tables `portal_users`,
`portal_accounts`, `portal_sessions`, `portal_verification_tokens`,
`portal_keys`).

⚠️ **The SQL migrations are the source of truth for the database
shape**: `../db/migrations/0003_portal_auth.sql` (written by the SIN
backend task) creates these tables in the same Neon database SIN uses,
and `../db/migrations/0004_metasync_node_bindings.sql` adds the
node-binding columns on `portal_keys`. This Prisma schema is a client
mirror of that design — including the binding fields, but NOT the
partial unique index on `bound_node_public_key`, which Prisma cannot
express and which is migration-owned (see the comment in
`prisma/schema.prisma`). Do not run `prisma migrate` / `db push`
against the live database from here.

One implementation note: `@auth/prisma-adapter` hard-codes the default
model accessors (`user`, `account`, …), so `src/auth.ts` passes it a
small delegate shim mapping those names onto the `Portal*` models.

## Run locally

```bash
npm install          # also runs `prisma generate`
cp .env.example .env # fill in real values for a real database/OAuth
npm run dev          # http://localhost:3001
npm test             # unit tests for the key module (node:test + tsx)
npm run build        # production build
```

The build and tests run fine with dummy env values (see `.env.test`);
auth itself cannot be exercised without real Google/GitHub OAuth apps
and a database.

## Taking it live — Boulder-only steps

(From PORTAL-DESIGN.md §8.1, in order.)

**Secrets first:**

1. Generate the pepper once: `openssl rand -hex 32`. Set it as
   `API_KEY_PEPPER` in the portal's Vercel env **and** in the SIN
   function's env — the **same value in both**. The portal hashes keys
   with it at creation; SIN hashes presented keys with it at the door.
   A mismatch means every portal key fails verification while shared
   keys keep working. Never change it afterwards without invalidating
   every issued key.

**Google sign-in (Google Cloud Console):**

2. Create an OAuth 2.0 client (Web application).
   - Authorized JavaScript origin: `https://metasync.ssync.net`
   - **Authorized redirect URI: `https://metasync.ssync.net/api/auth/callback/google`**
   - (For local dev also add `http://localhost:3001/api/auth/callback/google`.)
3. Put the client ID/secret into Vercel env as `AUTH_GOOGLE_ID` /
   `AUTH_GOOGLE_SECRET`.

**GitHub sign-in (GitHub → Settings → Developer settings → OAuth Apps):**

4. New OAuth app: homepage `https://metasync.ssync.net`, authorization
   callback URL `https://metasync.ssync.net/api/auth/callback/github`.
   Client ID/secret → `AUTH_GITHUB_ID` / `AUTH_GITHUB_SECRET`.

**Vercel:**

5. Create the portal project (root directory `portal/` in the
   MetaSync repo), set env: `DATABASE_URL` (pooled Neon string for the
   shared MetaSync database), `AUTH_SECRET` (`openssl rand -base64
   32`), `ADMIN_EMAILS` (your email), `API_KEY_PEPPER` (step 1),
   `AUTH_GOOGLE_*`, `AUTH_GITHUB_*`, `POOL_URL` (the raw SIN function
   URL until step 7 exists).
6. Attach the custom domain `metasync.ssync.net`.

**Cloudflare:**

7. DNS: add CNAME `metasync` → `cname.vercel-dns.com` (follow the
   proxy guidance Vercel shows when attaching the domain).
8. Before public launch: the `pool.ssync.net` Worker in front of the
   SIN function (PORTAL-DESIGN.md §7.4), then set `POOL_URL` to
   `https://pool.ssync.net` so users never store the raw Neon URL.

**Database:**

9. Migrations `0003_portal_auth.sql` **and**
   `0004_metasync_node_bindings.sql` must be applied (with the
   unpooled connection string, in order, after `0002`) as part of the
   SIN backend deploy — see the parent repo's migration notes. Not
   from this app. Without `0004`, the binding columns don't exist and
   the keys/admin pages (which read them) will fail.

**Smoke test (the launch proof):** sign up → create key → paste into a
MetaSync node → Test button succeeds → portal dashboard shows the
check-in time.

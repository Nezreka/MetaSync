# MetaSync API-Key Portal — Design

**New here? Read [PORTAL-EXPLAINED.md](PORTAL-EXPLAINED.md) first** — the
same system in plain English. This document is the technical version.

**Status: DESIGN ONLY.** Nothing in this document is built, deployed, or
migrated. Written Oct 22, 2026.

**Purpose.** Before MetaSync goes any further, users need a self-serve way
to get a **pool API key**. Today the SIN pool accepts one shared node key
(`SIN_INGEST_KEY`) — fine for development, wrong for distribution: it
can't be revoked per user, can't be rate-limited per user, and handing it
out means handing it out forever. The portal fixes that: a user signs up,
gets a **personal key**, pastes it into their MetaSync node, and can
regenerate or revoke it themselves.

**The pieces:**

| Piece | Where | What it is |
|---|---|---|
| Info page | `ssync.net/metasync` | Static page on the existing GitHub Pages site (`Nezreka/soulsync-website`, framework-free HTML). Explains MetaSync (the metadata project) and SIN, and how to connect. Links to the portal. **No login here — a static site cannot run one.** |
| Portal app | `metasync.ssync.net` | A small Next.js app (Vercel, like roll-call) with signup/login, a dashboard, and key management. This is where keys are issued. |
| SIN backend | Neon Functions (existing) | `src/sin` in this repo, same Neon project (`fragrant-mouse-42130917`), **same database** as the portal. Accepts per-user portal keys for verification, and under §3A gains the key-exchange endpoint, short-lived access tokens, node-signature and binding checks, and authenticated reads. |
| MetaSync node | User's machine (existing) | The Python node (`node/`, see `DESIGN-NODE.md`). Under Boulder's flow the user enters the **pool URL + personal key in SoulSync**; SoulSync hands the key to the local MetaSync over the key-authenticated local handoff (§3A.5). MetaSync's Pools page becomes connection status/display and a fallback/manual setup surface, not the primary place the key is entered. |

**Access-rule amendment (Oct 22, 2026).** Boulder's rule is:
**“SIN should only be accessible by SoulSync via MetaSync.”** §3A is
the controlling design for pool access wherever it differs from the
key-only flow described earlier in this document. In the target design,
a personal key is entered in SoulSync, handed to that install's local
MetaSync, and exchanged — together with MetaSync's node signature and
approved node binding — for a short-lived access token. Routine pool
calls use the token plus a fresh node signature, never the raw key.

**Decisions still PENDING Boulder** (marked inline, collected in §10):
the friendly pool URL timing (§7.4) and whether Phase 2 adds a
pool-visible SoulSync pairing assertion (§3A.6). Sign-in is settled:
Google, like roll-call (§5).

---

## 1. User flow, end to end

1. **Land.** User reads about MetaSync/SIN at `ssync.net/metasync` (static
   page) and clicks "Get a pool key" → `https://metasync.ssync.net`.
2. **Sign up / log in.** With Google, like roll-call (§5).
   First sign-in creates the account row; if the email is on the
   `ADMIN_EMAILS` list the account is created with `role = "admin"`
   (roll-call's bootstrap, §6).
3. **Dashboard.** Shows, for the SIN pool:
   - the **pool URL** (one canonical value, with a copy button — see
     §7.4 for what that value is at launch),
   - the user's **keys list**: label, prefix (e.g. `sin_live_Ab3…`),
     created date, last-used date, status, and — under §3A — its
     MetaSync binding state (unbound, pending approval, or the bound
     node's fingerprint). **Not the full key** — see the show-once
     decision below,
   - a **"Create key"** form (label only: "living-room PC", "NAS", …),
   - **connection status** per key: derived from `last_used_at` (§3.3) —
     "last check-in 3 days ago" / "never used yet".
4. **Create key → show once.** The full key is displayed **exactly
   once**, on the creation screen, with a copy button and a plain
   warning: *"This is the only time you'll see this key. Store it in
   your MetaSync node now. Lost it? Regenerate — the old one stops
   working."*
5. **Enter the key in SoulSync.** In SoulSync's MetaSync settings the
   user adds the pool URL (from step 3) and pastes the key. SoulSync
   sends the key to the install's local MetaSync over the local
   handoff in §3A.5, authenticating with MetaSync's own local API
   key — the reverse of the SoulSync `sk_` key MetaSync already
   uses to read SoulSync. MetaSync's Pools page shows
   the resulting pool, key prefix, binding and token status; it remains
   available as a fallback/manual setup surface. (This refines step B
   of the node's two-step connection model — connecting MetaSync to
   SoulSync is still its own separate step and joins nothing by itself;
   see `DESIGN-NODE.md` §1 and §3A.5.)
6. **Approve the node binding, then exchange.** On MetaSync's first
   exchange attempt for an unbound key, the pool records the node's
   public key as *pending* and issues no token. The portal shows that
   pending node under the key; the user compares its fingerprint with
   the fingerprint in MetaSync/SoulSync and approves it. The next
   exchange verifies the key fingerprint, the node signature and the
   approved binding, returns a short-lived access token, and sets
   `last_used_at`, so the dashboard flips to "connected." MetaSync then
   proves the working token with `GET /v1/whoami` (§3.4, as amended by
   §3A). Failures remain one undifferentiated "key not recognized"
   state to the caller (§3.2, no oracle).
7. **Steady state.** The node sends its weekly report and daily pulls
   with the **access token plus a fresh node signature**, never the raw
   key. Each accepted authenticated call refreshes `last_used_at`
   (§3.3). The dashboard's "last check-in" is the user's proof their
   node is alive — and the only usage trace that exists anywhere (§4).
8. **Regenerate (roll).** Dashboard button on a key: creates a
   replacement key with the same label and **revokes the old key in the
   same transaction** (roll-call's `rollKeyAction` pattern). The new
   key is shown once. The old key and any access tokens issued from it
   die immediately under §3A.4. The approved node binding is copied to
   the replacement key because the signed-in user performed the roll;
   the user enters the new key in SoulSync and MetaSync exchanges it
   without a second binding approval. A separate **unbind node** action
   remains available when the install itself should no longer be
   trusted. Dashboard copy must say all of this plainly.
9. **Revoke.** Dashboard button on a key: sets `revoked_at`. No
   replacement. The key cannot exchange again, and §3A's per-request
   key-status check kills outstanding tokens immediately rather than
   letting them live out their expiry. Revoked keys stay listed
   (greyed, with the revoke date) so the user has a record; they can be
   hidden, never resurrected.
10. **Multiple installs.** A user with two SoulSync installs creates two
    labeled keys, each bound to that install's MetaSync node —
    revoking the NAS's key or unbinding its node doesn't touch the PC.
    Cap: **5 active keys per account** at launch (roll-call allows 20;
    MetaSync users have a handful of installs at most, and a low cap is
    cheap Sybil friction, §6).

### Show-once vs always-visible — decision: **show once**

Roll-call's pattern, kept. The database stores only a **hash** of the
key (§2), so always-visible is not a display choice — it would require
storing raw keys, which ends the hash-only guarantee the whole privacy
and breach story rests on. If a key leaks from an always-visible store,
every key leaks. Show-once costs one moment of user care at creation;
regenerate is the recovery path and it is one click. The dashboard
thereafter identifies keys by **prefix + label + last-used**, which is
all a user actually needs to tell their keys apart.

---

## 2. Data model

Same Neon database as SIN (project `fragrant-mouse-42130917`, branch
`production`). The portal's tables live alongside `raw_reports`,
`aggregates`, `index_publications`, `canonical_playlists`. SIN's tables
are owned by this repo's SQL migrations; the portal's tables are added
by one more migration in the same sequence (§2.3) and mirrored in the
portal app's Prisma schema (models below). **The portal's Prisma schema
contains only the portal models** — Prisma must never be pointed at
this database with a schema that claims the SIN tables.

The model is roll-call's auth/key core
(`~/workspace/roll-call/prisma/schema.prisma`, models `User`,
`Account`, `Session`, `VerificationToken`, `ApiKey`) with everything
the portal doesn't need removed: no `Subscription`, no `ApiTier`, no
`ApiUsage`, no `UserProfile`, no billing fields of any kind.

### 2.1 Accounts (mirrors roll-call, simplified)

```prisma
model PortalUser {
  id            String    @id @default(cuid())
  email         String    @unique          // stored lowercased by the app
  name          String?
  image         String?
  emailVerified DateTime?
  role          String    @default("user") // "user" | "admin"
  createdAt     DateTime  @default(now())
  updatedAt     DateTime  @updatedAt

  accounts PortalAccount[]
  sessions PortalSession[]
  keys     PortalKey[]
}

model PortalAccount {              // OAuth linking (Auth.js Prisma adapter shape)
  id                String @id @default(cuid())
  userId            String
  type              String
  provider          String
  providerAccountId String
  refresh_token     String? @db.Text
  access_token      String? @db.Text
  expires_at        Int?
  token_type        String?
  scope             String?
  id_token          String? @db.Text
  session_state     String?

  user PortalUser @relation(fields: [userId], references: [id], onDelete: Cascade)

  @@unique([provider, providerAccountId])
  @@index([userId])
}

model PortalSession {
  id           String   @id @default(cuid())
  sessionToken String   @unique
  userId       String
  expires      DateTime

  user PortalUser @relation(fields: [userId], references: [id], onDelete: Cascade)

  @@index([userId])
}

model PortalVerificationToken {    // only used by the magic-link option (§5)
  identifier String                // email
  token      String   @unique
  expires    DateTime

  @@unique([identifier, token])
}
```

Notes, all deliberate copies of roll-call behaviour:

- **Names are prefixed `Portal*`** in the Prisma schema (mapped to
  `portal_users` etc. via `@@map`) so the shared database stays
  self-describing and a future reader never confuses portal accounts
  with SIN data. (Roll-call's tables are unprefixed because it owns its
  database; the portal doesn't.)
- **`PortalSession` exists but sessions are JWT.** Roll-call configures
  `session: { strategy: 'jwt' }` in `src/auth.ts` while the adapter
  still persists users/accounts; the session table is adapter schema,
  not the live session store. The portal copies this knowingly: role
  rides in the JWT so middleware never hits the DB.
- **`PortalVerificationToken` ships in the migration even if magic
  link loses** (§5) — it's two columns of foresight that keeps the
  sign-in decision reversible without a second migration.
- No demographics/profile table. Roll-call's `UserProfile` exists for
  aggregate crosstabs on votes; the portal has no equivalent need and
  §4 forbids collecting it.

### 2.2 `portal_keys` — the key table

Roll-call's `ApiKey` model, minus tiers/usage, renamed:

```prisma
model PortalKey {
  id         String    @id @default(cuid())
  userId     String
  label      String                        // user-chosen: "NAS", "living-room PC"
  keyHash    String    @unique             // SHA-256(pepper + rawKey), hex
  keyPrefix  String                        // first 12 chars of the raw key
  lastUsedAt DateTime?
  expiresAt  DateTime?                     // null at launch; field ships for later
  createdAt  DateTime  @default(now())
  updatedAt  DateTime  @updatedAt
  revokedAt  DateTime?

  user PortalUser @relation(fields: [userId], references: [id], onDelete: Cascade)

  @@index([userId])
}
```

- **Validity is exactly roll-call's rule:** `revokedAt IS NULL AND
  (expiresAt IS NULL OR expiresAt > now())`. There is intentionally
  **no `status`/`isActive` boolean** — roll-call's schema comment says
  why: a single nullable timestamp cannot contradict itself.
- **Hash: SHA-256 over `pepper + rawKey`**, copied from roll-call's
  `pepperedHash` in `src/lib/api-keys.ts`. The pepper is an env secret
  (`API_KEY_PEPPER` in roll-call) that **must exist in production**
  (roll-call throws at startup without it — same fail-fast here),
  because adding a pepper later invalidates every issued key. The
  pepper must be **the same value in the portal app and in the SIN
  function** — both hash presented/created keys and compare against
  `portal_keys.key_hash`. (Portal env: `API_KEY_PEPPER`; SIN env:
  same value, same name — one secret, two deployments, §8.)
  SHA-256 (not bcrypt/argon2) is the right choice here and is
  roll-call's: the raw key is 256 bits of machine-generated entropy,
  not a human password, so a fast hash + pepper is not brute-forceable
  and keeps verification a single indexed lookup.
- **Raw key format** mirrors roll-call's `generateRawKey`:
  `randomBytes(32)` → base64url, prefixed by purpose:
  **`sin_live_<43 chars>`**. The prefix makes a leaked key greppable
  (GitHub secret scanning can be taught the pattern) and makes the
  first-12-chars `keyPrefix` (`sin_live_Ab…`) self-identifying in the
  dashboard.
- **One key vs many — decision: many, capped at 5 active.**
  One-key-per-user is simpler but breaks the real usage shape: users
  run multiple installs, and the only recovery for a key pasted into
  the wrong place would be killing every install at once. Labeled
  per-install keys give per-install revoke, per-install last-used, and
  a dashboard that answers "is my NAS checking in?" — at the cost of
  one extra column (`label`) over the single-key design.

#### Access-rule amendment — node binding lives on `portal_keys`

Decision: **columns on `portal_keys`, not a separate bindings table**
for launch. A portal key is already per-install, and the access rule
needs exactly one approved MetaSync node for that key at a time. §3A.3
specifies a forward migration (`0004`) adding the approved and pending
public-key/node-id fields, timestamps, and the partial unique index.
The portal Prisma model mirrors those columns. A bindings table would
add history and multi-node-per-key flexibility SIN does not need; if
either becomes a product requirement, the current columns are the
current row of that future table.

### 2.3 Migration (spec)

Following this repo's existing style (`db/migrations/`: plain SQL,
numbered, `IF NOT EXISTS`, header comment with run instructions,
applied with the **unpooled** connection string):

- **File:** `db/migrations/0003_portal_auth.sql` — creates
  `portal_users`, `portal_accounts`, `portal_sessions`,
  `portal_verification_tokens`, `portal_keys` (with the unique index
  on `key_hash` and the `user_id` index). Header notes it belongs to
  the MetaSync repo sequence and must land **after `0002`** (0002 is
  written but, at the time of this design, not yet applied live — the
  sequence order still holds).
- **Access-rule follow-on:** `db/migrations/0004_metasync_node_bindings.sql`
  (spec in §3A.3) adds the node-binding columns and request-nonce table
  without rewriting `0003`. It lands after `0003`; the portal Prisma
  schema and SIN Drizzle schema both mirror only the columns each side
  is allowed to use.
- The portal app's Prisma schema (§2.1–2.2) is a **mirror for the
  client**, not a second migration system: SQL in this repo is the
  source of truth for the database shape, exactly as `src/sin`'s
  Drizzle schema mirrors `0001`/`0002` today.
- (Housekeeping found while writing this: `db/migrations/README.md`
  still says "No migration has been run yet", which stopped being
  true when `0001` was applied live on Oct 22, 2026. Worth a one-line
  fix whenever `0003` is added — noted, not done here.)

---

## 3. How SIN verifies a per-user key

> **Post-access-rule reading.** This section describes the underlying
> key verification. Under §3A, that portal-key check moves to the
> exchange endpoint: `POST /v1/report`, the index, playlists and
> whoami do **not** accept a raw portal key directly once MetaSync-only
> enforcement is switched on. They accept the short-lived token issued
> by the exchange, plus the bound node's fresh request signature.
> Direct env-key behaviour below remains a bootstrap/operator path,
> not the public MetaSync access path.

### 3.1 The door check at `POST /v1/report`

Today `requireNodeKey` in `src/sin/index.ts` does timing-safe compares
of the `X-SIN-Key` header against two env values. The change extends —
does not replace — that gate, in this order:

1. **Admin key** (`SIN_ADMIN_KEY`) — timing-safe compare, as today.
   Passes (official MetaSync, §9 of `DESIGN-NODE.md`).
2. **Shared node key** (`SIN_INGEST_KEY`) — timing-safe compare, as
   today. Passes. **Kept for dev and Boulder's own testing**; it is no
   longer the key handed to the public once the portal is live (the
   node treats all pool keys identically — a string in its Pools page —
   so nothing in the node changes when a user swaps shared → personal).
3. **Portal key** — hash the presented value
   (`SHA-256(API_KEY_PEPPER + presented)`) and look up
   `portal_keys.key_hash`. Passes iff a row exists and is valid per
   §2.2's rule. This is roll-call's `validateApiKey` in
   `src/lib/api-keys.ts`, transplanted: one indexed query, all failure
   modes collapse to the same outcome.
4. Otherwise → **`401 {error: "unauthorized"}`**, identical body and
   timing class for "no such key", "revoked", and "expired" — **no
   oracle** (roll-call's stated rule for the same function).

Fail-closed behaviour is preserved and extended: today the route 503s
when neither env key is configured. New rule: the route 503s only when
**no** verification path exists — neither env keys configured **and**
no database. In practice the report insert already requires the DB, so
portal-key verification adds no new availability assumption: a report
was never acceptable without the database anyway.

**Drizzle change:** `src/sin/schema.ts` gains a read-mostly
`portalKeys` table definition (columns: `id`, `user_id`, `key_hash`,
`last_used_at`, `expires_at`, `revoked_at`) so the function can query
it. SIN never inserts portal keys and never reads `portal_users` at
all — the join to identity is not SIN's business (§4, rule P3).

### 3.2 Rate limiting, per key

Roll-call rate-limits per key id via Upstash Redis sliding windows
(`src/lib/rate-limit.ts`). SIN's shape is different — a healthy node
sends **one report per week** — so the limits are tripwires, not
quotas:

| Caller | Limit (design values) | Why |
|---|---|---|
| Portal key | 10 reports / hour, 50 / day | A node needs 1/week; 50/day absorbs retries and clock skew while making a stolen key useless for flooding |
| Portal key, `GET /v1/whoami` | 60 / hour | The node's test button + daily refresh (§3.3) |
| Shared / admin key | Same report caps, keyed by a fixed bucket id | Boulder's own use is the same shape |
| Per IP (all write routes) | Existing coarse behaviour + Cloudflare edge rules if abuse appears (§7.1) | Backstop, not the mechanism |

Under §3A the same tripwires apply to the **key + bound-node pair**:
exchange attempts get a separate, lower bucket (a healthy node needs
roughly one exchange/day), while report/whoami limits count token-
authenticated calls. A valid raw key presented from an unbound or
wrong node must not be able to burn the legitimate node's quota
without also failing closed; repeated bad-signature exchanges are
therefore IP- and pending-binding-rate-limited as an abuse backstop.

Implementation: reuse roll-call's Upstash pattern if Redis is added
to the SIN deployment, or a Postgres fixed-window counter table if
not — **either way the counter is keyed by `portal_keys.id` (or the
fixed bucket for env keys), never by anything in the report body.**
Counters expire with their window; nothing about *what* was reported
is ever counted per key (see §4, rule P5 — a per-key counter of
*requests* is fine, a per-key record of *signals* is forbidden).

What rate limiting cannot do, said plainly: it does not stop Sybil
token inflation by itself. The dedupe token is derived from the
**node's own key** (`sha256(node_key + week)`, `DESIGN-NODE.md` §5),
not from the portal key — one portal key could front many node keys.
The real defences are the **k = 15 threshold** (an item needs 15
distinct tokens before anything publishes), one account per verified
email (§6), and admin revoke as the kill switch. The portal key is a
door key, not a proof of personhood; the design doesn't pretend
otherwise.

### 3.3 `last_used_at` without linking identity to reports

The mechanism is roll-call's `touchKeyLastUsed`, simplified:

- On a **successful portal-key verification** (report or whoami), SIN
  issues one conditional update:
  `UPDATE portal_keys SET last_used_at = now() WHERE id = $1 AND
  (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')`.
  The throttle is in the `WHERE` clause, so no Redis marker is needed
  (roll-call uses Redis for this; SIN doesn't have Redis today, and
  one conditional `UPDATE` per key-hour is cheaper than adding it).
  The update is fire-and-forget: its failure never fails the report.
- **Freshness for the dashboard:** one report/week makes `last_used_at`
  up to 7 days stale even for a healthy node. Its **daily refresh**
  pulls therefore count as check-ins too — but after §3A those pulls
  are authenticated, not public: the node presents its access token
  and request signature (never the raw key), SIN verifies them, and
  the same conditional touch runs. A healthy node then shows a
  check-in within ~24h.
- **The structural guarantee:** the auth gate's only outputs are
  *pass/fail* and this one timestamp side-effect. The handler that
  inserts into `raw_reports` receives **only the validated body**
  (`{week, token, signals}`) — the key, its row id, and its user id
  never enter the insert path. This is a code-structure rule for the
  `src/sin` change, reviewable in the diff (§4, rule P2).

### 3.4 `GET /v1/whoami` (new, small)

Under §3A this is authenticated with the **access token + bound-node
signature**, not with a raw portal key. It returns
`{ ok: true, keyPrefix }` for a portal-key token and `{ ok: true }`
for an operator/bootstrap token. `401` otherwise, same no-oracle body.
Purpose: the node's post-exchange connection test (§1.6) and the
touch in §3.3. It reveals nothing the authenticated node doesn't
already know.

---

## 3A. MetaSync-only access — the key exchange

Boulder's rule (Oct 22, 2026): **“SIN should only be accessible by
SoulSync via MetaSync.”** The intended caller is not a person with a
key, a script, or a third-party client. It is a genuine MetaSync node,
connected to a SoulSync, acting for the account that owns the key.

The primary mechanism is a **key exchange**, a standard and
well-trodden pattern: a long-lived API key is presented once over a
protected exchange and traded for a short-lived access token. The new
build surface is deliberately narrow: one exchange endpoint in SIN,
token + signature verification on the other pool endpoints, a
SoulSync settings section and local handoff, and the two local API
keys the two programs authenticate with (§3A.5). Everything else
below is a rule for how those pieces fit together.

### 3A.1 Threat model — what this stops, and what it cannot

**Attackers and misuse this design is meant to stop:**

- **Casual scripting with a copied key.** A raw portal key by itself —
  in a shell history, a screenshot, a pasted config, or a `curl`
  command — cannot call the pool. It cannot even complete an exchange
  without the private signing key of the node already bound to that
  key.
- **Key sharing and resale.** A portal key is bound to one approved
  node public key. Giving someone the API key does not give them a
  working client; moving it to another node requires the signed-in
  account holder to unbind the old node and approve a new pending
  binding in the portal.
- **Third-party clients.** A clean-room client cannot simply implement
  the documented routes and start calling. It needs a portal key, a
  node signing identity, an approved binding, a fresh token, and a
  valid signature on every request.
- **Bulk fake nodes and playlist poisoning.** This is reduced, not
  solved: account/key caps, the one-node binding, portal approval,
  per-key + per-node limits, and the k = 15 publication threshold make
  each usable identity slower and more visible to create. An attacker
  can still create accounts and run many copies of the real open-source
  node; signatures prove possession of registered keys, not that each
  copy is a different person or an unmodified program.
- **Replay and token theft.** A captured request cannot be replayed
  outside a short clock window because its timestamp and nonce are
  signed and nonces are single-use. A captured token alone is also not
  enough: every call still needs a fresh signature from the bound node.

**The limit, stated plainly:** no open-internet API can perfectly
prove which program is calling when the caller controls the computer
the program runs on. The owner of a machine can inspect a program's
files and memory, attach a debugger, extract its secrets, or patch the
program to sign requests it was never meant to send. Hardware-backed
keys and platform attestation can raise that cost on some systems, but
they are not in this design and would still be evidence about a device
and software state, not absolute proof for every future request. This
is the same client-attestation limit every desktop app lives with.

So the design goal is precise: **make a key alone useless, make
unapproved clients fail, make stolen tokens expire quickly, and make
misuse revocable.** It does not claim to defeat the owner of the node.

### 3A.2 Layer 1 — every pool request is signed by the node

The node already has a persistent identity foundation. In the current
implementation (`node/metasync_node/identity.py`),
`load_or_create_identity` creates a random 32-byte `node_key` in the
node's own state directory (mode 0600), derives the weekly token from
it, and exposes only a 12-character hash fingerprint for status views.
It does **not** yet expose a public key that can verify signatures;
the displayed fingerprint is a hash of the secret and must not be
treated as one.

The signing build keeps that persistent secret as the node's root
identity and adds a real signing pair without ever sending the root
secret:

- Derive an Ed25519 signing seed with HKDF-SHA256 over `node_key`,
  using a domain-separated label such as
  `metasync-sin-request-signing-v1`.
- Register only the resulting **public key**. Define
  `node_id = SHA-256(public_key_bytes)` (hex) and show a grouped
  prefix of that value in both MetaSync and the portal for human
  comparison.
- Sign every exchange and pool-data request. Canonical signed bytes
  are versioned and cover: HTTP method; path plus canonical query;
  timestamp; nonce; SHA-256 of the request body; and, for data calls,
  SHA-256 of the presented access token. GET requests sign the hash
  of an empty body.
- Headers: `Authorization: Bearer <access_token>` for data calls,
  `X-SIN-Node: <node_id>`, `X-SIN-Timestamp: <unix seconds>`,
  `X-SIN-Nonce: <128-bit random, base64url>`, and
  `X-SIN-Signature: <base64url Ed25519 signature>`. The exchange uses
  the same timestamp/nonce/signature headers and identifies the raw
  key through the existing `X-SIN-Key` header over TLS.
- The pool accepts a timestamp only inside a **±5-minute window** and
  records each `(node_id, nonce_hash)` until the window has passed.
  A duplicate is a `401`. The nonce store is a small security table —
  `sin_request_nonces (node_id, nonce_hash, expires_at)` — with
  periodic deletion; it stores no request body, report, MBID, or
  report identifier.
- Verification uses the **bound** public key from §3A.3, never a
  public key supplied in the same data request. That prevents an
  attacker from swapping in their own key after binding.

A portal key alone is therefore only one factor. The working caller
must also possess the bound node's private signing material at the
moment of the exchange and at the moment of each later call.

### 3A.3 Layer 2 — each portal key is bound to one node

**Data-model decision: binding columns on `portal_keys`, not a
bindings table.** One key is for one install; one approved node per
key is the launch rule. A forward migration,
`db/migrations/0004_metasync_node_bindings.sql`, adds:

```text
bound_node_id             text        -- SHA-256(public key), hex
bound_node_public_key     text        -- Ed25519 public key, base64url
bound_at                  timestamptz
pending_node_id           text
pending_node_public_key   text
pending_requested_at      timestamptz
```

Plus a partial unique index on `bound_node_public_key` for non-null
bindings on unrevoked keys, and an index on `bound_node_id`. The
portal Prisma model mirrors all binding fields; SIN's Drizzle model
adds only the fields needed to verify status, binding and signature.
No identity columns are added to any SIN data table.

**Registration and approval:**

1. A key starts unbound. SoulSync hands it to the local MetaSync as in
   §3A.5, and MetaSync attempts the exchange (§3A.4).
2. The pool verifies the raw key's fingerprint and the candidate
   node's signature over the exchange request. If the key has no
   binding and no different pending node, it stores the presented
   public key in the *pending* fields and returns a distinct
   binding-pending failure — **no access token is issued**.
3. The portal key page shows the pending node's fingerprint, first
   request time, and an **Approve this MetaSync** action. The user
   compares the fingerprint with the one shown by that install's
   MetaSync/SoulSync screen and approves only if they match.
4. Approval moves pending → bound and clears the pending fields. The
   next exchange can issue a token.
5. A request from a different public key for an already-bound key is
   the same generic `401` as any other failed authentication; it does
   not reveal that a binding exists and does not replace the pending
   or bound value.
6. Replacement is a signed-in portal action: **Unbind node** clears
   the approved binding. The next exchange from the replacement node
   becomes pending and must be approved. There is no node-initiated
   rebinding.
7. Regenerating a key copies the approved binding to the replacement
   key in the same portal transaction (§1.8), because the account
   holder explicitly rolled the key for the same install. Unbinding
   remains the separate action when the install itself is suspect.

This gives Boulder and the account holder two distinct levers:
**revoke the key** (the credential dies) and **unbind/revoke the
node** (that MetaSync can no longer act even if a replacement key is
later created, until a person signs in and approves a node again).

### 3A.4 The exchange — raw key in, short-lived token out

**Endpoint:** `POST /v1/auth/exchange`.

The exchange request travels over HTTPS and contains:

- the raw portal key in `X-SIN-Key` (never in a URL; never logged);
- the candidate/bound node public key on a first, unbound attempt;
  on later attempts the pool uses the stored bound key;
- the Layer 1 timestamp, nonce and signature — for the exchange, the
  canonical string also covers SHA-256 of the raw key; and
- no requested privilege escalation: the pool assigns scopes from the
  credential type. A user portal key can never request admin scopes.

The pool then checks, in order: transport and request shape; raw-key
fingerprint and §2.2 validity; candidate signature; binding state
(pending → no token; bound public key and `node_id` must match); and
exchange rate limits. On success it returns:

```json
{
  "access_token": "<signed token>",
  "token_type": "Bearer",
  "expires_at": "<timestamp>",
  "node_id": "<bound node id>",
  "key_prefix": "sin_live_Ab…",
  "scopes": ["report:write", "index:read", "playlists:read", "whoami:read"]
}
```

**Token format and contents.** Use a compact signed token (JWS/JWT
shape) signed with the pool's own Ed25519 private key
(`SIN_TOKEN_SIGNING_PRIVATE_KEY` in the SIN function env). Its payload
contains only `key_id` (the `portal_keys.id`), `node_id`, `scopes`,
and `exp`; the header carries the algorithm and pool signing-key id.
It contains no user id, email, raw key, key hash, or report data.
MetaSync verifies the token signature using the pool's published
public key (a public token-key discovery document, fetched over TLS
and pinnable in node configuration), in addition to relying on TLS to
authenticate the exchange response.

**Lifetime: 24 hours at launch.** A healthy node exchanges about once
a day alongside its daily refresh; an intermittently started node can
exchange when it starts. The protocol may permit a deployment to
choose up to 7 days for genuinely offline installs, but 24 hours is
the launch setting because exchanges are cheap and a shorter token
shrinks every leak window. Expiry is not an error state: MetaSync
silently performs a fresh exchange before the old token expires or
after a `401` that indicates expiry.

**Routine calls never carry the raw API key.** Reports, index pulls,
playlist pulls and whoami carry only `Authorization: Bearer
<access_token>` plus the fresh Layer 1 node signature. The raw key
appears only in a fresh exchange, over HTTPS.

**Revocation recommendation: outstanding tokens die immediately.**
Do not rely on token expiry alone. Every data call already needs the
database for nonce storage and, for reports, storage itself; the gate
therefore also performs one indexed `portal_keys` lookup by
`key_id` and checks that the key is still valid, the token's
`node_id` still equals the current binding, and the scopes are still
allowed for that credential. Revoke, expiry, roll to a replacement
key, or unbind therefore takes effect on the next request — no
separate token denylist is needed at SIN's scale. The trade-off is
one indexed lookup per call instead of purely stateless token
verification; that is the right trade for immediate revocation. If
call volume ever makes stateless verification necessary, shortening
the token further is preferable to silently accepting a 24-hour
revocation delay.

**Operator credentials.** The official MetaSync uses the same shape:
its admin credential is exchanged from its registered official node,
and the returned token carries the additional `aggregate:write` and
`playlists:publish` scopes. The shared node key remains a development
and Boulder-only bootstrap credential; at MetaSync-only enforcement
cutover it must no longer be a direct public data-route credential.
`/health` remains public because it exposes no pool data.

**What the exchange proves.** On success, the pool has verified that
an approved account's active key was presented together with the
private signing key of its bound MetaSync. MetaSync has verified,
over TLS and by the token's pool signature, that the pool accepted
that presentation and issued working credentials for the bound node.
Neither statement, by itself, proves to the pool that SoulSync's code
was running at that instant; the local handoff is §3A.5 and the
stronger pool-visible SoulSync evidence is the Phase 2 option in
§3A.6.

### 3A.5 SoulSync ↔ MetaSync local connection and key handoff

> **Amendment, Oct 22, 2026 (Boulder's simplification).** This
> section originally specified a local pairing ceremony: a one-time
> pairing code, a fingerprint comparison, a 256-bit shared pairing
> secret generated at pairing time, HMAC stamps on every local call,
> and SoulSync pinning MetaSync's node identity. **All of that is
> dropped.** Boulder's call: *"why not give the SoulSync API key to
> MetaSync? that's what it's for."* He is right — both programs
> already have a key system, and the local connection is just those
> two key systems facing each other. The ceremony bought invisible
> setup at the cost of a system nobody could picture. What remains
> is below.

Local authentication is the mirror-image key pattern — each program
holds a key the other one issued:

- **MetaSync → SoulSync:** a normal SoulSync API key (`sk_...`),
  created by the user in SoulSync Settings → API keys — exactly how
  the Companion extension and Museck already authenticate, and
  already how the built node works (`DESIGN-NODE.md` §1 step A;
  `node/metasync_node/soulsync.py`). MetaSync presents it as a
  bearer token when reading SoulSync over API v1. **No change.**
- **SoulSync → MetaSync:** **MetaSync issues its own local API
  key.** It is shown once in MetaSync's UI (a "SoulSync connection"
  / local API key area), and the user pastes it into SoulSync's
  MetaSync settings section (below). SoulSync presents that key as
  a bearer token when it calls MetaSync's local endpoints — the
  pool-key handoff and the status reads. MetaSync stores only a
  hash of its local key, the same fingerprint treatment as
  everywhere else in this design. **Regenerate/revoke lives in
  MetaSync's UI:** rolling the local key cuts SoulSync off until
  the new key is pasted into SoulSync's settings. That is the
  entire local trust story: whoever holds MetaSync's local key may
  hand it a pool key and read its status; nobody else can.

**SoulSync core addition (local handoff):**

- A **MetaSync section in SoulSync settings**: MetaSync URL,
  MetaSync's local API key (password-style field, masked after
  save), pool URL, a password-style pool API key field,
  connection/binding/token status, and Send key / Disconnect
  actions.
- SoulSync stores the entered pool key as one of its own credential
  settings and displays only a masked prefix after save; MetaSync
  stores its copy in its own 0600 settings so it can renew the
  exchange without SoulSync being awake for every call.
- A local client that hands the pool key to MetaSync and reads back
  non-secret status (key prefix, binding state, token expiry, last
  check-in) — every call authenticated with MetaSync's local key.
  Status responses carry no secret and need no signature.

**What was dropped, plainly:**

- the one-time pairing code and the pending-pairing session;
- the fingerprint comparison step on the SoulSync side (the
  portal's binding approval in §3A.3 keeps its own fingerprint
  comparison — that one is pool-side and stays);
- the 256-bit pairing secret and its storage on both sides;
- HMAC stamps on local calls (a bearer key over the local channel
  replaces them);
- SoulSync pinning MetaSync's `node_id`/public key for local calls.

**Disconnect semantics:**

- **MetaSync-side disconnect** deletes its stored pool key and
  access token. Regenerating its local key in MetaSync's UI
  additionally cuts SoulSync off until the new key is pasted.
- **SoulSync-side disconnect** just removes its stored MetaSync
  local key and pool key. Nothing on either side keeps working
  credentials it shouldn't: MetaSync's stored pool key is deleted
  on MetaSync-side disconnect, or whenever the user clears it in
  MetaSync's UI.

**Network cases, proportionately (rewritten for the key pattern):**

- **Same host / loopback:** the normal case. Both keys stay inside
  the machine; nothing more is needed.
- **Docker network:** SoulSync should address MetaSync by its
  private Compose service name (for example `metasync:8790`), not a
  published host port. Keys crossing the private Compose network
  are fine — “Docker network” is still a location, not an identity;
  the keys are the identity.
- **LAN:** be honest about the wire. The handoff carries keys: the
  pool key (the valuable one — it is what SoulSync sends MetaSync),
  and SoulSync also presents MetaSync's local key on those calls
  (lower stakes: it only unlocks handing MetaSync a pool key and
  reading its status, and it can be rolled in MetaSync's UI). Over
  plain HTTP on a LAN, anyone watching that network's traffic can
  read both. So: loopback or the Docker private network is the
  recommended setup; if the handoff must cross a LAN, put MetaSync
  behind HTTPS (reverse proxy or a future built-in TLS option).

MetaSync's Pools page then becomes **status/display plus fallback**:
pool URL, key prefix, bound node, token expiry, last exchange, and
disconnect/re-exchange actions. A manual key field remains for
headless development and recovery, clearly labelled as fallback; the
SoulSync handoff is the primary user flow.

### 3A.6 Layer 3 — SoulSync-issued pairing assertion (Phase 2)

> **⬜ BOULDER DECISION — Phase 2.** Local SoulSync handoff (§3A.5)
> is part of Boulder's specified primary flow. Making SoulSync's
> participation visible and verifiable *to the pool* is the stronger,
> separate step below.

In Phase 2, SoulSync receives its own persistent instance signing
keypair. During binding registration, the portal also records the
SoulSync instance public key and instance id alongside the node
binding (a later binding migration extends the §3A.3 amendment).
When SoulSync hands a pool key to MetaSync, it also issues a signed
assertion naming the instance id, the bound `node_id`, the portal
key's prefix or fingerprint, the purpose (`sin-exchange`), and a
short issue/expiry window. MetaSync presents that assertion at the
exchange; the pool verifies it with the registered SoulSync public
key before issuing the token. Renewal requires a fresh-enough
SoulSync assertion, so SoulSync must periodically re-attest while the
install remains connected.

This proves something Layers 1–2 and the local handoff do not: a
holder of that SoulSync install's private instance key participated
in authorizing this MetaSync and this exchange. It is the closest
available evidence that the node really is attached to a live
SoulSync, rather than merely holding a portal key and a node key.

Its limits remain explicit. It needs the SoulSync settings work of
§3A.5 plus an instance-key and assertion endpoint in SoulSync core;
the assertion proves participation at issuance/renewal, not that
SoulSync is unmodified or still running for every later signed call;
and the machine owner can still extract or misuse the instance key.
Phase 2 is therefore an assurance upgrade, not a repeal of §3A.1's
limit.

### 3A.7 Layer 4 — reads are gated too

The access rule applies to data coming out as well as data going in.
After enforcement cutover, all of these require a valid access token
**and** a fresh bound-node signature:

- `GET /v1/index/trending`
- `GET /v1/index/most-wanted`
- `GET /v1/index/breakouts`
- `GET /v1/playlists`
- `GET /v1/whoami`

There is no anonymous SIN data API. `/health` and the pool token
public-key document remain public because neither returns pool data.

**Trade-off, explicit:** the original sidecar sketch wanted a public
downloadable index so anyone could independently inspect and verify
the aggregate outputs. Gating reads ends that form of public
verifiability. It moves to **published outputs**: the portal and
ssync.net/metasync page display the current aggregate lists and
canonical playlists as operator-published snapshots that anyone can
read without an account. Readers can verify what was published; they
cannot query the live API or independently enumerate other weeks
unless Boulder later chooses a separate public snapshot/export.

### 3A.8 Backstops already in the design/build

The exchange does not replace the existing defences; it sits in front
of them:

- per-key report and whoami rate limits (§3.2), now also bucketed by
  the bound node and by failed-exchange source;
- the k = 15 threshold before any item can publish;
- identical `401` responses for unknown, revoked, expired, wrongly
  bound and badly signed credentials (no oracle);
- portal revoke for a key, portal unbind for a node, and admin revoke
  for abuse (§6); and
- raw reports still deleted after 24 hours, with aggregates — not
  reports — as the durable output.

### 3A.9 What changes where — build-list delta

| Area | Delta |
|---|---|
| **Node** | Request signing moves from `DESIGN-NODE.md` §11's “not in the MVP” into SIN scope. Add signing-key derivation/public-key exposure to identity; sign exchange, report, pull and whoami calls; implement exchange/token storage and renewal; add binding-pending state and fingerprint display; issue/regenerate/revoke the node's own local API key and require it on the §3A.5 handoff/status endpoints; receive the pool key through that handoff; Pools page becomes status/display + fallback. |
| **SIN backend** | Add `POST /v1/auth/exchange`; pool token signing key and public-key discovery; token verification on report/index/playlists/whoami and on the official node's aggregate/playlist-publish routes; portal-key fingerprint + status + binding verification; Ed25519 request-signature verification; nonce table with expiry/cleanup; immediate token invalidation through current key/binding status; exchange-specific rate limits; redact `Authorization`, raw-key and signature headers from logs. |
| **Portal** | Add binding fields (§3A.3); pending-node display with fingerprint comparison; Approve, Unbind node and Replace node flows separate from key revoke/roll; show binding and token/check-in status; admin can view binding state and unbind/revoke a node for abuse. |
| **SoulSync core** | Add the MetaSync settings section (MetaSync URL, MetaSync local-key field, pool URL, pool-key field), store MetaSync's local API key, call MetaSync's handoff/status endpoints with it as a bearer token, and read back non-secret status. No instance id, no pairing protocol. Phase 2 (§3A.6) additionally adds an instance signing keypair and signed exchange assertions. |
| **Docs / website** | Update connection instructions everywhere: key is entered in SoulSync; MetaSync trades it for a temporary pass; pulls are authenticated; public aggregate verifiability is via portal/website snapshots, not anonymous API reads. The ssync.net/metasync copy must not promise a public index API. |

### 3A.10 What does **not** change — privacy and show-once

- **Two boxes stay two boxes.** Bindings, public keys, key status and
  token issuance belong to Box 1 (accounts + keys). Reports remain in
  Box 2 under the rotating weekly token.
- **The door check is transient.** Key id, node id, token claims,
  binding result and signature result are used to pass/fail the
  request and, for a valid portal key, to touch `last_used_at`. They
  are **not** passed into the report handler and never written to
  `raw_reports`, `aggregates`, or `index_publications`.
- **The nonce store is not a report log.** It holds only node id,
  nonce hash and expiry for replay defence, has no report/body/MBID
  field, and expires in minutes. Server logs must redact the raw key,
  access token and signature headers so operational logging cannot
  recreate a key/node-to-report link.
- **Signatures do not become report identity.** A report row remains
  exactly `{week, token_hash, signal, mbid, received_at}` in shape;
  its token still derives from the node's root identity and rotates
  weekly, independently of the stable node binding used at the door.
- **Show-once stays show-once.** The exchange does not require storing
  or re-displaying a raw key in the portal. SoulSync and MetaSync hold
  the user's copy locally; the portal and pool store only its
  fingerprint.
- **Playlist authority stays official-only.** User-node token scopes
  exclude aggregation and publishing; §2's permission model and
  `DESIGN-NODE.md` §9's admin-key split remain in force behind the
  token gate.

---

## 4. The privacy guard — enforceable rules

SIN's promise (existing design, unchanged): *reports are anonymous
aggregate counts; nobody — including the operator — can see what a
person's SoulSync reported.* Portal keys authenticate the door. These
rules keep the door from becoming a window. Each rule names its
enforcement, so it's checkable in code review, not just intended.

- **P1 — Reports are stored only under the rotating token.**
  `raw_reports` keeps exactly today's columns (`week`, `token_hash`,
  `signal`, `mbid`, `received_at`). The `0003` migration touches no
  SIN table, and **no column may ever be added to `raw_reports` (or
  `aggregates`) referencing a key, account, IP, or user agent.** The
  token stays `sha256(node_key + week)` — unlinkable across weeks,
  derived from material the portal never sees.
- **P2 — Identity stops at the gate.** In `src/sin`, the portal-key
  lookup result (key id, user id) is consumed only by (a) pass/fail
  and (b) the §3.3 timestamp touch. It is never passed to, logged by,
  or returned from the report handler. Review rule for the §3 change:
  grep the insert path — `portal` anything must not appear below the
  auth gate.
- **P3 — SIN never joins keys to people.** The SIN function's Drizzle
  schema for `portal_keys` (§3.1) omits every identity column; it has
  no `portal_users` table definition at all. Joining a key to an email
  is possible only from the **portal app's** database access, which
  SIN code cannot make.
- **P4 — `last_used_at` is the only usage trace.** No per-key request
  log, no per-key signal counts, no `ApiUsage`-style table (roll-call
  has one for billing; the portal deliberately does not). Rate-limit
  counters (§3.2) are ephemeral window counts keyed by key id and are
  never joined to report content.
- **P5 — What the operator CAN see** (and the dashboard/admin shows
  only this): an account exists (email, signup date), its keys exist
  (label, prefix, created, **last check-in time**, and under §3A the
  bound node's public fingerprint/state), pool-wide health counts
  (active keys, distinct tokens this week, aggregate rows,
  publication log). **What he cannot see:** any signal, MBID, or
  playlist input attributable to an account — because per P1–P4 that
  data does not exist anywhere in attributable form. Raw reports are
  additionally deleted 24h after receipt (existing backend behaviour),
  and anything below k = 15 distinct tokens never publishes at all.
- **P6 — The key is not an identity for the node.** A user's node key
  (`node_key` file) and portal key are independent secrets: the portal
  key can be revoked/rolled from the dashboard without the node
  changing identity, and rolling it does not change the user's weekly
  token history (tokens derive from `node_key`, which never leaves
  the user's machine).
- **P7 — Signatures and bindings stop at the same gate.** Under §3A,
  the access token's key/node claims, the binding lookup and the
  request signature are authentication context only. They follow P2's
  rule exactly: pass/fail, rate-limit bucket and the §3.3 timestamp
  touch may use them; the report handler and every SIN data table may
  not. The short-lived nonce store is a separate security mechanism
  under §3A.2, contains no report content or report identifier, and
  expires in minutes. Authentication logs must redact the raw key,
  token and signature values.

---

## 5. Sign-in method — decided: Google, like roll-call

Roll-call's shipped pattern is the reference: **NextAuth v5
(`next-auth@5.0.0-beta.32`) + `@auth/prisma-adapter`**
(`src/auth.ts`), edge-safe provider config split into
`src/auth.config.ts`, JWT sessions with role in the token, custom
`/login` page. The portal copies that skeleton wholesale. What differs
per option:

### Option A — OAuth (Google chosen; GitHub also in roll-call's setup)

- **What it is:** roll-call's OAuth pattern. **Boulder's launch
  decision is Google** — Gmail is how he signs in to roll-call — using
  the Google provider from roll-call's `auth.config.ts`. GitHub
  (`GitHub({ scope: 'read:user user:email' })`) is the second provider
  in that same setup and can remain available as a secondary button.
  The adapter creates `PortalUser` + `PortalAccount` rows; no other
  tables and no email infrastructure are needed.
- **Needs:** a Google OAuth client for the portal (callback
  `https://metasync.ssync.net/api/auth/callback/google`), with client
  id/secret in the portal env. If GitHub is enabled too, its OAuth app
  uses callback
  `https://metasync.ssync.net/api/auth/callback/github`.
- **Fit:** the sign-in users already know from roll-call, with a
  provider-verified email and zero password surface or deliverability
  work. Downside: the user needs an account with an enabled provider.
- **Roll-call caveat, copied:** **no
  `allowDangerousEmailAccountLinking`** — providers don't all
  guarantee verified emails in the same way, so accounts are never
  auto-linked on email match; a collision produces the branded error
  page, not a silent merge.

### Option B — Email magic link

- **What it is:** Auth.js Email provider + the Prisma adapter,
  writing to `PortalVerificationToken` (already in the schema, §2.1).
  User enters email → receives a sign-in link → session. There are no
  passwords anywhere in this option.
- **Needs — the new dependency:** **an email-sending provider.**
  Roll-call has *no* auth email infrastructure to copy (its only
  providers are OAuth; Stripe sends its own mail). The portal would
  add e.g. **Resend**: an account, an API key in env, and **`ssync.net`
  sending verification** (SPF/DKIM DNS records — Boulder, in
  Cloudflare, §8). Deliverability and a spam-folder story become
  ongoing concerns, small but real.
- **Fit:** lowest-friction for non-GitHub users — SoulSync's wider
  audience is music self-hosters, not necessarily developers. No
  password reset flow exists because there is no password.
- **Build-time spike (flagged, not asserted):** verify the
  email-provider + JWT-session combination under NextAuth v5 before
  committing — it's the one pairing in this section without a
  roll-call precedent running in production.

### Option C — Email + password

- **What it is:** Auth.js Credentials provider. **No roll-call
  precedent at all:** roll-call's `User` model has no password column
  and no credentials provider exists in the codebase.
- **Needs:** a new `passwordHash` column on `PortalUser` (bcrypt or
  argon2), signup validation, login rate-limiting/brute-force
  protection, **and a password-reset flow — which needs email sending
  anyway**, i.e. Option B's entire Resend dependency *plus* the
  password machinery, plus a credential-stuffing/breach-corpus risk
  posture that a one-person project has to maintain forever.
- **Fit:** the most familiar to users and the most expensive in every
  other dimension. Nothing about the portal needs passwords: the
  account guards exactly one asset (pool keys) that is itself
  revocable and rotatable.

### Decision

**Launch with Option A, Google first.** Boulder settled this on
Oct 22, 2026: roll-call let him use Gmail, and that is the sign-in he
expects here. GitHub may ride along as roll-call's second OAuth
provider. The design keeps Option B a **pure addition** (provider
config + Resend env + the verification-token table that already ships
in `0003`) if non-OAuth users later need another route. Option C
remains rejected: it buys familiarity with the project's only
credential database. The data model (§2), key system (§2.2), SIN
verification (§3/§3A), and privacy rules (§4) do not change.

---

## 6. Admin side & abuse notes

**Admin identity.** Boulder's own portal account, flagged by
`role = "admin"` — set the roll-call way: `src/auth.ts`'s `createUser`
event checks the new user's email against an `ADMIN_EMAILS` env list
and promotes on first signup. No separate admin login, no hardcoded
account. Admin pages sit behind the equivalent of roll-call's
`requireAdmin()` (`src/lib/admin.ts`: session role checked at the top
of **every** admin action, not just the layout).

**Admin can:**
- List accounts: email, signup date, key count, per-key label/prefix/
  last-used and node-binding state; **revoke any key** (same
  `revoked_at` mechanism as the owner's own revoke — the kill switch
  for an abusive key, §3.2), and **unbind any node** under §3A when the
  key is valid but the registered MetaSync should no longer act.
- See pool health: active key count, distinct reporting tokens this
  week, aggregate/index publication state — everything in §4 P5's
  "can see" list.
- Publish-side operations stay where they already live: aggregation
  and playlist publishing are the **admin-key** functions of the
  official MetaSync (`SIN_ADMIN_KEY`), *not* portal features. The
  portal admin role confers no SIN write powers.

**Admin cannot** (structurally, per §4): see any user's signals, map a
report or token to an account, recover or view a full key (hash-only
storage — admin sees prefixes like everyone else), or impersonate a
user's node.

**Abuse / Sybil notes.**
- **One account per verified email.** OAuth sign-in keys the account
  to the provider-verified email; magic link verifies the email by
  construction. Disposable-email signups are possible but each new
  identity costs a new verified mailbox — that, plus the caps below,
  is the launch posture; heavier proofing is out of proportion for a
  free key that gates anonymous counts.
- **Caps:** 5 active keys/account (§1), one approved node per key
  (§3A.3), per-key + per-node report rate limits (§3.2), and
  roll-call's other copied guard — portal login attempts ride the
  provider's own protections (OAuth) or the email provider's
  (magic link).
- **k = 15 is the load-bearing wall.** Even a successful Sybil (many
  accounts, many node keys) must field 15 distinct tokens agreeing on
  one MBID in one week before anything publishes, and what publishes
  is a *count on a chart/playlist* — taste data, not access, money, or
  anyone's library. The value at risk is low and the threshold is the
  mitigation; if operator experience ever shows gaming, raising k and
  admin revoke are both one-move responses.
- **Key leakage** (a user pastes their key somewhere public): damage
  is bounded by the per-key rate limits; recovery is roll/revoke.
  The `sin_live_` prefix (§2.2) lets GitHub secret scanning flag the
  common leak path.

---

## 7. Hosting & routing

Facts of the ground (verified Oct 22, 2026): **`ssync.net` is behind
Cloudflare** — responses carry `Server: cloudflare` / `CF-Cache-Status`
headers, DNS is on Cloudflare, and the GitHub Pages static site is the
origin behind it. That changes the routing options, mostly for the
better.

### 7.1 The info page: `ssync.net/metasync`

Unchanged by everything below: a **static page** in the
`soulsync-website` repo (`metasync/index.html` alongside the existing
`index.html`), served by GitHub Pages behind Cloudflare like the rest
of the site. It explains MetaSync + SIN, gives the connection steps
(§1), and links out to the portal. Boulder's original URL instinct is
satisfied for the page that humans read.

### 7.2 The portal's address: subdomain vs path routing

**Option 1 — subdomain: `metasync.ssync.net` (recommended for the app).**
One Cloudflare DNS record: CNAME `metasync` → the portal host
(`cname.vercel-dns.com` for Vercel). The app is a normal Next.js
deployment on its own origin: its own cookies (host-only, exactly
scoped), its own cache headers, zero interaction with the static
site's Cloudflare cache rules. A user's mental model is clean:
`ssync.net` is the website, `metasync.ssync.net` is the app.

**Option 2 — path: `ssync.net/metasync` via a Cloudflare Worker.**
Feasible after all, because Cloudflare sits in front: a Worker on
route `ssync.net/metasync*` reverse-proxies those requests to the
portal origin (rewriting the path prefix), while every other path
continues to the GitHub Pages origin untouched.
- *Pros:* one origin, the exact URL Boulder first pictured; cookies
  could be path-scoped under `/metasync`; the info page and the app
  share an address.
- *Cons:* the portal app must be built with Next.js `basePath:
  '/metasync'` and NextAuth configured for the subpath — a permanent
  build-time commitment that then fights Option 1 if both are ever
  wanted (pick one canonical entry; serving both means two builds or
  accepting one is a redirect). Session cookies now live on the main
  domain (low risk today — the static site sets no cookies — but it's
  the whole site's cookie scope, not an app's). All portal traffic
  transits the Worker (fine at this scale; still a moving part to
  debug at 1 a.m.), and Cloudflare cache rules for the static site
  must explicitly exclude `/metasync*`.
- *Verdict:* real, but it buys a prettier URL with ongoing complexity,
  and it risks touching the static site that just had its SEO fix
  (§7.1's page already gives humans the `/metasync` address for
  everything they read).

**Recommendation: app on the subdomain (Option 1); info page at the
path ( §7.1).** If Boulder later wants login itself under
`ssync.net/metasync`, Option 2 remains available as a Worker +
`basePath` rebuild — nothing in the subdomain launch forecloses it.

### 7.3 Portal hosting: Vercel vs Cloudflare

- **Vercel (recommended).** Roll-call is the precedent end-to-end:
  Next.js + Prisma + Neon Postgres, deployed on Vercel, already
  operated by Boulder. The portal is the same stack minus 90% of the
  app. Custom domain is a Vercel project setting + the §7.2 DNS
  record. No new platform, no adapter risk.
- **Cloudflare (Pages/Workers).** Tempting for tidiness — DNS is
  already there — but Next.js on Workers runs through an adapter
  (OpenNext / next-on-pages), and Prisma on Workers needs a driver
  adapter or Hyperdrive in front of Neon. Each is a novel seam
  roll-call never had to debug. The win (one vendor) is smaller than
  the risk (a second, different production stack for the same person
  to maintain).
- **Verdict:** Cloudflare stays the edge (DNS, proxy, optional
  Workers for §7.4); Vercel hosts the app; Neon hosts the data. Same
  division of labour as roll-call + the website today.

### 7.4 The pool URL users paste: friendly name via Worker

The pool URL is **configuration users keep** — it sits in every node's
Pools page, possibly for years. The raw SIN function URL
(`https://br-tiny-mode-b55fkv5s-sin.compute.c-7.us-east-2.aws.neon.tech/`)
embeds a Neon branch id: if the branch is ever recreated, every
user's stored config breaks at once.

**Option: `pool.ssync.net` via a Cloudflare Worker.** A proxied DNS
record for `pool` + a small Worker that fetches the Neon origin
(pass-through: same method, headers — including `Authorization`,
`X-SIN-Key` on an exchange, and the §3A node-signature headers — and
body; the Neon URL lives in the Worker's config, not in user
configs). Users paste `https://pool.ssync.net` and never see Neon.
The authenticated GET routes are **not shared-edge-cacheable** under
§3A; a later bonus is an edge rate-limit backstop (§3.2), not public
response caching.
(A path variant — `ssync.net/pool` — is the same Worker trick; the
subdomain is cleaner as an API base because the node appends
`/v1/…` paths to it.)

**Recommendation: do this before public launch** — i.e., before keys
are widely distributed — even though the portal can launch first for
Boulder's own testing against the raw URL. It is a ~30-line Worker and
one DNS record now, versus a config-breaking migration for every user
later. What the portal dashboard displays as "the pool URL" (§1.3) is
`https://pool.ssync.net` from the moment the Worker exists.

> **⬜ PENDING — Boulder confirms `pool.ssync.net` + timing** (before
> public launch, per the recommendation).

### 7.5 Cloudflare Access — honest assessment

Cloudflare Access is an off-the-shelf gate: put it in front of
`metasync.ssync.net` and Cloudflare itself demands an email OTP or a
GitHub/Google sign-in before any request reaches the app. It is real
and it works. **It cannot replace building the portal's auth**, for
one specific reason: Access authenticates *a browser at the edge*,
but the portal's core job is **issuing database-backed per-user API
keys**. Access creates no rows in our database, knows nothing about
`portal_keys`, and gives the SIN function nothing to verify.

The middle path exists — read Access's validated identity JWT
(`Cf-Access-Jwt-Assertion`, email claim) in the portal and
just-in-time-provision a `PortalUser` from it — but then we still
build the user/key/provisioning layer (the actual product), while
*also* accepting Access's session/logout/UX semantics, its per-app
policy console as a second admin surface, and vendor coupling in
place of roll-call's proven NextAuth pattern. The savings would be
one login form; the cost is a second identity system to reason about
forever.

**Verdict: not for the public portal.** A reasonable later use:
Access in front of the portal's `/admin` paths as a second factor for
Boulder alone. Optional, post-launch, not in the build order.

---

## 8. Build order

Rough sizes: S ≈ half a day, M ≈ 2–4 days, L ≈ a week+. Steps 1–2 are
the product; 3–5 are launch plumbing. Nothing here is started.

| # | Step | Size | Notes |
|---|---|---|---|
| 1 | **SIN key-verification change** (`src/sin`): `portalKeys` Drizzle table, §3.1 gate order, `GET /v1/whoami`, §3.3 conditional touch, per-key rate limits; tests in the existing suite's style (node-key/admin behaviour must not regress) | M | Includes migration `0003_portal_auth.sql` (§2.3) — applied by Boulder's deploy flow with the unpooled URL, after `0002` |
| 2 | **Portal app**: Next.js + NextAuth (provider per §5 decision) + Prisma portal models; pages: login, dashboard (pool URL + keys + last check-in), create/roll/revoke, admin list/revoke/health; `API_KEY_PEPPER` fail-fast copied from roll-call | M | A skeleton of roll-call's `src/auth.ts`, `src/auth.config.ts`, `src/lib/api-keys.ts`, `src/app/dashboard/keys/*`, `src/lib/admin.ts` — with billing/tiers/usage deleted, not adapted |
| 3 | **Node touch-up (pre-access-rule baseline)**: send the credential on daily refresh GETs (§3.3); pool test calls `/v1/whoami` | S | Superseded in scope by step 3A for the MetaSync-only target; retained here as the smaller baseline change |
| 3A | **MetaSync-only access (§3A)**: SIN exchange + token/signature/binding gates + nonce store; portal binding approval/unbind UI; node signing, exchange and token renewal; SoulSync MetaSync settings and the local key handoff (two keys, no pairing ceremony — §3A.5 as amended) | L | The controlling access build. Phase 2's pool-visible SoulSync assertion (§3A.6) is a separate Boulder decision and is not bundled into 3A |
| 4 | **Info page**: `metasync/index.html` in `Nezreka/soulsync-website` (MetaSync + SIN explained, connect steps, portal link) | S | **Pushing to the website repo is Boulder's go-ahead** — the page is prepared, he approves the publish |
| 5 | **`pool.ssync.net` Worker + DNS** (§7.4), then portal dashboard switches its displayed pool URL | S | Mostly Boulder's dashboard clicks, guided (§8.1) |
| 6 | **Vercel project + domain** for `metasync.ssync.net`, env set, first deploy, smoke test: signup → key → node test → weekly report accepted → dashboard shows check-in | S | The end-to-end proof is this smoke test, run once, live |

### 8.1 What Boulder must do himself

In Cloudflare (dashboard):
1. DNS: add CNAME `metasync` → `cname.vercel-dns.com` (for the Vercel
   custom domain; follow Vercel's proxy guidance shown at attach
   time).
2. DNS + Worker for `pool.ssync.net`: add the proxied record and
   create the Worker with its route (§7.4) — exact click path and
   Worker code supplied at build time.
3. *(Only if magic link wins §5)* DNS records Resend gives him for
   sending from `ssync.net` (SPF/DKIM), in Cloudflare.

In Vercel:
4. Create the portal project, attach `metasync.ssync.net`, set env:
   `DATABASE_URL` (pooled Neon string for the shared DB),
   `AUTH_SECRET`, `ADMIN_EMAILS` (his email), the OAuth or Resend
   credentials from step 7/8, and `API_KEY_PEPPER` (step 6).

Keys & accounts:
5. Generate `API_KEY_PEPPER` once (e.g. `openssl rand -hex 32`)
   and set the **same value** in the portal's Vercel env **and** the
   SIN function's env before the §3 deploy — a mismatch means every
   portal key fails verification while env keys keep working (a
   confusing half-up state; the smoke test in step 6 exists to catch
   exactly this).
5A. For §3A, generate the pool's Ed25519 token-signing keypair through
   the deployment's secret flow: the private key goes only into the
   SIN function's `SIN_TOKEN_SIGNING_PRIVATE_KEY` env; the public key
   is published for nodes to verify issued tokens. Register the
   official MetaSync node's public key through the admin/bootstrap
   path before enabling its admin-scoped exchange.
6. Create the Google OAuth client for the portal (§5) and hand over
   client id/secret via the usual secret path. Create the GitHub
   OAuth app too only if the secondary GitHub button is enabled.
7. *(Only if magic link is added later)* Create the Resend account
   and API key.
8. Approve/push: the `soulsync-website` info page (§8 step 4), the
   MetaSync repo changes (steps 1–3A) — pushes and deploys stay his
   call, as standing.

---

## 9. What this design deliberately does not do

- **No tiers, quotas-as-product, or billing** — roll-call's
  `Subscription`/`ApiTier`/`ApiUsage` machinery stays in roll-call.
  If SIN ever charges, that's a new design.
- **No second pool at launch.** `portal_keys` keys are for *the* SIN
  pool. Third-party pools (anyone can run a SIN-compatible backend,
  per the node design) run their own key story; the portal may learn
  to issue for more pools later — the key table would gain a `pool`
  column then, not now.
- **No anonymous SIN data API** under §3A: index, playlist and whoami
  reads require the token + node signature. Public verifiability is
  provided by portal/website-published aggregate snapshots (§3A.7),
  not by opening the live routes.
- **No changes to the shared-key path's bootstrap semantics** beyond
  §3.1's ordering and §3A.4's cutover rule: `SIN_INGEST_KEY` remains
  a dev/Boulder bootstrap credential, and `SIN_ADMIN_KEY` remains the
  credential the official node exchanges for aggregate/publish
  scopes. Neither is a public direct-client path.
- **No metadata-pool (SoulID) keys.** This portal serves SIN. The
  MetaSync metadata module gets its own trust design when it's built.

## 10. Decisions pending Boulder

1. **Friendly pool URL** — recommendation: `pool.ssync.net` Worker
   before public launch (§7.4).
2. **Phase 2 SoulSync assertion** — decide whether the pool-visible
   SoulSync pairing credential in §3A.6 is required, and if so
   whether it gates the first public launch or follows Layers 1–2,
   gated reads and the local SoulSync handoff in §3A.5.
3. **Sign-in method is settled** — Google, like roll-call (§5).
   Everything else in this doc is decided by the Oct 22, 2026
   direction or copied from roll-call's running code, and is ready to
   build on his word.

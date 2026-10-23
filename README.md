# MetaSync

One repo, two SoulSync network services, both declared as Neon Functions in
[`neon.ts`](neon.ts) against one Neon Postgres database:

| Function slug | Source | Status |
|---|---|---|
| `sin` | `src/sin/index.ts` | **MVP implemented** (central aggregator) |
| `metasync` | `src/metasync/index.ts` | **Scaffold only** — health + 501 placeholder |

The original `hello` function from `neon init` was removed: its job (proving
the Neon Functions wiring) is now covered by the `/health` route on both
functions.

Design source: the Oct 21, 2026 sidecar architecture sketch
(`SIDECAR-ARCHITECTURE-SKETCH.md` §2–§3), with Boulder's Oct 22 decisions:
build SIN first (it's the smaller project), only scaffold the metadata
MetaSync part, and keep both in this one repo.

## The MetaSync node — [`node/`](node/) (Python)

The program that runs alongside a user's SoulSync (bare-metal
`python3 run.py` or Docker; Flask UI on its own port 8790). It connects
to the user's SoulSync (step A — this alone joins nothing), optionally
joins a pool like SIN (step B), computes the four weekly signals from
SoulSync API v1, pushes the report, pulls the pool's canonical playlist
data back, and materializes/updates the SIN global playlists inside the
user's SoulSync. User nodes only push reports and pull data; publishing
the shared playlist definitions is reserved for the official MetaSync
(admin pool role). Full design: [DESIGN-NODE.md](DESIGN-NODE.md). The
backend follow-on that permission model needs (split SIN keys: node vs
admin, plus `GET/POST /v1/playlists` — spec'd in DESIGN §9) is now
**implemented in `src/sin` with tests, but NOT deployed**: the live
backend still runs the single-key build, so until the deploy below
happens a user node's playlist refresh against live SIN still honestly
reports `playlist_data_unavailable`.

Honest status: MVP built Oct 22, 2026 with 20 mocked tests green
(SoulSync, pool, MusicBrainz and the UI all mocked on localhost). **Not
deployed, not committed, never run against a live SoulSync or the live
SIN backend.**

## SIN — SoulSync Intelligence Network

Each SoulSync node computes a small set of curation signals locally, once a
week, and sends anonymous counts to this aggregator. The aggregator builds
the Tastemaker Index and serves it back as public JSON for Discover rails
(Trending in SoulSync, Most Wanted, Breakout Artists). SIN exists to enrich
SoulSync itself — no data is sold.

Signals are keyed **only by MusicBrainz IDs**. SoulID is *not* used by SIN
(see the MetaSync section — SoulID belongs to the metadata service only).

### The four signals (the only ones that exist)

| Signal | Meaning | Key |
|---|---|---|
| `watchlist_add` | User started watching this artist this week | artist MBID |
| `wishlist_add` | User wishlisted this album this week | release-group MBID |
| `import_keep` | Album imported 14+ days ago and still in the library | release-group MBID |
| `hunt_demand` | Album wishlisted 7+ days ago with no successful download yet | release-group MBID |

### API (`sin` function)

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | public | `{ ok, service: "sin", version }` |
| `GET /v1/auth/public-key` | public | The pool's token-signing public key (`{ public_key }`, base64url) so a node can verify its access token; 503 when the signing key is not configured |
| `POST /v1/auth/exchange` | `X-SIN-Key` (raw key, once) + node signature headers | The §3A key exchange: trades a key + node signature for a 24h pool-signed access token; unbound portal keys get `202 {status:"binding_pending"}` and no token |
| `POST /v1/report` | `X-SIN-Key`: admin key, node key, or a per-user **portal key** — or `Authorization: Bearer` access token + node signature | Validates + stores one weekly report into `raw_reports` |
| `GET /v1/whoami` | same two doors as report | Connection test for a node: `{ ok: true }`, plus `keyPrefix` for portal keys |
| `POST /v1/aggregate` | `X-SIN-Key`: **admin key only** — or an admin-scope access token | Internal: run one aggregation pass (optional `{ "week": "2026-W43" }` body) |
| `GET /v1/index/trending` | key or token + node signature (**gated since §3A.7**) | `?week=2026-W43` optional; latest week by default |
| `GET /v1/index/most-wanted` | key or token + node signature | Same week handling |
| `GET /v1/index/breakouts` | key or token + node signature | Compared against the prior week with data |
| `GET /v1/playlists` | key or token + node signature (**gated since §3A.7**) | `{ week, playlists: [{ key, name, tracks }] }` for the latest published week; `week: null, playlists: []` before any publish |
| `POST /v1/playlists` | `X-SIN-Key`: **admin key only** — or an admin-scope access token | Publish one week's canonical playlist definitions (upsert by `(week, key)`); returns what was stored |

### Two keys (the §9 permission split — implemented, not yet deployed)

Both keys travel in the same `X-SIN-Key` header — **the key value
determines the role**:

- **`SIN_INGEST_KEY` — the node key.** Every user node holds this. It can
  only `POST /v1/report` (push its own signals). Since §3A.7 the pulls
  (index, playlists) are gated too — a valid key (or an access token, see
  below) is required; they are no longer public. A user node can never
  run aggregation or publish playlists — those routes answer it 401.
- **`SIN_ADMIN_KEY` — the admin key.** Held **only by the official
  MetaSync** (Boulder's). It alone can `POST /v1/aggregate` and
  `POST /v1/playlists` — i.e. only the official MetaSync publishes the
  shared/global playlist definitions everyone else pulls and
  materializes as local copies in their own SoulSync. The admin key
  also works on `POST /v1/report`.

Comparisons are timing-safe and happen *before* body parsing or any DB
work; if the relevant key is not configured the route fails closed
(503 `ingest_not_configured` / `admin_not_configured`), never open.

### Portal keys (per-user keys — implemented, not yet deployed)

The third door through `POST /v1/report` (PORTAL-DESIGN.md §2.2/§3):
keys a user creates for themselves in the portal app
(`sin_live_<43 chars>`, up to 5 active per account, labeled per
install). The portal stores only a **hash** — SHA-256 hex of
`API_KEY_PEPPER + raw key` — in the `portal_keys` table
(`db/migrations/0003_portal_auth.sql`, which also adds the
`portal_users` / `portal_accounts` / `portal_sessions` /
`portal_verification_tokens` tables). SIN hashes the presented key the
same way and looks the hash up: the check order is admin key, shared
node key, portal key, and unknown / revoked / expired portal keys all
collapse to the identical `401 {error:"unauthorized"}` — no oracle. A
key is valid iff `revoked_at IS NULL AND (expires_at IS NULL OR
expires_at > now())`; there is no `is_active` flag.

- **`API_KEY_PEPPER`** is the pepper env for the `sin` function
  (declared in `neon.ts`). It must be the **same value** the portal app
  hashes with when issuing keys; rotating it invalidates every issued
  key. If it is unset, the portal-key path is disabled (portal keys get
  401) and the shared/admin keys are unaffected.
- **Privacy (§4, P1–P4):** the portal lookup result feeds only
  pass/fail, the rate-limit bucket, and one timestamp side-effect. The
  report rows inserted into `raw_reports` are built only from the
  validated body — no key, account, or user reference is ever stored
  with a report, and SIN's schema for `portal_keys` carries no identity
  columns (there is no `portal_users` table in SIN's Drizzle schema at
  all). On a successful portal-key report or whoami, SIN refreshes the
  key's `last_used_at` with a single conditional UPDATE throttled to
  once per hour in its WHERE clause — that timestamp ("last check-in"
  on the portal dashboard) is the only usage trace kept. A portal key
  sent on the pulls (`/v1/index/*`, `/v1/playlists`) refreshes it the
  same way (since §3A.7 those pulls require a credential — see the
  exchange section below).
- **`GET /v1/whoami`** authenticates any valid key type and returns
  `{ ok: true }` for env keys, `{ ok: true, keyPrefix }` for portal
  keys — the node's connection test.
- **Rate limits (§3.2)** are per-key request-count tripwires (a healthy
  node sends one report per week): 10 reports/hour and 50/day per key,
  60 whoami/hour per key, env keys capped the same under fixed buckets.
  Counters are keyed by key id and count requests only — never signals
  or report content.
- **Status: built locally, NOT deployed.** Migration `0003` is a file
  only and has **NOT been applied** to the live database; the live
  backend does not yet have the `portal_keys` table or the pepper, so
  live reports still authenticate with the two env keys only.

### Key exchange + access tokens (§3A — implemented, not yet deployed)

The primary way a MetaSync node talks to the pool (PORTAL-DESIGN.md
§3A). The raw pool key is presented **once**, at exchange time; all
routine traffic uses a short-lived token instead.

- **Node identity.** Each node holds an Ed25519 keypair. Its node id is
  the hex SHA-256 of the raw 32-byte public key. Every exchange and
  token-door request is signed: headers `X-Node-Id`,
  `X-Node-Signature` (base64url), `X-Node-Timestamp` (unix seconds),
  `X-Node-Nonce`; the signed string is `METHOD \n PATH+QUERY \n
  TIMESTAMP \n NONCE \n SHA256_HEX(raw body)`. Timestamps must be
  within ±5 minutes and each `(node_id, nonce)` is single-use — spent
  nonces are remembered in the `sin_request_nonces` table until they
  age out of the window.
- **`POST /v1/auth/exchange`** takes `X-SIN-Key` (a portal key, or the
  admin/shared key for the official node) plus the signature headers.
  A portal key is **bound** to one node: the first exchange from an
  unbound key (with `X-Node-Public-Key`, the base64url raw public key)
  only records that node as *pending* and answers
  `202 {status:"binding_pending"}` — no token until a person approves
  the node in the portal. Once bound, only that node's signature can
  exchange or spend tokens for the key; a different node presenting
  the key gets the same undifferentiated 401 as any other auth
  failure. Exchanges are rate-limited to 10/hour per key fingerprint
  and per node id (§3A.8).
- **Access tokens** are compact JWS-style, Ed25519-signed by the pool:
  header `{alg:"EdDSA", typ:"SIN"}`, claims `{key_id, node_id,
  scopes, exp, iat}` with a 24-hour life. User scopes are
  `report:write, index:read, playlists:read, whoami:read`; an admin
  exchange adds `aggregate:write, playlists:publish`. The pool's
  signing key is the **`SIN_TOKEN_PRIVATE_KEY`** env (declared in
  `neon.ts`): a base64url raw 32-byte Ed25519 seed — the `d` value of
  `generateKeyPairSync("ed25519")` exported as JWK — or a PEM/PKCS#8
  key. Generate one with
  `node -e "const c=require('crypto');const k=c.generateKeyPairSync('ed25519');console.log(k.privateKey.export({format:'jwk'}).d)"`.
  When it is unset, exchange and the public-key endpoint fail closed
  with `503 {error:"token_not_configured"}`. Nodes fetch the matching
  public key from `GET /v1/auth/public-key`.
- **Token door.** Data routes accept `Authorization: Bearer <token>`
  plus the signature headers *instead of* the raw key (the key door
  stays for the official node and the transition). Every token call
  re-checks the key's **current** row — revoked, expired, unbound, or
  rebound to another node kills the token on that very request, not
  at token expiry. Admin routes accept an admin-scope token in place
  of the raw admin key.
- **Gated reads (§3A.7).** `GET /v1/index/*` and `GET /v1/playlists`
  are no longer public: they need the key door or the token door.
  (Public verifiability moves to published snapshots — a portal-side
  concern, design §3A.7.)
- **Privacy (P7).** Signatures, tokens, and bindings are door checks
  only: verification results feed pass/fail, the rate-limit bucket,
  and the throttled `last_used_at` touch. Nothing about them is
  written to `raw_reports` or any other SIN data table, and tokens are
  never stored server-side.
- **Status: built locally, NOT deployed.** Migration
  `db/migrations/0004_metasync_node_bindings.sql` (binding columns on
  `portal_keys` + the `sin_request_nonces` table) is a file only and
  has **NOT been applied** to the live database; `SIN_TOKEN_PRIVATE_KEY`
  is not set on the live function, so the live backend has no exchange
  yet and its reads are still public.

Playlist publish payload (strictly validated in `src/sin/logic.ts` —
unknown fields rejected at every level):

```json
{
  "week": "2026-W43",
  "playlists": [
    {
      "key": "trending",
      "name": "Trending in SoulSync",
      "tracks": [
        {
          "id": "<spotify-shaped track id>",
          "name": "Track title",
          "artists": [{ "name": "Artist" }],
          "album": { "name": "Album", "images": [{ "url": "https://…" }] },
          "duration_ms": 200000,
          "image_url": "https://…"
        }
      ]
    }
  ]
}
```

`key` is one of `trending | most-wanted | breakouts`; at most 3
playlists per publish, at most 500 tracks per playlist; `id`, `name`
and `artists[].name` are required per track, `album` / `duration_ms` /
`image_url` optional.

Report payload:

```json
{
  "week": "2026-W43",
  "token": "<hash(node_key + week), 16-128 chars>",
  "signals": [{ "type": "wishlist_add", "mbid": "<release-group MBID uuid>" }]
}
```

Validation is strict (`src/sin/logic.ts`): exactly the four signal types,
UUID-shaped MBIDs, ISO week strings, a payload size cap (64 KiB / 1,000
signals), and **unknown fields are rejected** — a client can never smuggle
titles, names, paths, or listening data through this endpoint.

Write routes authenticate with a timing-safe comparison *before* the body
is parsed or Postgres is touched (per the Neon Functions
production-hardening guidance: a function URL is public, so never leave a
write route unauthenticated). If the relevant key (`SIN_INGEST_KEY` /
`SIN_ADMIN_KEY`) is not configured, the write routes fail closed with 503.

### Privacy model (the honest version)

"Anonymous aggregate counts, computed on your machine."

- **Binary contributions.** Per item, per week, a node reports 1 or
  nothing. Enforced twice: payload dedupe in `logic.ts` and a unique index
  on `(token_hash, week, signal, mbid)` in the database. A count means
  "this many distinct nodes," full stop.
- **Rotating dedupe token.** Each report carries `hash(node_key + week)`.
  It is used only to dedupe double-submits within that week; next week's
  hash is unlinkable to this week's. There is no cross-week tracking.
- **k-anonymity publish threshold.** An item appears in the index only if
  at least **k = 15** distinct nodes reported it (`K_PUBLISH_THRESHOLD` in
  `src/sin/logic.ts` — a named constant, change it in one place). Below
  that, the data point does not exist publicly.
- **Raw reports are deleted within 24 hours** of aggregation
  (`RAW_REPORT_RETENTION_MS`). The aggregator keeps only aggregates:
  `(week, signal, mbid, node_count)`.
- **Never collected, never sent:** listening history or play counts, track
  titles or artist/album names as text, file paths, library listings or
  library size, usernames, emails, IP-based identity, SoulSync instance
  URLs.

### Scoring (simple on purpose, every score explainable in one sentence)

- **Trending** = `adds_this_week / (1 + average weekly adds over the prior
  3 weeks)`, where "adds" are `watchlist_add` + `wishlist_add` counts.
  Reason: `"Added by N collectors this week"`.
- **Most Wanted** = `wishlist_add` + `hunt_demand`. Reason:
  `"Wishlisted by N collectors, still hard to find"` (when hunt_demand is
  present).
- **Breakouts** = `watchlist_add` grew **≥ 2x** week-over-week with
  **≥ 20 nodes** this week. Reason: `"Watchlist adds grew 2.5x this week —
  added by N collectors"`.

All scoring/validation/dedupe logic is pure and DB-free in
`src/sin/logic.ts`, and covered by `tests/sin.test.ts`.

### Data

- Drizzle schema: `src/sin/schema.ts` — `raw_reports`, `aggregates`,
  `index_publications`, `canonical_playlists`, plus the portal-auth
  tables (`portal_keys` incl. the §3A binding columns) and
  `sin_request_nonces`.
- SQL migrations: `db/migrations/0001_sin_init.sql` (applied live),
  `db/migrations/0002_canonical_playlists.sql` (the `canonical_playlists`
  table backing `GET/POST /v1/playlists` — written with the §9 code,
  **NOT applied to the live database yet**),
  `db/migrations/0003_portal_auth.sql` (portal users + keys — file
  only, **NOT applied**), and
  `db/migrations/0004_metasync_node_bindings.sql` (§3A node bindings +
  request nonces — file only, **NOT applied**). All match the schema.
  **Migrations use the direct/unpooled URL** (`DATABASE_URL_UNPOOLED`,
  no `-pooler` in the hostname), never the pooled `DATABASE_URL` the
  functions use for request traffic. See `db/migrations/README.md`.
- Runtime DB access: module-scope `pg` Pool (`max: 5`) +
  `attachDatabasePool` from `@neon/functions`, wrapped by Drizzle
  (`src/sin/db.ts`).

## MetaSync metadata service — SCAFFOLD ONLY

The second module, deliberately not implemented yet: a lot of work will go
into it, and it ships after SIN proves the chassis, install story, and
trust story.

What exists today:

- `src/metasync/index.ts` — `GET /health` and
  `GET /v1/records/:soulId` returning **501 not-implemented** with a TODO.
- `src/metasync/schema.ts` — the planned schema, commented: records keyed
  by **SoulID** (the canonical ID SoulSync assigns each track, album, and
  artist) holding MBIDs, cross-provider IDs (JSONB), genres/tags,
  user corrections (flagged as corrections), the publishing node's
  signature, and a corroboration count (proposed acceptance rule: N = 3
  agreeing peers, or a MusicBrainz match).

Rules any future implementation must keep:

- **SoulID is only for this metadata service, not for SIN.**
- **Local data always wins.** MetaSync never overwrites a user's own
  SoulSync metadata; it fills gaps and offers corrections.
- **No audio. Ever.** Not whole, not in pieces. Metadata only. No library
  dumps, no listening data, no file paths.

## Neon state — deployed and verified Oct 22, 2026

Neon project `fragrant-mouse-42130917` (name: metasync, region
aws-us-east-2, org `org-ancient-lab-52574808`), branch `production`
(`br-tiny-mode-b55fkv5s`). Auth is via a Neon API key held in Muse's
Secure Vault (connector `custom.neon`) — the CLI runs through the helper
in `~/workspace/custom-connectors/custom.neon/bin/neon_cli.py`, which
mints a fresh credential surrogate per run. No browser login is used.

What has been run and verified live:

- `neon config plan --env .env.local` planned exactly two additions:
  function `sin` and function `metasync` (services: Postgres, Functions).
- `neon deploy --env .env.local` applied both, and pulled 5 Neon
  variables into `.env.local` (DATABASE_URL, DATABASE_URL_UNPOOLED,
  NEON_BRANCH, NEON_FUNCTION_SIN_BASE_URL, NEON_FUNCTION_METASYNC_BASE_URL).
  Function URLs:
  - sin: https://br-tiny-mode-b55fkv5s-sin.compute.c-7.us-east-2.aws.neon.tech/
  - metasync: https://br-tiny-mode-b55fkv5s-metasync.compute.c-7.us-east-2.aws.neon.tech/
- Migration `db/migrations/0001_sin_init.sql` applied with the DIRECT
  (unpooled) connection string. Tables: raw_reports, aggregates,
  index_publications, plus the binary-contribution unique index.
- End-to-end smoke test against the live functions and live database:
  both `/health` routes 200, the MetaSync placeholder route returns 501
  as designed, POST /v1/report without the key returns 401, with the key
  returns 201 (accepted 2), the identical report re-sent inserts 0 and
  ignores 2 duplicates (binary rule), a forbidden signal type returns
  400, POST /v1/aggregate runs, and the index routes return empty items
  for a single node because nothing below k=15 publishes. The smoke-test
  raw rows were deleted afterwards; the tables are empty again.
- `SIN_INGEST_KEY` was generated (`openssl rand -hex 32`) and lives only
  in the gitignored `.env.local` and in the deployed function env. A SoulSync
  node will need this same key as its X-SIN-Key when reporting ships.

Still not done, deliberately:

- **The §9 backend (key split + canonical playlist endpoints) is built
  but NOT deployed.** The live `sin` function still runs the previous
  single-key build and the live DB has no `canonical_playlists` table.
  Deploying it needs, in Boulder's hands: generate a `SIN_ADMIN_KEY`,
  set it in `.env.local` (note: `neon.ts` now declares it alongside
  `SIN_INGEST_KEY`, so `neon deploy` will refuse to run without it —
  intended fail-closed), run `neon deploy`, and apply migration
  `db/migrations/0002_canonical_playlists.sql` with the unpooled URL.
  None of that has been done.
- **The portal-keys backend (§3) and the §3A key exchange are also
  built but NOT deployed.** On top of the §9 deploy they need:
  generate an `API_KEY_PEPPER` and a `SIN_TOKEN_PRIVATE_KEY` (both
  declared in `neon.ts`, same fail-closed behavior), apply migrations
  `0003_portal_auth.sql` and `0004_metasync_node_bindings.sql` with
  the unpooled URL, then `neon deploy`. The same `API_KEY_PEPPER`
  value must be configured in the portal app, or portal-issued keys
  will not verify here. None of that has been done either.
- No scheduled aggregation trigger yet. Aggregation currently runs only
  when POST /v1/aggregate is called. Wiring a Neon Function Trigger
  (nightly schedule) is a follow-on.
- Local dev loop: `neon dev` (serves every function in neon.ts with hot
  reload and injects DATABASE_URL and friends).
- Nothing is committed or pushed to GitHub yet. This directory is a
  working copy of Nezreka/MetaSync main (5bd46869) plus the build above;
  pushing is Boulder's call.

## Development

```bash
npm run build   # tsc --noEmit (strict) over neon.ts, src/, tests/
npm test        # node --test — pure logic + route tests, no live DB needed
```

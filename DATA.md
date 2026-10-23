# MetaSync — the data map

**What data exists in this system, where it flows, and how it is used.**
Written Oct 23, 2026 against MetaSync `main` @ `11ef51fe`, then revised
the same day for the review branch `fix/review-privacy-correctness`
(count-on-arrival backend, closed-week reports, listening signals — see
§16 for what changed and why). Every claim below is traceable to the
file named beside it. Where the system's behavior has a sharp edge, the
edge is stated, not smoothed over (§15).

## 0. The parties and the one-paragraph version

Four parties hold data:

1. **SoulSync** (the user's own app) — source of all library facts.
2. **The node** (`node/`, runs on the user's machine) — reads SoulSync,
   computes signals, stores its own working state, talks to pools.
3. **The SIN backend** (`src/sin/`, Neon Postgres + Neon Functions) —
   receives reports, aggregates them, composes charts, serves them back.
4. **The portal** (`portal/`, Next.js on Vercel, same Neon database) —
   accounts and pool keys; the only place a key is tied to a person.

The flow: the node reads the user's SoulSync locally → reduces the
library to **binary weekly signals keyed only by MusicBrainz IDs** →
sends one report per pool per week, describing the week that just
closed → the backend counts it on arrival (+1 per item on a running
tally; nothing per node is stored) and publishes only items at or above
**k = 15 nodes** → a composer turns the surviving counts
into charts, resolving titles/track lists from MusicBrainz **once, on
the server** → nodes pull the charts back and create playlists inside
the user's own SoulSync. Nothing in the system ever moves a title, a
file path, or a listening record off the user's machine.

## 1. What the node reads from SoulSync

All reads are `GET`s against SoulSync's API v1 with the user's SoulSync
API key (`sk_…`, header `Authorization: Bearer …`), made by
`node/metasync_node/soulsync.py`. The node reads **four** endpoints, plus
a fifth only when the user has turned listening signals on:

| Endpoint | Fields the node actually uses | Used for |
|---|---|---|
| `GET /api/v1/watchlist` | per artist: `musicbrainz_artist_id`, `date_added` (fallback `created_at`) | `watchlist_add` |
| `GET /api/v1/wishlist` (paged, 200/page) | per track: `date_added`, and a release-group MBID inside `track_data` under `musicbrainz_release_group_id` / `musicbrainz_releasegroup_id` / `release_group_mbid` | `wishlist_add`, `hunt_demand` |
| `GET /api/v1/library/albums` (paged, 200/page) | per album: `musicbrainz_release_id`, `created_at` | `import_keep`, `recent_add`, the library snapshot sets behind churn |
| `GET /api/v1/library/download-activity?since=<week start>&until=<week end>` | per album: `musicbrainz_release_id`, `completed_downloads`, `completed_since`, `first_downloaded_at`, `last_downloaded_at` | `download_grab`, `download_churn` history |
| `GET /api/v1/library/listening-activity?since=<week start>&until=<week end>` — **only with `share_listening` on** | per album: `musicbrainz_release_id`, `plays`, `distinct_tracks`, `distinct_days` (SoulSync's shared listening pile only) | `listen_album` |

Notes that matter for review:

- Wishlist rows carry a release-group MBID in
  `musicbrainz_release_group_id` once SoulSync's MusicBrainz worker has
  matched the row's album in the background (SoulSync branch
  `metasync/release-group-enrichment`). Until then, or on an older
  SoulSync, an item contributes only if its `track_data` has a
  release-group MBID under one of the three keys above. Anything else is
  **skipped and counted in diagnostics** (`node/metasync_node/signals.py`,
  GAP-1).
- Library albums likewise carry `musicbrainz_release_group_id` from the
  same worker. The node uses it as-is and asks MusicBrainz (§4) only
  about releases SoulSync has no group for yet.
- `download-activity` is computed **inside SoulSync** (it joins its
  `track_downloads` ledger to library albums by track file path) and
  returns IDs + counts only — SoulSync PR #1848. A SoulSync build
  without it answers 404; the node then computes **no** download
  signals that run and flags `downloads_available: false` in
  diagnostics. The report itself never fails over this endpoint.
- SoulSync album rows carry a MusicBrainz **release** ID and, once
  enriched, its release-**group** ID. The node bridges release → group
  itself (§4) only where SoulSync has no group yet.
- Page caps: reads stop at `library_max_pages` (default 5000 pages);
  a capped read is flagged (`albums_truncated` / `wishlist_truncated`)
  in diagnostics, and a truncated library read disables churn for
  that run and does not rotate the churn snapshots.

## 2. The eight signals — exact rules

Computed by `node/metasync_node/signals.py::compute_signals`, purely
from the §1 reads plus the node's own state (§6). All signals are
**binary per node per week**: a node contributes at most one of each
(type, MBID) per ISO week, and the pool accepts one report per node
per week (§5, §8).

**The report week** is the last full ISO week before the run,
`[Monday 00:00 UTC, next Monday 00:00 UTC)` (DESIGN-NODE.md §12).
"In the week" below means inside that window; ages are measured at the
window's end.

| Signal | Key | Fires when (all conditions) |
|---|---|---|
| `watchlist_add` | artist MBID | artist's `date_added` is in the report week and the MBID is UUID-shaped |
| `wishlist_add` | release-group MBID | wishlist item's `date_added` is in the report week and a group MBID was found in `track_data` |
| `hunt_demand` | release-group MBID | wishlist item's `date_added` is 7+ days before the week's end (proxy for "wanted, still not got": SoulSync removes fulfilled items; there is no per-item success flag — GAP-4) |
| `import_keep` | release-group MBID | album's 14-day mark (`created_at` + 14 days) falls **in the report week**, it is still in the library, and its release resolves to this group — **once per album**, never re-sent |
| `recent_add` | release-group MBID | album's `created_at` is in the report week and its release resolves to this group |
| `download_grab` | release-group MBID | download-activity row has `completed_since ≥ 1` (SoulSync counted completions at/after the week start the node passed), the release resolves, **and the group produced no `recent_add` this run** — disjoint by construction |
| `download_churn` | release-group MBID | **opt-in only** (§7). Group is in the node's accumulated download history with a first download 14+ days before the week's end, was present in the library snapshot from two **weeks** ago, and is absent from last week's snapshot **and** this one (two consecutive weekly absences), on a **complete** library read |
| `listen_album` | release-group MBID | **opt-in only** (§7). At least **3 distinct tracks** of the album were played in the report week (SoulSync's shared listening pile); at most the **50** most-played such albums per report (DESIGN-LISTEN-SIGNALS.md) |

**Bulk rule.** More than 500 of one event type (`watchlist_add`,
`wishlist_add`, `import_keep`, `recent_add`, `download_grab`,
`download_churn`) in a week is a bulk operation, such as an import,
a migration or a drive remounting. That type is not reported that
week, and the preview says so. Standing `hunt_demand` above 500 is
sampled to 500 by a hash of (week, MBID). So a report is a week of
events, never a library or wishlist listing.

Album `created_at` is SoulSync's library-row creation time — the
import-date proxy (GAP-3); there is no dedicated `imported_at`.

## 3. What the signals become — report payload

`POST /v1/report` body, built by
`node/metasync_node/service.py::payload_for_pool` — the **entire**
payload, nothing else is in it:

```json
{
  "week": "2026-W43",
  "token": "<64 hex chars>",
  "signals": [{ "type": "import_keep", "mbid": "<uuid>" }, …]
}
```

Server-side validation (`src/sin/logic.ts::validateReport`) is strict:
the body may contain **exactly** `week`, `token`, `signals`; each
signal exactly `type` and `mbid`; `type` must be one of the eight;
`mbid` must be UUID-shaped; `week` must be a real ISO week **and an open one**: the week that just
closed, or the current week (`openReportWeeks`), so nobody can backfill
an old week or pre-stuff a future one; `token` must be 16–128 chars of
`[A-Za-z0-9_-]`. Caps: **5,000 signals**, **1,000 per type**, **100
`listen_album`**, and **1 MB** per report. A report over a cap is
refused, not trimmed. Unknown fields, extra keys inside a signal, or
a title/artist field smuggled into a signal are all 400s. (The unit
tests pin the "a signal carrying a title is rejected" case.)

## 4. MusicBrainz and ListenBrainz — the only third parties that see anything

MusicBrainz receives **MBIDs in URL paths and nothing else** — no user
data, no library data; the queries are "what is this public ID".

**From the node** (`node/metasync_node/musicbrainz.py`): 
`GET /ws/2/release/<release MBID>?inc=release-groups&fmt=json`, used
to bridge release → release-group. Lookups are paced (default
≥ 1.1 s apart), capped per run (`release_resolution_cap`, default
**5,000** fresh lookups; download-activity releases are resolved
**first**), and answers are cached permanently on the node
(`release_to_group` in state, plus a raw-response `mb_cache`).
Failures skip that album for the week; they never fail a run.
User-Agent: the node's configured UA (`metasync_node/config.py`).

**From the composer** (`src/sin/mb.ts`):
`GET /ws/2/release-group/<mbid>?inc=artists+genres+releases`, then the
earliest Official release's track list, and
`GET /ws/2/artist/<mbid>?inc=genres+release-groups` for breakout
picks. Paced at ≥ 1,100 ms, UA
`MetaSync/0.2 (composer; contact admin@ssync.net)`, per-pass fetch
budget (default 40, max 200). Results are cached permanently in
`mb_resolutions` (§8). Cover art is **not fetched** — only a
Cover Art Archive URL is constructed
(`https://coverartarchive.org/release-group/<mbid>/front-500`).

**ListenBrainz, from the composer only** (`src/sin/mb.ts`,
`ListenBrainzPopularity`): `POST https://api.listenbrainz.org/1/popularity/recording`
with `{recording_mbids: [...]}` — the recording MBIDs of a **charted**
album's tracklist, taken from MusicBrainz's public catalogue. The answer is
ListenBrainz's catalogue-wide listen and listener counts per recording,
used to pick the album's 5 most popular tracks for the playlists (§9).
Once per album, cached in its `mb_resolutions` bundle (`topTrackIds`). No
node ever talks to ListenBrainz for this, and nothing in the request came
from a SIN user: SIN collects no track-level data at all.

## 5. Identity, tokens, and the wire protocol

**Node identity** (`node/metasync_node/identity.py`): on first run
the node generates a random 32-byte root key, stored hex in
`<state_dir>/node_key`, mode 0600. It never leaves the machine. Three
things derive from it:

- **Weekly dedupe token**: `token(week) = sha256("<root_key_hex>:<week>")`,
  hex. This is the `token` in §3. It rotates every week, so reports
  from different weeks cannot be linked by token; within one week it
  is what makes a node's second send of the same signal a no-op
  (the backend unique index is on `(token_hash, week, signal, mbid)`).
- **Signing key**: an Ed25519 seed derived by HKDF-SHA256 over the
  root key with info label `metasync-sin-request-signing-v1`.
- **node_id**: `sha256(raw Ed25519 public key)`, hex. The first 12
  chars are the human **fingerprint** shown for binding approval.

**Key exchange** (`node/metasync_node/pool.py`, backend
`POST /v1/auth/exchange`): the node presents its pool key once, in
the `X-SIN-Key` header, with an empty body, signed (below). The pool
checks the key's hash, then the binding (§10): an unbound node's
first exchange parks its node_id + public key as *pending* and
returns 202 — **no token is issued** until the account holder
approves the fingerprint in the portal. A bound node (or an approved
pending one) receives a pool-signed **access token, 24 h**, whose
claims are exactly `{ key_id, node_id, scopes, iat, exp }`
(`src/sin/logic.ts::AccessTokenClaims`). User scopes:
`report:write`, `index:read`, `playlists:read`, `whoami:read`; the
official node's admin credential adds `aggregate:write` and
`playlists:publish`. The pool's token-signing public key is public
(`GET /v1/auth/public-key`); the private key exists only as the
backend env var `SIN_TOKEN_PRIVATE_KEY`.

**Routine calls** carry `Authorization: Bearer <access token>` **and**
a fresh node signature over the canonical string
`METHOD \n PATH+QUERY \n TIMESTAMP \n NONCE \n SHA256_HEX(body)`,
in headers `X-Node-Id`, `X-Node-Signature`, `X-Node-Timestamp`,
`X-Node-Nonce`. The backend enforces a ±5-minute timestamp window
and single-use nonces (stored as SHA-256 hashes with expiry in
`sin_request_nonces`, §8), and **re-checks the key's validity and
binding on every token-authenticated request** — revoking a key or
unbinding a node kills live tokens on the next request, not at
expiry. Rate limits are per-key request counters (in-memory, fixed
windows): reports 10/hour and 50/day, exchange 10/hour (per key
**and** per node id), whoami 60/hour.

**Legacy fallback**: a pool **on loopback** that 404s the exchange is
used in a labelled direct-key mode (the report call presents the pool
key directly). This exists for development pools on the same machine.
Any other pool that 404s the exchange is an error, and the raw key is
never sent to it. Pool URLs must be HTTPS unless they are on loopback.
On the backend, the direct-key door accepts **only the operator's env
keys** (`SIN_ADMIN_KEY`, `SIN_INGEST_KEY`). A raw portal key is
accepted nowhere except the exchange, so a copied key is useless on any
machine other than the bound node.

**Endpoints the backend exposes** (`src/sin/index.ts`): `GET /health`,
`GET /v1/auth/public-key`, `POST /v1/auth/exchange`,
`GET /v1/whoami` (returns key prefix + binding status for the
presented credential), `POST /v1/report`, `POST /v1/aggregate`
(admin scope), `POST /v1/compose` (admin scope), `GET /v1/index/
trending|most-wanted|breakouts` (public; aggregates only),
`GET /v1/playlists` (token or key), `POST /v1/playlists` (admin
scope only — **user nodes can never publish**).

## 6. What the node stores on the user's machine

State dir: `METASYNC_STATE_DIR` (Docker default `/data`; bare metal
typically `./data`). Files (`node/metasync_node/store.py`,
`identity.py`):

- **`node_key`** — the root secret (§5), 0600.
- **`ui_secret`** — random Flask session-signing secret, generated once.
- **`settings.json`** — connections and switches:
  - `soulsync`: SoulSync URL + the user's SoulSync `sk_` API key
    (stored in plaintext in this file — it is the node's own config
    on the user's own machine, same trust level as the SoulSync
    install itself);
  - `pools`: list of `{id, name, url, api_key, role, enabled}` — the
    raw pool key lives here (it must, to present at exchange);
  - `dry_run` (default **on**), `share_churn` and `share_listening`
    (both default **off**, §7);
  - `ui_password_hash` (werkzeug hash of the node's local admin
    password), `local_api_key_hash` / `local_api_key_prefix` /
    `local_api_key_created_at` (§11).
- **`state.json`** — runtime state:
  - `release_to_group` — the permanent MusicBrainz bridge cache;
  - `mb_cache` — raw MusicBrainz JSON responses by URL path, for the
    admin flow's lookups only (release → group lookups keep just the
    answer, in `release_to_group`);
  - `downloaded_groups` — group MBID → `{first_downloaded_at,
    last_downloaded_at}`, the node's own accumulated download
    history, built from weekly download-activity reads **while
    albums are in the library** (the endpoint forgets deleted
    albums; this record is the only churn memory);
  - `library_group_snapshots` — `[{week, groups}]`, one **complete**
    library read per reported week, the last three weeks (the churn
    two-absence rule);
  - `previews` — per pool, the **exact last report payload** (week,
    token, signals) so the preview page renders a snapshot, never a
    recomputation;
  - `pool_auth` — per pool: exchange mode, binding status, the live
    **access token** and its expiry, last exchange result;
  - `last_report` (per pool: week, time, signal count),
    `last_refresh_at`, `index_cache`, `playlists_cache`,
    `managed_playlists` (what was synced into SoulSync, with content
    hashes), `built_definitions` (admin mode only), `report_job`
    (live job record), `last_report_attempt` (scheduler back-off), `pairing_session` / `pairing_attempts` /
    `last_pairing` (§11), `last_errors` (newest 20).

The scheduler (`node/run.py`, `service.report_due`) runs the report
once per closed week, after the node's slot (0–6 h into Monday, derived
from the node secret), with a 6-hour back-off after a failed send. It
refreshes playlists every `refresh_interval_h` hours. A manual "Run
now" always works. A second send for a week that was already sent is
acknowledged by the pool as a duplicate and counts nothing.

## 7. The opt-in switches (consent split)

`download_grab` is in the base signal set: it makes the same claim
as `recent_add` ("I acquired this"), just for paths the library
can't see. `download_churn` is different in kind — it reports a
**deletion** — so it has its own switch, `share_churn`, **default
off**, settable on the dashboard ("Share drop signals (optional)",
with the explanation rendered under it) or via the
`METASYNC_SHARE_CHURN` env var. With the switch off, the node still
*tracks* download history and snapshots locally (that state never
leaves the machine) but computes and sends **zero** churn signals.
`listen_album` has its own switch, `share_listening` ("Share listening
signals (optional)", dashboard or `METASYNC_SHARE_LISTENING`), **default
off**. With it off the node **does not read** listening history at all:
the §1 endpoint is never called. With it on, SoulSync returns per-album
counts for the one week, from the shared listening pile only. A profile
that keeps its own pile is never included. The node turns those counts
into the binary, thresholded, capped signal of §2. The full privacy
design is DESIGN-LISTEN-SIGNALS.md.

Standing rule: new signal types never activate silently on existing
nodes — base-set additions ship announced and visible in the payload
preview before first send; toggle-gated types stay off until flipped.

## 8. What the backend stores (Neon Postgres)

Schema: `src/sin/schema.ts`, migrations `db/migrations/0001`–`0008`.

| Table | Columns (complete) | Purpose / retention |
|---|---|---|
| `report_tokens` | `week`, `token_hash` (PK) | One row per accepted report: the weekly dedupe token, so a second report for the same week counts nothing. **Holds no signals and is never joined to the counts.** Pruned after 3 weeks (the report window is 2). |
| `aggregate_counts` | `week`, `signal`, `mbid`, `node_count` (PK = week+signal+mbid) | Private running distinct-node counts, **including below-k ones**. A report adds +1 per distinct signal the moment it arrives, in the same transaction as its token claim. No route reads this table. |
| `raw_reports` | `id`, `week`, `token_hash`, `signal`, `mbid`, `received_at` | **No longer written** (migration 0008). The next aggregation pass drains any rows left by the old ingest path into `aggregate_counts` and deletes them. Drop the table once it is empty. |
| `aggregates` | `week`, `signal`, `mbid`, `node_count`, `computed_at` (PK = week+signal+mbid) | Distinct-node counts **after** the k filter — below-k rows are never written. The only table the public index reads. Kept indefinitely (it is the published history). |
| `index_publications` | `id`, `week`, `trigger`, `aggregate_count`, `raw_deleted`, `published_at` | Audit log of aggregation runs. No user data. |
| `portal_keys` | `id`, `user_id`, `key_hash`, `key_prefix`, `label`, `last_used_at`, `expires_at`, `created_at`, `updated_at`, `revoked_at`, `bound_node_id`, `bound_node_public_key`, `bound_at`, `pending_node_id`, `pending_node_public_key`, `pending_requested_at` | Pool keys (§10). Only `sha256(API_KEY_PEPPER + raw_key)` is stored; the raw key exists only on the user's machine. `user_id` exists in the table but **SIN function code never selects or returns it** (privacy rule P3); the portal app is the only reader of that column. |
| `sin_request_nonces` | `node_id`, `nonce_hash`, `expires_at` | Replay defence (§5). Hashes only, no bodies, no report content; rows expire with the ±5-minute window and are deleted opportunistically. |
| `canonical_playlists` | `week`, `key`, `name`, `tracks` (jsonb), `published_at` (PK = week+key) | The published charts (§9). `tracks` is the Spotify-shaped subset: per track `{id (MusicBrainz recording MBID), name, artists: [{name}], album: {name, images: [{url}]}, duration_ms?, image_url}`. |
| `mb_resolutions` | `mbid`, `kind`, `bundle` (jsonb), `resolved_at` | Composer's permanent MusicBrainz cache. Release-group bundle: `{mbid, kind, title, artistName, artistMbid?, genre?, year?, tracks, topTrackIds?}` (`topTrackIds`: the 5 most popular recordings, from ListenBrainz); artist bundle: `{mbid, kind, name, genre?, releaseGroupMbids}`. **Public catalogue data only** — nothing in it came from a user. |

## 9. Aggregation and composition — how counts become charts

**Counting** happens at ingest (`src/sin/index.ts::countReport`): in
one transaction, claim `(week, token)` in `report_tokens` (a repeat
claims nothing and counts nothing), then add +1 per distinct signal
to `aggregate_counts`.

**Aggregation** (`src/sin/index.ts::aggregateOnce`) is the publication
pass. It drains leftover legacy `raw_reports` into the counts, copies
every running count at **k ≥ 15** (`K_PUBLISH_THRESHOLD`) into
`aggregates`, prunes old report tokens, and logs the run in
`index_publications`. Consequences stated plainly:

- Below-k counts exist only in `aggregate_counts`, as bare numbers.
  They are never published and never read by a route.
- Counts are exact however a week's reports are spread out. The old
  path deleted raw rows 24 h after receipt and recounted only the rows
  left, so an item reported by 15 installs over more than a day never
  reached k.
- Publication is triggered (admin `POST /v1/aggregate`, and as the
  first step of compose). Nodes report the closed week through Monday
  and stragglers later, so compose a week once most reports are in
  (Tuesday or later), and re-compose to pick up late ones.

**Composition** (`POST /v1/compose`, admin scope; logic
`selectChartSpecs` + `buildEditions` in `src/sin/logic.ts`):
inputs are `aggregates` only — composition never reads raw reports.

- **Most Wanted**: `wishlist_add + hunt_demand` per group, top 25.
- **Loved & Kept**: `import_keep − download_churn` per group (both now
  fire once per album: keeps the week an album reaches 14 days, drops
  the week it is confirmed gone); albums whose net is ≤ 0 drop out. Churn aggregates exist only where ≥ 15
  **toggle-on** nodes reported that album's churn, so the subtraction
  never mixes populations; where no churn aggregate exists there is
  no dampening. Churn is a ranking input only — no chart displays it,
  and chart reason strings cite keeps only.
- **Just Landed**: `recent_add` counts, top 25.
- **On Repeat**: `listen_album` counts, top 25. It exists only where at
  least 15 opted-in installs played an album that week.
- **Trending**: per-group heat = **max(wishlist_add, download_grab)**
  (the public index adds `watchlist_add` on top for artists), scored
  `heat / (1 + mean heat over the prior weeks present)`, top 25.
- **Rising**: demand = `wishlist_add + hunt_demand + recent_add`,
  week-over-week growth ≥ 2× (or new), top 25; needs a previous week.
- **Breakout Artists**: `watchlist_add` artists, current ≥ 20 nodes
  (`BREAKOUT_MIN_NODES`) and ≥ 2× the previous week (or new), top 10.
- **Editions**: genre and decade editions of Trending and Most
  Wanted only, built from resolved bundles; an edition needs ≥ 8
  chart items sharing the tag (`CHART_MIN_EDITION_ITEMS`). Playlists
  cap at 400 tracks (`CHART_MAX_TRACKS`); flagship album charts at 25.

**Tracks per album**: each charted album contributes at most **5
tracks**, its most popular by distinct ListenBrainz listeners (then total
listens), or its first 5 when ListenBrainz has no data for it.

A chart spec is published (upserted into `canonical_playlists`)
**only when every item in it resolved** through MusicBrainz within
the pass budget; otherwise it is reported `pending` with the missing
count and a later compose pass resumes it (resolutions are cached,
so passes converge). The public index endpoints
(`GET /v1/index/…`) serve aggregates-derived items with plain reason
strings (e.g. "Added by N collectors this week").

## 10. Portal accounts and keys

The portal (`portal/`, Auth.js + Prisma) is the only component that
knows who a user is:

- **`portal_users`**: `id`, `email` (unique, lowercased), `name`,
  `image`, `email_verified`, `role` (`user` | `admin`, from the
  `ADMIN_EMAILS` env list), timestamps.
- **`portal_accounts`**: the Auth.js adapter table — provider,
  provider account id, and the OAuth tokens Auth.js stores
  (`access_token`, `refresh_token`, `id_token`, scopes, expiry).
  This is standard Auth.js storage for Google/GitHub sign-in; SIN
  never reads it.
- **`portal_sessions`**, **`portal_verification_tokens`**: Auth.js
  session tokens / (unused-at-launch) magic-link tokens.
- **`portal_keys`** (§8): label (user-chosen), hash, 12-char prefix,
  lifecycle timestamps, binding fields. Rules: raw keys are
  `sin_live_` + base64url(32 random bytes), **shown once** at
  creation; **max 5 active keys per account**; a key is valid iff
  `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)`.
  Binding approval, rejection, unbind, and key roll (binding carries
  to the replacement key) happen here; the SIN backend only ever
  answers pass/fail for a presented key/token.

The two stores are deliberately unjoined in code: the accounts store
(portal tables) and the reports store (SIN tables) share a database
but no SIN code path selects `portal_keys.user_id` or joins a key
to a person (§15 states the limit of this honestly).

## 11. Pairing and the local channel (SoulSync ↔ node)

Separate from pools entirely — this is two programs on the user's
own machine/LAN (`node/metasync_node/service.py`,
`node/metasync_node/app.py`):

- **Manual**: the user pastes SoulSync's URL + `sk_` key into the
  node's settings. Stored in `settings.json` (§6).
- **One-click pairing**: SoulSync `POST /local/pair/request` → node
  creates a 10-minute session `{session_id, 6-digit pairing_code}`
  and returns `{session_id, pairing_code, node_fingerprint}` → the
  user approves in the node dashboard → SoulSync
  `POST /local/pair/claim {session_id, pairing_code, soulsync_name,
  soulsync_url, soulsync_api_key}` → the node stores the SoulSync
  connection and returns, **once**, `{local_api_key, node_id,
  node_fingerprint}`. The local API key is `mn_local_` + 32 random
  bytes; only its SHA-256 hash is stored. Claims are single-use;
  all failures return one generic error (no oracle); pairing
  requests are rate-limited (5 per 10 minutes).
- **Pool-key handoff**: SoulSync (holding the local key as Bearer)
  `POST /local/handoff/pool-key {pool_url, pool_api_key, …}` → the
  node stores the pool (§6) and immediately attempts the §5
  exchange. Also `GET /local/status` and `POST /local/disconnect`
  on the same channel.
- Connecting MetaSync to a SoulSync joins **nothing**: a pool is
  joined only when a pool URL **and** pool key are both supplied,
  through SoulSync's handoff or the node's own pool form (the same
  single entry point in code).

## 12. Secrets inventory (names only)

Backend env (Neon function env / `.env.local`, never committed):
`DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `SIN_ADMIN_KEY` (the
official node's admin credential), `API_KEY_PEPPER`,
`SIN_TOKEN_PRIVATE_KEY`. Portal env (Vercel): `DATABASE_URL`,
`API_KEY_PEPPER` (same value as the backend's), `AUTH_SECRET`,
`AUTH_GOOGLE_ID` / `AUTH_GOOGLE_SECRET`, `AUTH_GITHUB_ID` /
`AUTH_GITHUB_SECRET`, `ADMIN_EMAILS`, `POOL_URL`. Node env
(bootstrap only; the settings file wins afterwards): `SOULSYNC_URL`,
`SOULSYNC_API_KEY`, `METASYNC_DRY_RUN`, `METASYNC_SHARE_CHURN`,
`METASYNC_STATE_DIR`, plus pool seeding via `default_pool_from_env`.

## 13. What comes back to the user

Nodes pull `GET /v1/playlists` (the week's canonical charts with
full track lists) and cache them in state. Materialization
(`service.py::_materialize`) writes each chart into the user's
SoulSync via `POST /api/v1/playlists/<id>/sync` with
`{playlist_name, tracks, image_url: "", sync_mode: "replace"}` under
a stable local playlist id derived from (pool id, chart key).
Unchanged charts (same content hash) are skipped. **Nothing
auto-downloads**: a chart arriving creates/updates a playlist
definition in SoulSync; SoulSync's own mirroring decides what to
fetch, when the user syncs it. Dry-run mode materializes nothing.

## 14. Never collected — anywhere in the system

Listening history, play counts, play times, last-played data of any
kind (the opt-in `listen_album` is one bit per album per week, for at
most 50 albums, and the node reads it as weekly per-album counts that
never leave the machine — §2, §7) · track
titles / artist names / album names **as user data** (titles exist
only inside `mb_resolutions` / `canonical_playlists`, sourced from
MusicBrainz's public catalogue, identical for every user) · file
paths · library listings or library size as a transmitted fact
(the node reads the library locally; reports contain only the
signals above) · SoulSync URLs · usernames, emails, or IPs as
stored data (§15 for the transport caveat) · per-track download
records (download data crosses SoulSync → node only as per-album
counts, and node → pool only as the two binary signals).

## 15. Honest limits — what this design does not hide

For the reviewer; these are properties of the system as built, not
bugs anyone is hiding:

1. **Transport metadata.** The backend terminates HTTPS, so at the
   HTTP layer a request arrives from an IP at a time. Nothing in
   the schema stores it, and rate limiting counts requests in
   memory only — but "never collected" (§14) means "never stored
   or used", not "physically unobservable by the server operator".
2. **The door knows the node; the data doesn't.** Token-authenticated
   calls re-check binding per request (§5), so the backend
   necessarily learns "bound node X made a report call in week W".
   Since migration 0008 nothing is stored per report: the token row
   holds no signals, and the counts hold no token. Inside the one
   request, though, a node's whole weekly report arrives together,
   and an operator who logged request bodies at that moment would
   see it. That is why reports are capped and describe one week of
   events, never a library (§2, bulk rule). Across weeks, tokens are
   unlinkable.
3. **The portal join exists in the data, not in code.** `portal_keys`
   carries `user_id` in the same database as the reports. No SIN
   code path selects it, and the portal is the only component that
   reads identity — but a database operator with raw access could
   join key → account, and (via `bound_node_id` + door logs that
   don't exist as stored data) narrow further. The separation is
   enforced by code discipline and this document, not by physical
   separation of databases.
4. **Below-k counts exist at rest** (§8, `aggregate_counts`): bare
   numbers per (week, signal, MBID), e.g. "3 installs played this
   album". They hold no token or identity and are never served. An
   operator watching the counts change between two moments could
   attribute a delta to whatever reports arrived in between; the
   per-node slot (§6) spreads arrivals over Monday's first six hours
   so those moments rarely hold one report.
5. **Aggregates are counts, published wide.** Above k, the counts
   are the product — anyone can read the public index. An item at
   exactly 15 nodes reveals that 15 installs reported it; the
   *which* is never in the data.
6. **Churn's population is the toggle-on set.** Churn only counts
   where users opted in (§7, §9). The composer's population rule
   keeps the math honest, but Loved & Kept is dampened by a
   self-selected population by design.
7. **The node's local files are the user's responsibility.**
   `settings.json` holds the SoulSync key and pool key in plaintext
   on the user's disk (0600 state dir conventions aside, no
   encryption at rest). Anyone with the state dir has the node's
   identity and keys.

## 16. What changed in the Oct 23 review, and why

| Before | After | Why |
|---|---|---|
| The report covered the week that had just **started** (sent at Monday 00:00) | It covers the week that just **closed** | "This week" signals covered about a minute and were almost always empty |
| `import_keep` every week for every album 14+ days old | Once per album, the week it crosses 14 days | Each report was close to a whole-library listing |
| No limit per type (100,000 total) | 500 per event type on the node (bulk held back), 1,000 per type and 5,000 total at the pool | A bulk import is not taste, and a report is never a library dump |
| Raw rows deleted 24 h after receipt, then recounted | Counted on arrival; nothing per node stored | Counts were lost whenever a week's reports were more than a day apart; per-node rows sat at rest |
| Any week accepted; re-sends topped up | Only the closed or the current week; one report per node per week | No backfilling or pre-stuffing; exact counts |
| Raw portal keys accepted on data routes | Env keys only; portal keys only at the exchange | The direct door skipped node binding entirely |
| Churn snapshots rotated per run | One per week | Two runs a minute apart counted as two absences |
| Scheduler retried every 60 s after a failure or in dry run | Once per week, per-node slot, 6 h back-off | Each retry re-read the whole library |
| Exchange 404 → direct-key mode for any pool; HTTP pool URLs allowed | Loopback only; HTTPS required off loopback | A proxy or wrong URL downgraded the node to sending its key in the clear |
| No listening signal | Opt-in `listen_album` (§2, §7) | Boulder's request; see DESIGN-LISTEN-SIGNALS.md |

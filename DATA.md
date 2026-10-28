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
the server** → nodes pull the charts back and offer them to the user's
own SoulSync, which pulls them once a day and shows them as playlists
the user can open (§13). Nothing in the system ever moves a title, a
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
| `wishlist_add` | release-group MBID | wishlist item's `date_added` is in the report week and a group MBID was found — or it was added up to 14 days earlier and matched only now (**late match**, below) |
| `hunt_demand` | release-group MBID | wishlist item's `date_added` is 7+ days before the week's end, SoulSync has **tried** it (`retry_count ≥ 1`), and the album is **not already in the library**. SoulSync removes fulfilled items, so still listed + tried = "searched for, still not got" (GAP-4) |
| `import_keep` | release-group MBID | album's 14-day mark falls **in the report week**, counted from the **earliest** date the node has seen for the group (`first_seen`, below), it is still in the library, and its release resolves to this group — **once per album**, never re-sent |
| `recent_add` | release-group MBID | album's `created_at` is in the report week, its release resolves to this group, and the node has **not** seen the group before (a re-scan that resets `created_at` is not an arrival) — or it landed up to 14 days earlier and matched only now (**late match**) |
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
import-date proxy (GAP-3); there is no dedicated `imported_at`. A
re-scan or migration can reset it, so the node keeps `first_seen`: per
release group, the earliest `created_at` it has ever read. `recent_add`
skips groups seen before, and `import_keep` counts from the earliest
date, so a reset re-sends neither.

**Late matches** (GAP-1). A wishlist row or library album that had no
release group in its own week (SoulSync's MusicBrainz worker had not
matched it yet) waits in the node's `late_pending` for up to 14 days.
When it is matched, its `wishlist_add` / `recent_add` goes in that
week's report, once. A re-run of the same week (a dry-run preview, then
the real send) gives the same report; the next week never repeats it.

## 3. What the signals become — report payload

`POST /v1/report` body, built by
`node/metasync_node/service.py::payload_for_pool` — the **entire**
payload, nothing else is in it:

```json
{
  "week": "2026-W43",
  "token": "<64 hex chars>",
  "signals": [{ "type": "import_keep", "mbid": "<uuid>" }, …],
  "churn_optin": true
}
```

`churn_optin` is present (and `true`) **only** when the user turned on
"Share drop signals"; every other node leaves it out. The pool counts
how many reports carry it each week (`report_populations`, §8) so
Loved & Kept can weigh drops from the opted-in share against keeps from
everyone (§9). It is never stored per node.

Server-side validation (`src/sin/logic.ts::validateReport`) is strict:
the body may contain **exactly** `week`, `token`, `signals`, and the
optional `churn_optin` (which must be `true` when present); each
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
used to pick the album's most popular tracks for the playlists (§9).
A fourth public read, `GET /1/stats/sitewide/recordings?range=week`,
is ListenBrainz's weekly song chart, behind **Top Tracks · from
ListenBrainz** (§9).
Once per album, cached in its `mb_resolutions` bundle (`topTrackIds`). No
node ever talks to ListenBrainz for this, and nothing in the request came
from a SIN user: SIN collects no track-level data at all.

**ListenBrainz public charts, from the composer only**
(`src/sin/public-charts.ts`): `GET /1/stats/sitewide/release-groups?range=week&count=1000`
(what ListenBrainz's whole user base played most last week) and
`GET /1/explore/fresh-releases/?days=30` (what was released recently).
Plain public reads with no parameters from SIN's data. They feed the
cold-start fill (§9).

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
  - `library_first_seen` — release group MBID → the earliest
    `created_at` the node has read for it (§2, GAP-3);
  - `late_pending` — SoulSync row ids (wishlist rows, library albums)
    still waiting for a release group, with their date, for at most
    14 days (§2, late matches);
  - `previews` — per pool, the **exact last report payload** (week,
    token, signals) so the preview page renders a snapshot, never a
    recomputation;
  - `pool_auth` — per pool: exchange mode, binding status, the live
    **access token** and its expiry, last exchange result;
  - `last_report` (per pool: week, time, signal count),
    `last_refresh_at`, `index_cache`, `playlists_cache` (the charts
    offered on `GET /local/charts`), `built_definitions` (admin mode only), `report_job`
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

Schema: `src/sin/schema.ts`, migrations `db/migrations/0001`–`0010`.

| Table | Columns (complete) | Purpose / retention |
|---|---|---|
| `report_tokens` | `week`, `token_hash` (PK) | One row per accepted report: the weekly dedupe token, so a second report for the same week counts nothing. **Holds no signals and is never joined to the counts.** Pruned after 3 weeks (the report window is 2). |
| `aggregate_counts` | `week`, `signal`, `mbid`, `node_count` (PK = week+signal+mbid) | Private running distinct-node counts, **including below-k ones**. A report adds +1 per distinct signal the moment it arrives, in the same transaction as its token claim. No route reads this table. **Deleted 3 weeks after its week**: once a week can no longer receive reports, the aggregation pass publishes everything at or above k, then drops the week's remaining (below-k) counts. |
| `report_populations` | `week` (PK), `reports`, `churn_optin` | Per week: how many reports were accepted, and how many carried `churn_optin`. +1s in the same transaction as the token claim (migration 0010). **Counts only, nothing per node.** Used to scale drops in Loved & Kept (§9) and by `GET /v1/admin/status`. |
| `raw_reports` | `id`, `week`, `token_hash`, `signal`, `mbid`, `received_at` | **No longer written** (migration 0008). The next aggregation pass drains any rows left by the old ingest path into `aggregate_counts` and deletes them. Drop the table once it is empty. |
| `aggregates` | `week`, `signal`, `mbid`, `node_count`, `computed_at` (PK = week+signal+mbid) | Distinct-node counts **after** the k filter — below-k rows are never written. The only table the public index reads. Kept indefinitely (it is the published history). |
| `index_publications` | `id`, `week`, `trigger`, `aggregate_count`, `raw_deleted`, `published_at` | Audit log of aggregation runs. No user data. |
| `portal_keys` | `id`, `user_id`, `key_hash`, `key_prefix`, `label`, `last_used_at`, `expires_at`, `created_at`, `updated_at`, `revoked_at`, `bound_node_id`, `bound_node_public_key`, `bound_at`, `pending_node_id`, `pending_node_public_key`, `pending_requested_at` | Pool keys (§10). Only `sha256(API_KEY_PEPPER + raw_key)` is stored; the raw key exists only on the user's machine. `user_id` exists in the table but **SIN function code never selects or returns it** (privacy rule P3); the portal app is the only reader of that column. |
| `sin_request_nonces` | `node_id`, `nonce_hash`, `expires_at` | Replay defence (§5). Hashes only, no bodies, no report content; rows expire with the ±5-minute window and are deleted opportunistically. |
| `canonical_playlists` | `week`, `key`, `name`, `tracks` (jsonb), `published_at` (PK = week+key) | The published charts (§9). `tracks` is the Spotify-shaped subset: per track `{id (MusicBrainz recording MBID), name, artists: [{name}], album: {name, images: [{url}]}, duration_ms?, image_url}`. |
| `mb_resolutions` | `mbid`, `kind`, `bundle` (jsonb), `resolved_at` | Composer's permanent MusicBrainz cache. Release-group bundle: `{mbid, kind, title, artistName, artistMbid?, genre?, year?, tracks, topTrackIds?}` (`topTrackIds`: up to 5 most popular recordings, from ListenBrainz; a playlist uses 2); artist bundle: `{mbid, kind, name, genre?, releaseGroupMbids}`. **Public catalogue data only** — nothing in it came from a user. |

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
  first step of compose). The scheduled GitHub Actions workflow
  `.github/workflows/compose.yml` composes **every day at 08:15 UTC**:
  after Monday's report slots (0–6 h), and again each following day to
  pick up late reports. It repeats compose until nothing is pending (up
  to 6 passes), then reads `GET /v1/admin/status` (admin key: report
  counts and chart keys for the closed week) and **fails** — GitHub then
  emails the workflow's owner — when the closed week has no charts, or
  when reports **stopped**: none for the closed week by Tuesday although
  the week before had some. (Before the first live report, zero is
  expected and only noted.) Report counts come from `report_tokens`.
- Compose builds **the week that just closed** unless told otherwise.
  (It used to default to the latest week with published counts, which
  would have pinned the charts to that old week whenever no newer week
  reached k.)

**Composition** (`POST /v1/compose`, admin scope; logic
`selectChartSpecs` + `buildEditions` in `src/sin/logic.ts`):
inputs are `aggregates` only — composition never reads raw reports.

- **Most Wanted**: `wishlist_add + hunt_demand` per group, top 25.
- **Loved & Kept**: `import_keep − download_churn × scale` per group
  (both fire once per album: keeps the week an album reaches 14 days,
  drops the week it is confirmed gone); albums whose net is ≤ 0 drop
  out. Keeps come from every reporter but drops only from the
  **opted-in** share, so raw drops undercount by that share: `scale` =
  reports ÷ opted-in reports for the week (`report_populations`), at
  least 1 and at most 5 (`CHURN_SCALE_MAX`), and 1 for weeks without
  population counts. Churn aggregates exist only where ≥ 15 toggle-on
  nodes reported that album's churn; where none exists there is no
  dampening. Churn is a ranking input only — no chart displays it,
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
- **Editions**: genre and decade editions of Trending, Most Wanted and
  Just Landed, built from resolved bundles. An edition needs ≥ 8 chart
  items sharing the tag (`CHART_MIN_EDITION_ITEMS`). Just Landed gets
  genre editions only (everything new is from this decade). An edition
  holding every item of its chart is skipped, and so is one whose
  published tracks are ≥ 80% its chart's own (`EDITION_MAX_OVERLAP`,
  checked after both are built); a skipped edition is withdrawn like any
  chart the pass no longer makes. An edition of a
  public-filled chart carries the same honest name ("Trending: Rock ·
  from ListenBrainz"). Album charts hold 25 albums; every playlist caps
  at 50 tracks (`CHART_MAX_TRACKS`).
- **Top Tracks · from ListenBrainz**: ListenBrainz's public weekly song
  chart, top 50, at most 2 songs per artist (one fan base can otherwise
  fill it). Always public: SIN collects no track-level signal, so there
  is no network version to replace it.

**Cold-start fill** (`fillFromPublic`, `chartDisplayName`): until the
network can fill them, **Trending** and **Just Landed** take
ListenBrainz's public weekly top albums (Just Landed: the ones released
in the last 30 days) in the slots the network leaves empty, up to 25.
Network items always rank first, so the public share falls to zero on
its own as the network grows. The playlist **name says so**: "Trending
· from ListenBrainz" (all public), "Trending · SoulSync + ListenBrainz"
(mixed), plain "Trending in SoulSync" (network only). A public album MusicBrainz
cannot turn into a tracklist is skipped, not waited for. Network items
still must all resolve before a chart publishes.

**History and decay**: `aggregates` keeps every published week, since
they are public counts already above k. Each chart describes its own
week only. Trending scores against the prior 3 weeks, so a steady
seller ranks below a riser, and Rising and Breakouts compare with the
previous week. Nothing older than that influences any chart. Private
below-k counts are dropped after 3 weeks (§8).

**Tracks per album, and order**: each charted album contributes its **2**
most popular tracks by distinct ListenBrainz listeners (then total
listens), or its first 2 when ListenBrainz has no data for it, so 25
albums make a 50-track playlist. The tracks are laid out **round by
round**: every album's best track first, in chart order, then every
album's second. The strongest songs lead, one album never plays twice
in a row, and the order changes only when the chart does. A random
shuffle would instead rewrite a synced playlist every day.

**Withdrawal**: a chart a compose pass no longer produces for the week
(an edition whose genre fell under 8 albums, say) is deleted rather than
served stale. A pass that produces nothing at all withdraws nothing.

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
- **`portal_accounts`**: the Auth.js adapter table — provider and
  provider account id only, the link that says "this GitHub/Google
  account signs in as this user". The OAuth tokens a provider returns at
  sign-in are **not stored** (`portal/src/lib/oauth-account.ts`; migration
  0009 cleared those stored before): the portal never calls GitHub or
  Google on anyone's behalf, so it keeps no credential to call them
  with. SIN never reads this table.
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
full track lists) every `refresh_interval_h` hours and cache them in
state. **The node never writes into SoulSync.** It offers the cache on
its local API, `GET /local/charts` (local key), and SoulSync pulls from
there:

- SoulSync's system automation **"Refresh MetaSync Charts"** (daily)
  fetches the charts (`core/metasync_charts.py`), stores them, and
  matches every track in the background. That is the same identify pass
  My Playlists use, and its cache makes repeats instant.
- A chart is shown only once every track is matched: a **SoulSync Charts**
  tab on SoulSync's dashboard, and a shelf on Discover.
- Opening one gives that profile its own playlist with the matches
  already in it, and lands on it in Sync. **Nothing is synced to a media
  server, wishlisted or downloaded until that person presses Sync &
  download** (or schedules Auto-Sync on it).

Until Oct 23, 2026 the node pushed each chart through SoulSync's
`POST /api/v1/playlists/<id>/sync`. That call also put every missing
track on the wishlist, which SoulSync's default wishlist automation then
downloaded. It was removed before any chart had published.

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

## 17. The metadata network connection (node side)

Separate from everything above: SIN moves counts about the *user*;
the metadata network moves facts about the *music* (DESIGN-METADATA.md).
This section covers only the node's connection to it.

- **The relay.** Nodes cannot accept inbound connections, so they hold
  one persistent outbound WebSocket to a relay: a small always-on
  program on the operator's infrastructure that forwards queries to
  connected nodes and routes their answers back by query ID. The relay
  **stores nothing** — no claims, no queries, no answers. Presence
  (which node keys are connected) lives in its memory only and is gone
  when it restarts. It has no database access at all: at connect
  time it verifies the node's pool exchange token offline against
  the pool's public key, and checks the issuer's signed revocation
  list. Whether the node's key is valid and bound was checked by
  the portal when it issued that token, not by the relay.
- **Early close.** A query's window runs its full course by default,
  but the ASKING node may end its own query early with a `close`
  message carrying the query id (the querier's sufficiency rule is
  §19). It is a post-v2 addition negotiated by CAPABILITY, not a
  version bump: the relay advertises `early_close` in its v2 welcome
  capabilities, and a node sends `close` only to a relay that
  advertised it (the client adopts the capability only when the
  advertised value is exactly JSON `true`). Capability negotiation
  remains the client-side discipline that decides whether a close
  is ever SENT; relay-side, a close is honored only on OWNERSHIP
  AND protocol v2 — the same connection the query was routed on,
  speaking version 2 or above. A close from a v1 connection is
  ignored, so v1 traffic keeps exactly its pre-close treatment.
  A relay that predates the message ignores it (the dispatch
  already ignores unknown message types on v2 connections),
  and a peer that never sends one behaves exactly as before — its
  queries end at the window. The relay honors a close ONLY from the
  owning v2 connection; a close for an unknown, finished, or foreign
  query is ignored with identical silence, so the response never
  reveals whether the query exists. On an honored close the query's fan-out
  state is freed at once and the id joins the same purged tombstones
  a window-expired query leaves. A tombstone remembers the asker's
  node id: if the asker itself re-registers the spent id it gets the
  `duplicate_query` refusal, but a NON-owner re-registering it gets
  exactly the treatment an id still open would draw (the
  `query_id_in_use` violation) — the response must not tell a fan-out
  recipient whether the asker has already closed. Answers still in
  flight are dropped
  silently and are NEVER charged — not against the per-query total
  byte budget, not against the answering node's hourly byte budget,
  never a strike — because they were solicited in good faith before
  the close. No `complete` is sent back: the asker initiated the
  close and ends its own stream. The relay counts honored closes
  (`queries_closed_early`) in its aggregate counters; nothing else
  about a close is recorded. One inference survives all of this,
  named rather than hidden: an answer sent while the query is
  still open IS charged against the answering node's hourly byte
  budget while an answer sent after a close is dropped uncharged,
  so a fan-out recipient watching its own budget can tell open
  from closed — the same inference window expiry already allowed,
  accepted as low-value.
- **The switch.** The node connects only when the user turns the
  metadata network on: settings `meta_network`, **default off**, plus
  a relay URL in `meta_relay_url`. With no URL stored, the node dials
  the shipped default destination, `wss://relay.metasync.ssync.net`; a stored
  URL wins over the default, and a stored empty or null value means
  dial nowhere. With the switch off the node never dials the relay.
  The connection authenticates with the pool exchange token (which
  now carries the `meta:read` scope) plus a signature by the node
  key binding a per-connection challenge from the relay, so a
  captured hello cannot be replayed.
- **The mode.** `meta_node_mode` says how the node answers other
  nodes' queries: `full` (own library plus re-served cache),
  `own_library` (**default**: answer, never re-serve), or `leech`
  (never answer; asking still works). Asking needs no approval.
- **This step is transport only.** The client keeps the connection
  alive (reconnect with backoff, heartbeat) and can ask and answer in
  signed claim batches it does not parse. What a node answers from
  (its claims stores) and how it verifies and merges what comes back
  are the responder and search steps that follow; query content itself
  is covered there and in DESIGN-METADATA.md §12.

## 18. The metadata responder (node side)

What the node answers from, and the rules it answers under
(DESIGN-METADATA.md §5/§6/§9; code in `node/metasync_node/meta/`).

- **The local claims store** is one SQLite file,
  `meta_claims.sqlite3`, in the node's state dir — nothing about it
  leaves the machine. It holds: the node's **own-library claims**,
  built from its SoulSync's metadata export by `meta/builder.py`
  (PR #12's claim construction: provider-id claims only on a
  resolved match status, scalars sourced by `canonical_source`
  else `file_tags`, durations passed through as milliseconds,
  album_track variants `mb:<release_id>` only when the MusicBrainz
  match resolved, never an album MBID minted from a release id —
  the release-group MBID is claimed from the export's own
  `musicbrainz_release_group_id` field when present and resolved —
  plus `provider_link` claims: an album's resolved provider ids
  naming each other (`same_entity`) and, per provider shared with
  its artist, the artist's id on it (`album_of`));
  the **answer cache** of other nodes' batches, stored frozen with
  origin signatures intact (written by the querier step; the
  responder only reads); and two derived indexes — a provider index
  (`provider:external_id` -> subject, from provider-id claims and
  both ends of provider_link claims — each row records BOTH the
  kind of the entity the ref names AND the kind of the subject
  the row belongs to, and resolution with a known kind matches
  on the subject's kind for every row type: providers number
  artists, albums and tracks separately, so an artist's Deezer
  id claim never resolves an album or track subject through a
  numeric collision — and a link's to-end does not pull its
  subject into another kind's ask either (an album answers an
  ALBUM ask through its album_of link, where that traversal is
  the honest case; a track ask for the artist's number must not
  manufacture an album record). Rows indexed before subject
  kinds were recorded read as kind-unknown and match any kind)
  and a name index (normalized
  name tokens -> subject, in the SoulID normalizer's family).
  Builds walk the export incrementally (cursor + `since` = walk
  start − 1 h, per-owner replacement, so re-exports never
  duplicate); the −1 h assumes the node's and SoulSync's clocks
  agree to well within an hour — the overlap only buys protection
  against small skew, and a clock off by more than that can miss
  rows until the next full walk. Each export page lands in one
  commit (its seen-owners and claim replacements together), so a
  crash leaves a page wholly written or wholly unwritten. A
  completed full walk drops claims whose rows left the library —
  guarded: a walk that saw zero rows while the store holds claims,
  or a sweep that would drop over half the store's own claims, is
  refused as a stopped build and deletes nothing. Incremental
  walks cannot see deletions at all, so a full walk is also due
  every 7 days regardless of incremental freshness. The scheduler
  rebuilds when due (answering on, last build ≥ 20 h old, or a
  full walk due); `NodeService.rebuild_meta_store()` is the
  manual trigger. Track slot lookups (`album_track` claims by the
  track inside their JSON value) run on a partial expression
  index over `json_extract(value_json, '$.track')`, not a scan.
- **Answering is its own opt-in.** Settings `meta_answering`,
  **default off**, separate from `meta_network` and from every SIN
  switch. The relay query handler is registered only while both
  `meta_network` and `meta_answering` are on; turning either off
  deregisters it on the next scheduler tick, no restart.
- **Modes** (`meta_node_mode`, read live per query): `leech` never
  answers (the handler refuses even if a query arrives);
  `own_library` (default) answers from the node's own claims only;
  `full` also **re-serves** answer-cache batches — but only batches
  whose every claim passes the §7.1 re-serve rule (one predicate,
  `claim_reservable` in `meta/store.py`, shared by the cache filter
  and the own-batch split): bare provider-id and `provider_link`
  claims always pass (§7.1 names bare ID links as what travels,
  whatever source stamped them), while catalogue values must be
  `file_tags`- or `musicbrainz`-sourced. The caution is field-level
  too: a `duration_ms`, `isrc`, or `display_name` claim not sourced
  from MusicBrainz blocks its batch even when its source is
  `file_tags` — those fields are answered from a node's own
  library but not forwarded. Re-served batches keep the origin
  node's signature and ride in this node's signed forwarding
  envelope; answers from the node's own library are minted fresh
  per query in the same split — up to two signed batches per
  subject (re-servable partition first, own-only second), capped
  at 50 batches per answer.
- **Query shapes.** Exact by SoulID; exact by provider ref
  (`deezer:3135556` as a subject, or a `provider_ids` list; the
  provider name matches case-insensitively and surrounding
  whitespace is stripped, the ID itself stays exact) resolved
  through the provider index — the answer carries the very
  provider-id/link claim that matched, so the asker can verify the
  mapping locally; text by terms, matched over the name index
  (exact token, prefix, or one edit away), best matches first, at
  most 25 subjects. An exact track answer also carries the
  album_track claim that places it.
- **Preview.** `POST /local/meta/preview` (local API key) runs the
  responder's exact code path locally and returns what the node
  would answer for a query body — nothing is sent anywhere.

## 19. The metadata querier (node side)

What the node asks, what it lets in, and how answers become one
merged view (DESIGN-METADATA.md §5–§9; code in
`node/metasync_node/meta/querier.py`, `admission.py`, `merge.py`,
`verify.py`).

- **Cache-first asking.** An exact query resolves each entry
  locally first — a SoulID as itself, a provider ref
  (`deezer:3135556`) through the provider index, a legacy SoulID
  ONLY through a verified alias (below). Subjects with any LIVE
  local claims (own library or answer cache) are answered
  locally; the relay is asked only for the rest — and "live" is
  §6's aging rule: a subject whose only cached claims are
  EXPIRED counts as missing and is re-asked, on both the exact
  and the text path (a text query whose local resolution is all
  expired goes to the network like an empty one). A provider ref
  nothing local resolves rides to the relay verbatim, as an
  opaque subject. A second identical query therefore sends zero
  relay queries. Asking needs no opt-in beyond the
  relay connection itself (`meta_network`); without a client,
  queries answer from local data + cache alone and say so
  (`asked_network: false`).
- **Early close — when an ask stops waiting (§17).** An EXACT ask
  rides its full window unless the merged view of what has ALREADY
  arrived is sufficient, where SUFFICIENT is pinned to merge's own
  terms: for EVERY subject the query carried, the subject's merged
  record is found AND at least one merged IDENTITY value on it — a
  provider id, an MBID, or an ISRC (claims.py's identity set) —
  carries support ≥ 5, support in merge's exact sense (distinct
  standing origins, this node's own key excluded, `network`-sourced
  rows excluded). Agreement on anything else never suffices: a
  SoulID subject's name is what its id is computed from, so a crowd
  can unanimously agree on the name while every provider id is
  still contested — and contested ids are exactly what the rest of
  the window is for. A provider ref riding verbatim counts only once it
  has resolved; an unresolved ref blocks sufficiency, since a
  later answer may still hold it. A RESOLVED verbatim ref carries
  a further condition: the identity value MATCHING the asked ref —
  that provider's field carrying that exact id — must itself carry
  support ≥ 5 on every subject the ref resolved to. The general
  any-identity bar cannot vouch for a verbatim ref, because the
  ref→subject link it rides on may rest on a single zero-standing
  assertion: one such key can plant `deezer:X` on a popular subject
  whose unrelated Spotify id is genuinely well supported, and the
  old rule closed on that strength before the real owner of X
  answered. If the matching value does not carry the bar, the entry
  counts as unresolved and blocks closing, exactly like a ref that
  has not resolved at all.
  Sufficiency additionally requires at least TWO DISTINCT DIRECT
  ANSWERERS WITH STANDING in the stream — nodes whose own batches
  arrived, in this stream, carrying standing; forwarding envelopes
  never make their carrier a direct answerer, and the asking
  node's own key never counts (it is not another node answering
  its own ask). The standing condition is load-bearing: without
  it, two fresh zero-standing keys answering directly plus
  forwarded standing batches would clear a rule meant to keep one
  cheap source from ending the window. Forwarded copies still count toward an
  identity value's support (they are the origin's signed claims,
  counted once under the origin), but the whole bar arriving as
  replays through ONE forwarder does not license closing: that one
  node could be replaying a stale cache while the origins' current
  batches (including retractions) are still in flight. When the
  standing bar lands before the second direct answerer, the check
  is deferred until that answerer arrives. A "forwarded copy"
  means the honest shape only: the full signed forwarding envelope
  (origin batch id, origin node, forwarded_at, signature) wrapping
  the origin envelope. Unwrap and classification share that ONE
  predicate — an earlier shape let a bare `{"forward": {},
  "envelope": ...}` unwrap to the inner batch at admission while
  classifying as NOT forwarded, so one forwarder's replays each
  counted as a separate direct answerer (standing 5, direct 5)
  and closed the ask on a single node's cache. Any envelope
  carrying a `forward` key short of the full shape is now dropped
  at admission as malformed, and the client's stamp check refuses
  the same shape before routing. The stamp check itself is
  stricter on a session that negotiated early close: a v2 relay
  stamps every answer it routes, so an answer arriving without a
  stamp (or with a stamp naming no sender) did not come through
  the routing and is refused outright — on an unnegotiated
  session, where a v1 relay never stamps, it still passes
  unchecked exactly as before. The plan's example bar was "5
  strong-verified answers": §7 verification runs only AFTER an
  ask returns, so fresh answers carry no verdict mid-stream and
  "strong-verified" cannot be evaluated for the answers a close
  would cut off — 5 distinct standing origins asserting the same
  value is the honest pre-verification form of that bar, and it
  is deliberately the crowd signal, not a per-origin verdict. The
  check runs on a real merge of the admitted cache (never on raw
  arrivals), first when 5 distinct standing origins have
  contributed AND two distinct direct answerers with standing
  have arrived —
  deferred to the second direct answerer when the standing bar
  lands first — and again at each doubling (10, 20, …), so its cost
  stays rare relative to the stream. Three properties are the
  point: a NICHE query never closes early — with fewer than 5
  standing origins in total, or fewer than two direct answerers,
  the check never even runs, and a
  subject nobody answered keeps the query open to the window's
  end (early close is a popular-item optimization only); closing
  is BEST-EFFORT — a lost close, an unnegotiated relay, or a
  checker error all leave behavior exactly as it was, the query
  ending at its window, and on an unnegotiated session the ask
  does not even end LOCALLY: the stream ends early only when the
  wire close was queued for delivery on the owning connection, so
  sufficiency reached without
  negotiation is simply unused and the window runs in full (an
  earlier shape ended the local stream anyway, leaving the relay
  forwarding — and charging answerers for — answers nobody read);
  and nothing downstream changes — what
  arrived still merges, verifies, and sanity-checks through the
  normal post-ask path, so an early-closed ask differs from a
  full-window one only in what never arrived.
  TEXT asks never close early. A text ask's terms re-resolve
  over a set that grows as answers land — resolution is
  progressive — so a sufficiency check mid-stream can only
  judge the subjects resolved SO FAR: the fast terms would
  read sufficient and the query would close over the obscure
  term whose answers arrive late, silently truncating its
  results. The exact path anchors on its ask-start subject
  set; the text path has no anchor, and completeness is the
  point of asking by name. Text asks ride the full window,
  always.
  Name the missed workload honestly: sufficiency is an
  EVERY-subject rule, so a multi-subject batch ask closes
  early only when EVERY carried subject clears the bar — rare
  enough that the optimization effectively serves
  single-subject click asks, the workload it was built for.
  Fan-out itself is one query per ask either way, so nothing
  about the relay's load changes for batch asks; they simply
  wait, as they always did.
  The cut-off tail's sharpest consequence is disputes. For
  fields no provider can verify in v1 (the Spotify, Tidal and
  iTunes ids), a dispute that would have arrived in the
  cut-off tail is not merely delayed: the admitted claims make
  the subject LIVE in this node's cache, so a later identical
  ask is cache-served and never re-asks — the one-sided view
  persists until the unverified claims age out (90 days).
  What is NOT exposed is promotion: support alone never
  promotes — the floor's legs require verification or a local
  confirmation fetch — so a cut-off dispute costs display-tier
  completeness, never a wrong value in the library. Accepted
  for v1.
  A quieter gradient rides along: claims admitted before a
  close are re-served with their original attribution — no
  support laundering — but a popular item's cached claims skew
  toward its FASTEST responders, and variants held only by
  slow nodes propagate less readily than the full-window set
  would have carried. Across many askers this partially
  self-corrects (each asker's close lands at a different
  point in its own stream), but the skew is real and is named
  rather than hidden.
- **Legacy aliases.** The export serves `soul_id` +
  `soul_id_legacy` per row; the builder learns the mapping into
  the store's `legacy_aliases` table ONLY when it verifies — the
  v2 id recomputed from the row's own name parts must equal the
  served `soul_id`. A legacy id is an alias entry, never a claim
  subject: the querier translates it before asking, and the
  responder answers a legacy-subject query from its own alias
  table the same way, silently when it holds no verified mapping.
- **Admission (§9) is the only door in.** Every received batch,
  direct or re-served (judged by its inner origin batch), passes
  four checks: (a) batch signature + digest + every claim valid;
  (b) an embedded attestation, when present, names the batch's
  signer — malformed or foreign-named attestations drop the
  batch; (c) `asserted_at` inside the attestation's
  issued_at..expires window; (d) the origin key not named on ANY
  held revocation list (the union — the issuer a batch claims is
  unverified at receipt). Failures of (a), (b), (d) drop the
  batch. STANDING is cryptographic: passing (a)–(c), attested age
  (`min(asserted_at, receipt) − bound_at`) ≥ 30 days, AND the
  attestation's signature VERIFYING against the issuer key this
  node holds for the issuer the attestation names (v1: the one
  configured `meta_issuer_public_key`, speaking for the trusted
  issuer id learned from this node's own attestation). Standing
  is recorded in the `batch_admission` table, in the same commit
  as the batch. Every case that cannot verify — an attestation
  from an issuer whose key this node does not hold, no key
  configured, this node holding no attestation of its own, a
  garbage signature, a young binding, a window miss, no
  attestation at all — is admitted at ZERO SUPPORT, exactly
  alike: shape, naming and window alone vouch for nothing,
  because fabricating all three costs a fresh key nothing.
  Standing is not frozen at first admission: a re-arrival of the
  same batch may UPGRADE the recorded verdict once it verifies
  here (never downgrade — a later unverifiable arrival cannot
  strip standing the door once verified). When a new
  revocation list is applied (relay welcome/push or pool
  exchange), the named keys' cached batches, admission rows, and
  verdicts are purged immediately; merged views are computed per
  read, so the purge IS the invalidation.
- **The merged record** (version 1 — consumers key off
  `version`) is what step 8's depot and step 9's view consume:
  `{version, subject, kind, name, fields, expired_claims,
  found}`. `found` is computed AFTER expiry filtering — a
  subject whose cached claims all expired reports `found: false`
  with no fields (its expired shell still surfaces the count in
  `expired_claims`). Each field holds `values` — one entry per
  distinct asserted value: `{value, tier, support,
  support_label, reports, local, origins, batches, sources}`
  plus `verified_at` (and `verification_stale` past 90 days) on
  verified entries — and `local_value` / `local_values` /
  `disputed`. SUPPORT is the count of distinct asserting ORIGIN
  keys with standing, excluding this node's own key and
  excluding `network`-sourced claims (zero support always);
  re-served copies count once. REPORTS is the count of distinct
  asserting origins holding a strong-verified verdict for the
  value — the multiplicity the duration consensus is weighted
  by. TIER is the best §7 verdict among the value's claim
  identities: `unverified` / `plausible` / `strong-verified`. The
  `support_label` bucket (`strong-verified` / `3+ nodes` /
  `1-2 nodes` / `unverified`) is the only form of the count a
  user surface may show, and origin keys never reach a user
  surface — both rules bind step 8/9, the record itself carries
  the provenance the depot's per-field index is keyed by (claim
  identity: origin key, subject, field, value; batch ids are
  evidence only). Local (own-library) values are marked `local`
  and named as the field's `local_value`; the merge never
  overwrites local data. `local` comes ONLY from the live
  own-library claims: a re-served copy of THIS node's own batch
  arriving through the cache merges as a network row (origin =
  this node's key, listed in `origins`, never local, never
  support) — otherwise a value this node once asserted and has
  since corrected would keep winning its field as "local"
  forever. Artist records carry a `disambiguation` block
  whenever §8's ladder has candidate evidence: `{settled, via,
  promotion, survivors, settled_at}` — which artist id a
  fetched link (or a shared strong MBID, or the local-link
  count) settled on, and when the settling evidence was
  verified. Field rules: provider ids, MBIDs and
  ISRCs are sets (every value kept; multiple strong/floor MBIDs
  flag the field `multiple`); year merges MusicBrainz-sourced
  values only (other network years are listed under `held`; the
  node's own year is its local value); genres union, ranked by
  tier then support, always `never_auto_fill`; durations merge
  per album_track variant context — a variant `consensus_ms` is
  the median of its STRONG reports WEIGHTED BY REPORT
  MULTIPLICITY (each value counted once per distinct origin
  holding a strong verdict for it — two origins reporting
  284000 outvote one reporting 290000; the distinct-value
  median would invent a number nobody reported), with >2 s
  outliers dropped, and a bare `consensus_ms` exists only when
  every variant context agrees within ±2 s; album_track
  variants never collapse (`variants` carries each variant's
  fingerprint, slots, and whether it settled), a variant-less
  tracklist keys by its (disc count, track count) shape —
  never one shared empty group — and a variant's fingerprint
  is §7's (disc, position, normalized title) hash, computed
  from the tracks' own name claims, so it compares directly
  against a fetched tracklist's. Unverified cached claims
  expire out of the view 90 days after their origin's
  `asserted_at` (counted in `expired_claims`); strong-verified
  claims do not expire on the timer. The merge is deterministic
  and reputation-free; display ordering is a separate
  presentation pass that PRESERVES tier precedence
  (strong-verified, then plausible, then supported-unverified)
  and lets reputation reorder only within the unverified,
  unsupported tail.
- **The depot projection (step 8's contract).** The full merged
  record above is the node's INTERNAL contract — step 9's view
  reads it from the querier directly. What the SoulSync-facing
  routes serve (`POST /local/meta/query`, `GET
  /local/meta/record/<entry>`) is a COARSE PROJECTION of it
  (`project_for_depot`): per subject, `{version, subject, kind,
  name, found, fields}` where each field carries its chosen
  value (the display-ordered head), the field's `local_value`,
  and one entry per distinct value reduced to `{value, tier,
  local, support, support_label}` — support as a BUCKET
  (`none` / `1-2` / `3+` — §9's buckets exactly; 1 and 2 are
  never distinguishable on this surface), never a count;
  album_track variants project per-track the same way (bucketed
  support, tier, local, the §7 fingerprint). Three projection
  rules bind what SoulSync may conclude: a field's head
  (`value` / `tier` / `local`) LEADS WITH THE LOCAL VALUE when
  one exists, regardless of tier ranking (display order still
  governs the values list beneath); a strong verdict older
  than the 90-day reliance window (`verification_stale`)
  projects as tier `unverified` — the internal record keeps
  the verdict and the flag for re-check, but the depot's
  promotion floor reads projected tiers, so stale evidence
  cannot promote; and `held` network years are NOT projected
  at all — only local or MusicBrainz-sourced year values reach
  SoulSync (§8). The projection
  NEVER carries origin keys, batch ids, exact support counts or
  report counts: §9 shows buckets to users, and SoulSync is a
  user surface. The disambiguation settlement rides the
  projection (provider ids are public identifiers, not
  provenance).
- **When SoulSync asks (the one rule).** SoulSync asks only on
  real interest — a click, a wishlist add, a download (design
  §13 step 8, Oct 28, 2026). A search result appearing never
  asks, and tagging reads the depot only: it never asks at all.
- **Verification (§7)** runs AFTER answers land — streamed
  results are unverified first, verdicts upgrade the cache as
  checks complete. Scheduling is RELIANCE-GATED: only subjects
  an exact ask returned — plus the album subjects an artist
  ask's disambiguation consulted (its closure) — are ever
  scheduled. `record()` is a pure local read and schedules
  nothing; subjects a text query merely resolved are bystanders
  and are not verified until an ask relies on them. Fetchers: MusicBrainz (public API, the node's
  configured pacing/user agent) and Deezer (public lookup, no
  auth); Spotify is an explicit not-configured stub (its
  credential route is an open sub-decision in the design).
  STRONG requires a cross-match where both sides come from
  provider responses — two providers agreeing on an ISRC, a
  MusicBrainz response linking to the very Deezer id in hand, a
  shared MBID — with titles/artists agreeing with the name
  claim; duration within ±2 s only ever supports, and a duration
  goes strong only riding a key-material cross-match. A single
  response's agreement is PLAUSIBLE (name+duration class) and
  never promotes. A fetched provider link (a link claim carrying
  a strong verdict) settles same-name-artist disambiguation with
  ONE album — but only when the album is one of the USER's: the
  link row is the user's own, or it sits on an album in the
  user's own library. A fetched link on a stranger's album
  proves the link exists, not that it is about this user's
  artist. `album_of` links earn that strong verdict from the
  album response itself: the response carries its own artist id
  (the Deezer album's artist object; the MusicBrainz release's
  first artist-credit), and a claim matching both ends of it was
  asserted by the provider. Unverified links settle display only, and only with
  ≥3 distinct local albums behind one candidate; a shared
  strong-verified MBID settles outright — "shared" meaning
  shared with the user's own non-`network` data, never merely
  agreed among network rows. Verification tries
  EVERY claimed id of the relevant kind until one
  identity-matches (the first id that merely resolves may be a
  namesake), and MusicBrainz recordings carry their FULL ISRC
  set — cross-matches and contradiction checks run against the
  set, so a claim asserting a genuine second ISRC of the same
  recording grades instead of failing. Track ids and ISRCs are
  SETS, and a set member the cross-matched pair does not carry
  is NOT a contradiction (a live/remaster/edit twin's true id
  is simply not this pair's): it earns no verdict and no failed
  check; only the recording MBID — canonical identity, not a
  set member — keeps the contradiction path. A tracklist variant
  naming a MusicBrainz release (`mb:<release_id>`) is graded
  against THAT release's tracklist ALONE, fetched by name — no
  fallback to the release-group pick or the Deezer edition,
  which are different pressings; variant-less claims grade
  against the album responses' tracklists. Named-variant claims
  fingerprint PER ASSERTING ORIGIN, exactly like variant-less
  ones: two origins asserting the same variant and agreeing
  both verify, and one origin's extra or wrong slot poisons
  only its own fingerprint. Verdicts live in
  `claim_verdicts`, keyed by claim identity, never by batch id.
  A verdict's `verified_at` is the EVIDENCE time — when the
  provider responses the grading rests on were fetched — not
  the grading moment, so the staleness window measures the age
  of the evidence. Verdict writes are monotonic: a lesser grade
  never overwrites a stronger verdict, a provider being
  unreachable changes nothing, and only a CONTRADICTION (the
  entity's identity established, the value different) clears a
  verdict, through the failed-check path. Fetcher etiquette:
  every response is cached 90 days in
  `fetch_cache`; first checks read through the cache, but the
  §6 reliance re-check and the §8 sanity re-check run FORCED
  past it — a re-check that replays cached responses could
  never overturn anything, and fresh verdicts computed from
  fresh responses can and do overturn stored ones. A composite
  album fetch whose tracklist call fails is NOT cached as a
  success (the fetch fails outright rather than pose as an
  album with no tracks); Deezer album tracklists paginate
  past 100 tracks, and when the album body still reports more
  tracks than were collected the payload records
  `tracks_truncated`. All of the node's MusicBrainz traffic —
  the §7 fetcher and the musicbrainz.py resolver alike — runs
  through ONE shared pacer, so the node's aggregate rate
  honors the ~1 req/s etiquette instead of doubling it per
  client;
  one provider's 404 is unreachable, not absence — absence is
  §7.1's rule, and it is implemented: a PREVIOUSLY VERIFIED id
  whose primary lookup is not-found, with a second independent
  (provider, method) path agreeing not-found and no response in
  the run asserting the value, loses its verdict and records a
  FAILED check for its origin (absence is evidence, recorded as
  such — without it a strong verdict for a deleted entity would
  stay strong forever, since verdicts exempt claims from the
  90-day expiry); another node's cached claims are never
  deleted over it. A claim the providers contradict (with
  the entity's identity established) clears its verdict and
  records a FAILED check for its origin. Verification itself
  is BOUNDED work: at most 200 network fetches per run and 40
  per subject (fetch-cache hits are free), scheduled
  best-evidence-first — subjects holding claims from standing
  origins first, then by the best §9 reputation among their
  asserting origins. A lookup that FAILS is negative-cached
  for one hour (in the verifier's memory — the fetch store
  holds responses only, per the honesty rule above), so a dark
  provider is not re-asked on every ask. Claims are read
  through §6's expiry predicate: an expired, unverdicted claim
  is NEVER verified (a verdict would resurrect it), and only
  fields §7 can grade at all (ids, duration, year, links,
  album_track slots) hold a subject in the verification queue —
  name, genres and display_name never receive verdicts and
  never schedule work. `checks_passed` — the sanity gate's
  currency — is credited on STRONG grades only: copying one
  public id and having it confirmed at the plausible level
  earns no trigger power.
- **Reputation (§9) is local and small**: per origin key, counts
  of §7 checks passed/failed (`reputation` table). It never
  gates support or admission. It decides display ordering among
  unverifiable values, cache eviction order under size pressure
  (the answer cache prunes to 2,000 batches, lowest-reputation
  origins' oldest first — and a pruned batch takes its verdicts
  with it ONLY when the claim identity is gone from every
  retained batch: verdicts are keyed by identity, so the same
  origin's re-minted batch keeps the verdict its predecessor
  earned), and the sanity-rule gate: a disputed
  field may trigger one re-verification per origin per field per
  24 h (`sanity_triggers`), and an origin with no passed check on
  record can never trigger one. The sanity rule itself is
  scoped to §8: it fires only for a field holding a prior
  STRONG-VERIFIED value, only against NETWORK claims
  challenging it (a dispute among unverified claims has no
  verified truth to check against, and a challenge to the
  LOCAL value alone is not the network's business), per
  challenging origin, inside a ROLLING 24 h window (not a
  calendar day — the day boundary allowed two triggers
  minutes apart across midnight); album_track variant
  conflicts can trigger it too.
- **Surface.** `POST /local/meta/query` (local API key) takes
  `{kind, mode: exact|text, subjects|terms, network?}` and
  returns the merged records — as the coarse depot projection
  above — plus `asked_network` and any `unresolved` entries.
  The admission tallies (how many answering batches were
  admitted or dropped) are exact counts and stay on the
  node-internal status surface only; they never ride this
  response. `GET
  /local/meta/record/<entry>` returns one subject's merged view
  from local data + cache, in the same projection — no relay
  traffic, ever; the entry
  may be a SoulID, a provider ref, or a legacy alias.
  `meta_status` gains a `querier` block: cache size, legacy
  alias count, revocation lists held (issuers, sequences, key
  counts), and the verification backlog (unverified cached
  claims, stale verdicts).
- **Per-pool attestation selection.** A node binds its key per
  pool, and the relay session runs on the FIRST joined pool (the
  one whose token the session exchanges with). The responder and
  builder therefore embed THAT pool's attestation; another
  pool's is never substituted. When the session pool has
  delivered none while another pool carries one, relay status
  says so — the node's answers count zero support until the
  session pool's own exchange delivers an attestation.
- **The `network` source.** The §6 source enum includes
  `network` (the §2.4 mark for depot-promoted values re-emitted
  by SoulSync). A `network`-sourced claim validates, displays,
  and can be strong-verified by this node's own checks — but it
  counts zero support however it travels, and the re-serve rule
  refuses it outright, bare ID links included: a promoted value
  must never launder back into network truth.
- **Known open edge — attestation window vs clock skew
  (design question, no code).** §9 specifies no clock-skew
  tolerance on the attestation window: a batch earns standing
  only when its `asserted_at` falls inside the attestation's
  `issued_at..expires`, with the receipt time capping the age
  side. The lower edge is the sharp one: an honest origin whose
  clock runs slow can keep asserting batches BEFORE its
  attestation's `issued_at`, and every one of them earns zero
  support until its clock or its next attestation catches up.
  The failure is one-directional — skew can only ever COST an
  origin support, never grant it — which is why it ships as a
  known edge rather than a hole; whether §9 should admit a
  small lower-edge tolerance (and what it would do to the
  window's meaning) is an open design question.

## 20. The enrichment view (node side)

What the node's operator can see of the merged record, and the
one place the full record is rendered whole
(DESIGN-METADATA.md §13 step 9; code in
`node/metasync_node/meta/enrichment.py`,
`NodeService.meta_enrichment`, the login-gated `GET
/enrichment` page).

- **The full record read, the coarse exposure rendered.**
  The service hands the presenter the full merged record,
  read from the querier directly — the presenter's gap
  classification is computed from the record's origin lists,
  so it needs the whole record internally. But §19's two
  rules bind step 9 as they bind step 8, and the page honors
  them: the `support_label` bucket is the only form of a
  count shown, and origin keys never reach the page — not on
  the SoulSync-facing routes, and not on the node's own page
  either. Per value, the page shows exactly what the coarse
  projection (`merge.project_for_depot`) exposes: the value,
  its tier, `verified_at` (and the stale flag), its local
  flag, its sources, and support as the bucket alone. Exact
  support counts, per-value report counts, origin keys and
  batch ids are stripped in the presenter
  (`enrichment._value_view`); what stripping cannot govern
  — the merge's own arithmetic showing through the results
  being rendered — is named as one cluster under
  **Merge-inherited orderings** below. The page's route
  builds the template context from those views plus a
  fixed set of
  lookup facts (ok/error, whether the network was asked,
  the unresolved entries, the backlog pair below, each
  counter as presence only) —
  the service's result dict, the full records, the own key
  string and the rest of the querier status never cross
  into the context at all (pinned by a
  `flask.template_rendered` test that walks the context).
  Per-slot support in the `album_track` variants is
  bucketed the same way. What the page adds beyond the
  projection is the node's own analysis of its own record —
  the gap classification, the `duration_ms` consensus and
  per-variant values, `year`'s held values (rendered in
  value order alone — the page shows no tier or count for
  them, so their order may carry nothing), the §8
  disambiguation block (settled candidate, via, promotion
  flag, survivors, settle time), the per-record verification
  tally, and the fact (never the count) that §6 aging
  dropped some network claims: the record's expired count
  is exactly other nodes' claims — the node's own never
  expire — so the page renders it as a boolean. It sits
  behind the node's admin login, on the operator's own
  machine, over data already in the node's local store, and
  serves nothing onward.
- **Two scopes, honestly different.** The form (kind,
  by-name or by-exact-ID, scope) picks one. Asking is never
  the default: a missing or unrecognized scope means "only
  what this node knows", because a bare `/enrichment?q=...`
  link must not set an ask in motion under the operator's
  session (the UI cookie is SameSite=Lax, so a cross-site
  top-level navigation carries it) — scope "ask the
  network" runs only when the operator picks it in the
  form. It runs the querier's existing ask
  paths — `query_text` / `query_exact`, the same cache-first
  fan-out SoulSync's asks use: the querier asks the relay for
  subjects local data cannot answer, answers may be ADMITTED
  into the cache, and verification of what the view relies on
  is scheduled (§19's reliance rule) — an ask-scope lookup is
  not side-effect-free, and the page's copy says so. Scope
  "only what this node knows" is a PURE READ of what the node
  already holds, and deliberately does not ride the ask paths
  (an exact ask schedules verification even with
  `network=False`; a text ask fires §8 sanity triggers):
  exact rides `Querier.record()` — own claims + answer-cache
  assembly that schedules nothing and never touches the
  relay — and text is built from the same parts (the
  responder's local name-index resolution, then `record()`'s
  assembly per subject). A local lookup admits nothing,
  fetches nothing, schedules nothing. Entries that resolve to
  nothing are listed as unresolved; a subject whose claims
  all expired renders its `found: false` shell.
- **Display-only, stated precisely.** The page is a GET form
  — there is no POST route to guard, the page itself writes
  nothing, and nothing on it promotes: promotion is the
  depot's business under §8's floors (§19, and SoulSync step
  8), never this page's. (What an ASK-scope lookup sets in
  motion is the ask's own machinery, above — not a write path
  of the page.) The presenter (`enrichment.record_view`) is a
  pure function of the record: it computes the page's one
  interpretation, the GAP classification — per field, whether
  the values are the library's alone, the network's alone (a
  gap enrichment could one day close), or both, read off the
  record's own local flags and origin lists (`album_track`'s
  locality is read from its slots, which carry no origins to
  split further) — plus two refinements so the summary cannot
  overclaim: a field whose only non-local assertions are this
  node's OWN key (its batches, re-served back) classifies as
  this node's, not the network's, and a `year` whose only
  network presence is §8-held values classifies "held" (held,
  not served) rather than as if the network had nothing. The
  per-record verification tally counts values and
  `album_track` slots alike and PARTITIONS them — fresh
  strong, fresh plausible, unverified, stale — so the four
  numbers add up to what was counted: a stale verdict,
  strong or plausible, value or slot, counts in "stale"
  alone, never also inside its tier's number, and the
  strong headline is fresh strong verdicts only. (Slots
  carry their §6 staleness through the merge for this; the
  depot projection builds its slot dicts key by key and
  does not pick the flag up.) A variant-less slot entry,
  which the merge duplicates under every asserting origin's
  shape key, counts once. Nothing
  computed here feeds back into the merge, the store, or the
  querier.
- **Verification state, two levels.** The tiles above a
  result are the verification half of the querier's §19
  status block at lookup time (the unverified backlog,
  stale verdicts), with §9 applied to both numbers: each
  tile renders PRESENCE only — "none" / "some" — never
  the count, and not the bucket either. An earlier draft
  of this section called the unverified queue "this
  node's own work queue"; it is mostly not. Newly admitted
  claims land in it unverdicted — verification runs on a
  background pool and has not finished when the page
  renders — so the exact number is mostly OTHER nodes'
  just-admitted claims, and its difference between two
  renders around an ask is exactly how many new
  origin-and-claim pairs the ask admitted: for a single
  field, a support count §9 hides. The bucket leaked
  too, at its lower boundary: a drained queue reads
  "none", an ask admits claims behind two values that
  each show bucket "1-2", and a queue then at "1-2"
  pins each value at exactly one origin. The
  stale-verdicts tile went exact in an earlier fold on
  the claim that admissions never move it, only time
  and re-checks do. That claim is false twice over. An
  ask on a record whose strong verdicts have gone stale
  schedules forced re-checks of that record, one per
  origin holding a stale verdict; when they land, the
  rows refresh (same-or-better) or clear
  (contradiction), and the count drops by exactly that
  many origins — separating 1 from 2 inside the "1-2"
  bucket and counting a "3+" outright. And admissions
  move it from the other side: admitting a batch prunes
  the answer cache, and an evicted batch takes its
  verdicts with it. The honest summary is that both
  counters move for reasons correlated with other
  nodes' claims — which is exactly why the page shows
  presence only: the operator can still see whether
  any backlog exists at all, which is all the tiles are
  for. The status block's cache-size
  counters (cached batches, cached claims) are
  deliberately NOT tiled: an exact global counter rendered
  beside a lookup hands back, by subtraction between two
  page loads, how many batches that lookup admitted — the
  per-lookup number the service keeps out of its own
  result. Per value, the tier and its evidence time render
  as recorded — with one projection discipline kept: a
  strong verdict gone stale wears the projection's demoted
  label (unverified, with the support label the demotion
  computes) beside its stale flag, the same words the
  depot surface uses, while the tally counts it as stale
  under its true tier. Tracklist slots demote the same way
  on this page (the demoted tier, its pill, the stale flag
  kept) — which the projection does NOT do, and the
  asymmetry is a named known inconsistency: the depot
  projection does not demote stale strong slots —
  `merge._project_field` serves such a slot to the depot
  as strong, and a variant's settled-ness counts it —
  while the view demotes it for display. Only the view
  changed; projection behavior is unchanged pending a
  depot decision on whether the depot surface should
  demote slots too.
- **Merge-inherited orderings — one cluster, ticketed,
  pending a merge-level decision.** Four disclosures
  share one root: the merge computes with EXACT support
  and report counts, and a surface rendering its results
  inherits that arithmetic unless it re-derives what it
  shows. None of the four is this view's own addition,
  and the view adds no exposure of its own beyond them;
  fixing any of them at the root means BUCKET-RANKED
  merge and projection — slot winners, value order and
  the consensus all change — which is a design decision
  above this branch, not a view fix. (a) VALUE ORDER:
  the merge orders a field's values by exact support
  (`_entry_sort_key`, kept by `apply_display_order`
  inside its classes), so of two same-tier values in
  bucket "1-2", the one rendered above a
  value-identity-earlier rival is exactly the 2. This
  page closes its own half: the presenter re-sorts by
  (tier, bucket, value identity) and takes its header
  name from a local value, else the first re-sorted
  name. `project_for_depot` still serves the merge's
  order to SoulSync, so the depot surface carries the
  disclosure until the merge-level decision. (b) SLOT
  WINNERS: `_merge_album_track` ranks a slot's
  candidates by (tier, exact support), then lowest
  track id, and only the winner is rendered — the view
  cannot re-rank a loser it never receives. A winner
  whose track id is HIGHER than a same-tier,
  same-bucket rival's (visible under another variant,
  or on an earlier render) discloses support(winner) >
  support(rival): inside "1-2", exactly 2 against 1.
  (c) SHAPE FAN-OUT: each origin contributes exactly
  one shape key, and a variant-less slot entry is filed
  under the shape of EVERY origin asserting it — an
  entry appearing under k distinct `shape:` variants
  has at least k asserting origins, so k = 2 inside
  bucket "1-2" pins support at exactly 2, modulo the
  own-key, standing and network-source caveats the page
  cannot rule out (slots carry no own flag). (d)
  DURATION CONSENSUS: the consensus is a median over
  REPORTS — only strong values count at all, each
  repeated once per distinct origin holding a strong
  verdict for it — and reports include this node's own
  key and origins without standing, which support
  excludes, so the split disclosed is of reports, not
  of support. The leak, precisely: a tie renders the
  midpoint (284500 for 1:1 or 2:2 alike), while a
  consensus picking one value means its report count is
  strictly greater than its rival's; `by_variant` adds
  one more median — one more equation — per variant.
  (This replaces the earlier, looser wording of this
  item — "strong reports heavier", "two reports against
  one".) SoulSync's depot surface serves the same
  consensus and the same per-variant values.
- **A timing-only residual.** `verified_at` renders to
  the minute. Polling local renders while a
  verification drain lands — verdict stamps advancing
  across minute boundaries, the stale tile flipping to
  "none" — gives a rough lower bound on how many
  origins were rechecked in that window. It is not
  exact and recovers no count; it is named here so the
  bound is a known one.
- **Provenance shape.** Live own-library rows carry no origin
  in the merge (origin is the asserting batch's signer; the
  library itself is not one), so a purely local value's
  provenance reads "your library" plus its sources. Values
  the answer cache asserts show bucket and sources only —
  whose batches asserted them is the record's internal
  business, never the page's. The one exception is a boolean:
  a value whose only non-local assertions are this node's
  OWN key (its batches, re-served back) is marked "this node
  — re-served", the operator's own identity as a flag; no key
  string, not even our own, appears on the page (such values
  count in origins internally, never in support — §19).
- **Escaping.** Every string the page renders passes through
  the template engine's autoescaping; crowd values are data
  and render as text, never markup (pinned by a test whose
  network genre value is a script tag).
- **Store-free landing.** On a node whose claims store was
  never created, the page says so and creates nothing:
  `meta_enrichment` returns the empty marker without opening
  the store, and the page's chrome checks existence without
  touching it — the §19 `meta_status` doctrine, applied to a
  browser surface.

## 21. Known issue — provider-index resolution without standing (named, not fixed here)

- **A planted provider link can steer local resolution.** A
  provider ref (`deezer:X`) resolves locally through the provider
  index, and admission writes index links for EVERY admitted
  batch — including batches with no standing at all. When a ref
  resolves locally through such a link, the ask goes to the
  network carrying the resolved SoulID and never the ref itself,
  so the ref's real owner is never asked: the question was
  silently redirected by a link nobody vouched for. The claims
  behind the planted link still count only their own support and
  nothing promotes on them, but display and resolution follow
  the link. This is PRE-EXISTING — it predates early close, and
  early close adds nothing to it (the §19 matching-value rule
  guards only refs that actually ride verbatim to the network) —
  and it affects every ask, not just closable ones. It is
  ticketed for its own change: whether a link may resolve locally
  only with standing (or some support) behind it, or whether a
  locally resolved ref should ALSO ride verbatim so its owner is
  asked, is a resolution-semantics decision, deliberately not
  folded into the early-close work.

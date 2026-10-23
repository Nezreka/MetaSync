# MetaSync Node — Design

Status: MVP built (Python, Flask UI, mocked tests green). Not deployed, not
run against any live SoulSync or pool. The SIN central backend this node
talks to is live (see README.md); one backend change is still required
before user nodes can pull playlist data — it is spec'd in §9 and flagged
there as a follow-on, deliberately not implemented in this pass.

MetaSync is a Python program, like SoulSync. It runs bare-metal
(`python3 run.py`, in the spirit of SoulSync's `dev.py`) or as a Docker
image, next to the user's SoulSync. Running it is the opt-in: no
SoulSync-core changes, and the node talks to SoulSync **only** through
API v1 — it never opens SoulSync's database.

## 1. The two connections (kept distinct everywhere)

- **Step A — connect to YOUR SoulSync.** SoulSync URL + a SoulSync `sk_`
  API key (created in SoulSync under Settings → API keys). The UI tests
  the connection with a read-only watchlist call. This alone joins
  **nothing**: with no pool configured the node computes previews and
  stays local.
- **Step B — join a POOL (optional, separate).** Pool URL + pool API key.
  A pool is any SIN-compatible backend; SIN is one pool. Joining enables
  push/pull for that pool only. Multiple pools can be joined; each has
  its own role and enabled flag.
  **[Amendment, Oct 22, 2026: for SIN, Boulder's primary flow in
  `PORTAL-DESIGN.md` §3A.5 supersedes pasting the key into this node's
  Pools page — the user enters the key in SoulSync, SoulSync hands it
  to the node, and the node exchanges it under §3A.4. The Pools
  page becomes status/display plus fallback.]**
  **[Amendment, Oct 22, 2026 (later, Boulder's simplification): local
  SoulSync ↔ MetaSync auth is the two keys, nothing else. This node
  reads SoulSync with a SoulSync `sk_` key (step A — unchanged, and
  how the built node already works). The reverse direction — SoulSync
  handing this node the pool key and reading its status — uses this
  node's OWN local API key: shown once in the node's UI, pasted by
  the user into SoulSync's MetaSync settings, presented as a bearer
  token, regenerable/revocable in the node's UI. The earlier
  pairing-ceremony design for this direction (one-time code,
  fingerprint comparison, shared pairing secret, HMAC-stamped local
  calls) is dropped — see `PORTAL-DESIGN.md` §3A.5 as amended.]**

Both connections live side by side — but separately — in the node's
settings (`settings.json` in its own state dir): `soulsync: {url,
api_key}` and `pools: [{id, name, url, api_key, role, enabled}]`.

## 2. Permission model (Boulder's rule)

- **User nodes** (pool role `node`) may ONLY:
  - `POST /v1/report` — push their own weekly signal report;
  - `GET /v1/index/*` — pull the published index;
  - `GET /v1/playlists` — pull the pool's canonical playlist data.
  They then materialize/update **local copies** of those playlists inside
  their own SoulSync. They never define, edit, or publish shared playlist
  data, and they never trigger aggregation.
- **The official MetaSync** (Boulder's) runs the same codebase with the
  pool joined at role `admin`. Only it may `POST /v1/aggregate` and
  `POST /v1/playlists` (publish canonical playlist definitions).
  Playlist *definition building* — index MBIDs → resolved track lists
  with titles/artists via MusicBrainz — happens only in this admin flow,
  so a thousand user nodes do not each re-derive (and diverge on) what
  "Trending in SoulSync" contains.

Enforcement on the node's side is structural: `PoolClient` refuses admin
operations on a node-role pool with `PermissionError` **before any HTTP
request is made** (unit-tested). The matching backend enforcement is the
key split spec'd in §9.

## 3. End to end

```
                 ┌────────────────────────── MetaSync node ───────────────────────────┐
                 │                                                                      │
 SoulSync ──API v1──► PULL: watchlist / wishlist / library                              │
 (user's)        │        compute 4 binary weekly signals (signals.py)                 │
                 │        payload preview in UI (/preview), DRY_RUN default            │
                 │ PUSH: POST /v1/report ──────────────► POOL (e.g. SIN)               │
                 │ PULL: GET /v1/playlists (canonical, admin-published)                  │
                 │        or, admin role: GET index → MusicBrainz → POST /v1/playlists │
                 │ PROVIDE: POST /api/v1/playlists/<minted id>/sync                    │
                 │        (sync_mode "replace") ──► back into the user's SoulSync        │
                 │ UPDATE: same minted id every refresh; unchanged content skipped     │
                 └──────────────────────────────────────────────────────────────────────┘
```

**PULL.** `GET /api/v1/watchlist`, `/api/v1/wishlist`, `/api/v1/library/albums`
(envelope `{success, data, error, pagination}`; wishlist/albums paginated,
followed to the end). Shapes verified against SoulSync dev head
`bd8404875ae42caa6c61a510203d823ce0ec91e0` (Oct 22, 2026, GitHub API) —
serializers are quoted in `metasync_node/soulsync.py`.

**Signals** (binary per node per item per week; deduped in-computation
and again server-side by the (week, token, signal, mbid) unique index):
- `watchlist_add` — artist MBID added to the watchlist this ISO week.
- `wishlist_add` — release-group MBID wishlisted this ISO week.
- `import_keep` — release-group whose album entered the library 14+ days
  ago and is still in `/library/albums` at computation time.
- `hunt_demand` — release-group wishlisted 7+ days ago and still on the
  wishlist (still listed = no successful download yet; see GAP-4).

**PUSH.** One report per enabled pool: `{week, token, signals}` with the
pool key in `X-SIN-Key`. The token rotates weekly (§5), so the same
payload shape works for every pool.

**PROVIDE.** *[Superseded Oct 23, 2026; see §13.]* The node used to
sync each canonical playlist into SoulSync. It now only caches them and
offers them on `GET /local/charts`; SoulSync pulls, matches and shows
them, and nothing syncs or downloads until a person presses the button.

**Global playlists** (names are part of the canonical definition):
- `trending` → **SIN · Trending in SoulSync**
- `most-wanted` → **SIN · Most Wanted**
- `breakouts` → **SIN · Breakout Artists** — breakouts are *artist*
  MBIDs. In the admin build they become a playlist by sampling up to
  `tracks_per_item` (default 3) tracks from each breakout artist's
  newest Album release-group: a breakout is an artist rail, so its
  playlist is a sampler, not the artist's catalogue. Trending items are
  release-group MBIDs, except an artist MBID can appear there too (the
  aggregator sums `watchlist_add` into Trending); those resolve as
  artists the same way.

## 4. The UI (own port, own password)

Flask, server-rendered templates, no build step, minimal JS (none
required). Default port **8790** (`METASYNC_PORT`) — SoulSync uses 8000,
so nothing collides. Pages:

- `/` dashboard — node fingerprint, week, dry-run state, connections,
  pools, last report/refresh, playlists managed, recent errors; buttons
  to run the report / refresh now and to toggle dry run.
- `/settings/soulsync` — step A form + **Test connection**.
- `/pools` — step B: list/add/enable/disable/remove pools, role select
  (node vs admin, with the official-only warning).
- `/preview` — the **exact** weekly report payload per pool, pretty
  printed, with signal diagnostics and gap counts.
- `/setup`, `/login` — first run sets an admin password (Werkzeug hash,
  stored in the settings file); every page except `/health` then
  requires login. `/health` stays public for Docker healthchecks. This
  is deliberately simple local setup protection: one password, session
  cookie, no accounts, no recovery flow (delete `settings.json`'s
  `ui_password_hash` on the host to reset).

## 5. Identity and privacy

- First run generates a random 32-byte node key in the node's own state
  dir (`node_key`, mode 0600). It never leaves the machine.
- Weekly token = `sha256("<node_key>:<week>")` hex. Unlinkable across
  weeks; the pool dedupes within a week on it.
- **Access-rule amendment, Oct 22, 2026:** the persistent `node_key`
  is also the root for the SIN request-signing identity specified in
  `PORTAL-DESIGN.md` §3A.2. The MVP described here does not yet derive
  or expose a signature-verifying public key — its 12-character
  fingerprint is a status display derived from the secret, not a
  public key — so signing-key derivation and registration are an
  explicit node build delta in §3A.9, not existing behaviour.
- A report contains only `{type, mbid}` pairs: no titles, names, play
  counts, listening history, file paths, or SoulSync identity. The
  preview page shows the literal payload before anything is sent.
- **DRY_RUN is the default** (settings + `METASYNC_DRY_RUN`). In dry run
  the node computes and stores previews but sends nothing and syncs
  nothing. A fresh node therefore cannot leak or mutate anything until
  its operator has seen a preview and turned dry run off.
- Pool raw reports expire after 24h server-side (backend behaviour);
  aggregates require k=15 distinct nodes before anything is published.

## 6. Scheduling

Defaults: **weekly report** (the scheduler notices a new ISO week and
reports once per pool) and **daily refresh** (every 24h). The serve
command runs a 60-second tick loop in a background thread alongside the
UI; each leg runs in isolation (see §8). One-shot CLI equivalents:
`python3 run.py report|refresh|preview`.

## 7. State

Node-owned directory (`METASYNC_STATE_DIR`, `/data` in Docker):
- `node_key` — identity (0600).
- `settings.json` — the two connections, dry-run flag, UI password hash
  (0600).
- `state.json` — last report per pool, last preview per pool, index and
  canonical-playlist caches, managed playlist ids + content hashes,
  MusicBrainz caches (response JSON + release→release-group map),
  recent errors (0600, written via temp-file + rename).
- `ui_secret` — Flask session signing key, generated once (0600).

Index staleness rule from the sketch carries over: cached pool data
older than 7 days is stale; a stale cache is never pushed into SoulSync
as if it were new.

## 8. Failure isolation

- SoulSync unreachable / pool unreachable / MusicBrainz down: the
  affected leg records `{at, leg, message}` in state, returns an outcome
  object, and the process stays up; the UI keeps serving.
- One playlist's sync failing (e.g. SoulSync answers 409 "sync already
  in progress") does not fail the other playlists; it retries on the
  next refresh with its previous content hash intact.
- A corrupt `state.json`/`settings.json` resets that document instead of
  crashing the node. Nothing the node does can take SoulSync down: its
  writes to SoulSync are the standard playlist sync endpoint SoulSync
  already exposes to the Companion.

## 9. Required backend follow-on (spec — NOT implemented here)

The live SIN backend predates the permission model: a single
`SIN_INGEST_KEY` gates both `POST /v1/report` and `POST /v1/aggregate`,
and there are no playlist endpoints. Required changes in `src/sin`
(single follow-on PR, with tests):

1. **Key split.** New env `SIN_ADMIN_KEY`. `POST /v1/report` accepts the
   node key (`SIN_INGEST_KEY`) or the admin key; `POST /v1/aggregate`
   and `POST /v1/playlists` accept **only** the admin key (401
   otherwise, timing-safe compare like the current check). Index and
   playlist GETs stay public. **[Amendment, Oct 22, 2026: the public-GET
   part is superseded for the MetaSync-only target by
   `PORTAL-DESIGN.md` §3A.7 — index, playlist and whoami reads require
   the exchange-issued token plus the bound node's signature.]**
2. **`GET /v1/playlists`** → `{week, playlists: [{key, name, tracks}]}`
   for the latest published week (empty list before the first publish).
3. **`POST /v1/playlists`** (admin) with that same body; validate with
   the existing strict style (known keys only; `key` in
   trending|most-wanted|breakouts; tracks capped, e.g. 500; track fields
   `id`, `name`, `artists[{name}]`, `album{name, images[{url}]}`,
   `duration_ms`, `image_url`).
4. **Storage.** New table `canonical_playlists (week text, key text,
   name text, tracks jsonb, published_at timestamptz, PK (week, key))`
   + one migration; GET reads the max week. Reuse
   `index_publications`-style logging if wanted.
5. **Tests.** Existing 22 must keep passing; add: node key rejected on
   aggregate/publish, admin key accepted, publish→pull round-trip,
   validation failures.

Until this lands, a user node's refresh honestly reports
`playlist_data_unavailable` and syncs nothing (implemented and tested in
the node), while the admin node can still build definitions and will
publish the moment the endpoint exists.

## 10. API gaps found in SoulSync v1 (honest list)

- **GAP-1 (wishlist MBID):** `serialize_wishlist_track` exposes no
  MusicBrainz field. A wishlist row only contributes
  `wishlist_add`/`hunt_demand` when its raw `track_data` happens to
  carry an explicit release-group MBID (`musicbrainz_release_group_id`
  and two aliases). Spotify-shaped `track_data` — the common case — has
  none, so those rows are skipped and counted in diagnostics. Fix
  belongs in SoulSync core (expose a release-group MBID on wishlist
  rows); the node must not invent one.
- **GAP-2 (album id kind):** library albums expose
  `musicbrainz_release_id` (a *release* MBID) while SIN keys album
  signals by *release-group* MBID. The node bridges this with
  MusicBrainz's deterministic `release/{id}?inc=release-groups` lookup
  (cached forever). Missing/unresolvable ids are skipped and counted.
- **GAP-3 (import date):** no `imported_at`; `import_keep` uses album
  `created_at` (row creation = import proxy; the recently-added
  endpoint orders by it too).
- **GAP-4 (download success):** no per-item flag; `hunt_demand` uses
  "still on the wishlist" as the not-yet-fulfilled proxy.
- **GAP-5 (playlist readback):** `GET /api/v1/playlists[/<id>]` lists
  Spotify/Tidal *source* playlists only, not the server playlists a sync
  creates — so sync-start is fire-and-forget (200 = started, not
  finished) and the node tracks the ids it manages itself. A 409 on a
  later sync is how "still running" surfaces.

## 11. Deliberately NOT in the MVP

- No metadata pooling / SoulID work (the other MetaSync module; the
  node codebase leaves room for it — same chassis, separate package).
- No Discover rails inside SoulSync core; global playlists are the
  node's only provide-back surface.
- No backend changes (the §9 key split + playlist endpoints are a
  spec'd follow-on, not silently bundled).
- No multi-user UI accounts, no password recovery, no HTTPS termination
  (put it behind your reverse proxy if you expose it at all; it is a
  local sidecar UI).
- No signing of reports in the original MVP (the weekly token is the
  identity the pool needs; the node keypair's signing role was
  originally left to the metadata module). **Amendment, Oct 22, 2026:
  superseded for SIN pool access by Boulder's MetaSync-only rule —
  request signing is now in SIN scope. See `PORTAL-DESIGN.md` §3A,
  especially §3A.2 (Layer 1 signing), §3A.4 (the key exchange), and
  §3A.9 (the node build delta).**
- No editing of canonical playlist contents by user nodes — by design
  (§2), not omission.

## 12. Reporting a closed week (review amendment, Oct 23, 2026)

The MVP's scheduler sent the report the minute a new ISO week began, and
computed it **for that new week**. Every "this week" signal
(`watchlist_add`, `wishlist_add`, `recent_add`, `download_grab`)
therefore covered about a minute and was almost always empty. Only the
standing signals carried any data. Meanwhile `import_keep` fired every
week for every album older than 14 days, so each report was close to a
full listing of the library's release groups. That contradicted §5
("a report contains only {type, mbid} pairs") in spirit, if not in
form.

As built now (`signals.py`, `service.py`, `run.py`):

- **The report covers the last full ISO week**, `[Monday 00:00 UTC,
  next Monday 00:00 UTC)`. Event signals are events inside it. The age
  rules (7 days for `hunt_demand`, 14 for `import_keep` and churn) are
  measured at the week's end. The library itself is read when the
  report runs. Download and listening activity are read with
  `since`/`until` bounded to the week.
- **A report describes what happened, never what the library is.**
  `import_keep` fires once, in the week an album crosses its 14-day
  mark. Churn fires once, in the week of the second absence.
- **Bulk operations are not taste.** More than 500 events of one type
  in a week (a library import, a wishlist migration, a drive coming
  back online) suppresses that type for the week, and the Payload
  preview says so. Standing `hunt_demand` is a real state, so a big
  wishlist is sampled to 500 rather than dropped. The sample is a hash
  of (week, mbid): deterministic within a week, rotating across weeks.
  The pool refuses more than 1,000 of a type in any case.
- **Churn snapshots are weekly.** One complete library snapshot per
  reported week, three kept. "Absent from two consecutive reads" now
  means two weeks, however often the report is re-run. Before this it
  rotated on every run, so two runs a minute apart could fire a false
  drop signal.
- **Once per week, on a per-node slot.** The scheduler runs when a
  pool has no report for the closed week. It waits until this node's
  fixed slot (0–6 h into Monday, derived from the node secret: it
  spreads load and keeps arrival times from lining up). After a failed
  send it waits 6 h (`REPORT_RETRY_INTERVAL_H`), where it used to
  re-read the whole library every 60-second tick. A dry-run node
  previews each closed week once. A run lock keeps the UI job and the
  scheduler from overlapping.
- **A quiet week sends nothing** (a pool refuses an empty report) and
  still counts as done.
- **One report per week, accepted once.** The pool counts a node's
  first report for a week and acknowledges a repeat as `duplicate`
  without counting it. The node shows that as `already_sent`.
- **Upgrade path.** A node upgraded from start-of-week reporting has a
  `last_report` naming the following week. "Last report is this week or
  later" counts as done, so no week is ever re-sent and double-counted.
  The cost is one under-reported week at the switch.
- **Transport.** A pool URL must be HTTPS unless it is on loopback
  (`METASYNC_ALLOW_INSECURE_POOLS` overrides, for development). The
  legacy direct-key fallback (§1, `pool.py`) applies only to a
  loopback pool: any other pool that 404s the exchange is an error,
  never a reason to start sending the raw key.

## 13. Charts are offered, never pushed (Oct 23, 2026)

The node used to deliver each chart by calling SoulSync's playlist sync
(`POST /api/v1/playlists/<id>/sync`). That sync builds a media-server
playlist from the tracks the user already owns, and puts every missing
track on the wishlist. SoulSync's default **Auto-Process Wishlist**
automation then downloads the wishlist every 30 minutes. Every node
would have started downloading up to 25 albums × 5 tracks per chart, per
week, with nobody asking. The automatic wishlist adds would also have
come back to SIN as `wishlist_add` signals, inflating the very albums
that charted. No chart had published yet, so nothing was ever downloaded
this way.

As built now:

- The node caches the pool's charts and serves them on
  `GET /local/charts` (local key). It has no code path that writes to
  SoulSync: `SoulSyncClient.sync_playlist` is gone.
- SoulSync pulls them with its daily **"Refresh MetaSync Charts"** system
  automation, matches them in the background, and offers the ready ones
  on its dashboard and Discover page (SoulSync `core/metasync_charts.py`).
- A person opens one, gets it as their own playlist with the matches
  already in it, and presses Sync & download if they want it. That is the
  only path to a download, and only a deliberate wishlist add ever
  becomes a signal.

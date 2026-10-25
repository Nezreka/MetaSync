# MetaSync

MetaSync connects SoulSync installs to shared networks called **pools**.
Its first network is **SIN — the SoulSync Intelligence Network**: an opt-in
network where SoulSync installs send small, anonymous taste signals once a
week, and get shared playlists back — Trending, Most Wanted, Breakout
Artists, and more.

SIN exists to enrich SoulSync itself. No data is sold, and nothing about
your library ever leaves your machine except the anonymous counts
described below.

> **Status: public beta (October 2026).** The SIN backend and the portal
> are live, and the first nodes are reporting. A chart only publishes once
> at least **15 different installs** report the same item in a week, so
> right now the network is gathering its founding nodes. Early testers are
> exactly what it needs.

## The pieces

| Piece | What it is |
|---|---|
| [`node/`](node/) | **MetaSync itself** — the program you run next to your SoulSync (Python, own web UI on port 8790, Docker or bare metal). It reads your SoulSync locally, computes the weekly signals, sends them to the pools you joined, and offers the network's charts to your SoulSync, which shows them as playlists you can open. |
| [`src/sin`](src/sin) | The SIN pool server (the backend behind the portal's pool URL). Takes reports, folds them into counts, and builds the charts. |
| [`portal/`](portal/) | The website — [metasync.ssync.net](https://metasync.ssync.net) — where you sign in, create your pool key, and approve your node. |
| [`src/metasync`](src/metasync) | A second, future network (a shared *metadata* service keyed by SoulID). Scaffold only — not part of the beta, and not used by SIN. |

## Join the beta

**1. Get a pool key.** Sign in at
[metasync.ssync.net](https://metasync.ssync.net) and create a key. (GitHub
sign-in works for everyone; Google sign-in is still limited while its
consent screen is in testing.) The key is shown **once** — copy it
somewhere safe. The portal only ever stores a hash of it.

**2. Run a node.** Docker is the easy way — the image is published on
Docker Hub (and mirrored on GHCR):

```bash
docker run -d --name metasync \
  -p 8790:8790 \
  -v metasync_data:/data \
  boulderbadgedad/metasync:latest
```

The `/data` volume holds your node's identity key, settings, and caches —
keep it. If it is ever lost, the node gets a new identity and the portal
asks you to approve it again. To run MetaSync in the same compose file as
your SoulSync, see
[`node/docker-compose.example.yml`](node/docker-compose.example.yml).
The GHCR mirror is `ghcr.io/nezreka/metasync:latest` — the same image.

Bare metal:

```bash
cd node
pip install -r requirements.txt
METASYNC_STATE_DIR=./data python3 run.py
```

Open http://localhost:8790 and set an admin password on first run.

**3. Connect MetaSync and SoulSync.** In SoulSync: **Settings → Advanced
→ MetaSync → Connect**. SoulSync finds the node and shows a pairing code;
approve it on the MetaSync dashboard and the two swap keys automatically.
Typing the keys by hand works too, in both directions. Connecting joins
*nothing* — it just lets the two programs talk.

**4. Join the pool.** Paste your pool key into SoulSync's MetaSync section
(SoulSync hands it to your node and never contacts the pool itself), or
add the pool on the node's Pools page. The first time your node presents
the key it lands in **waiting for approval** — go back to the portal and
approve it, checking the fingerprint matches the one on your node
dashboard. That step is deliberate: only a node *you* approved can ever
use your key, and revoking the key or unbinding the node cuts access on
the very next request.

**5. Send a report.** The node starts in **Dry run**: it computes the
exact weekly report and shows it on the Payload preview page, but sends
nothing. When you're happy, turn Dry run off and run it — the dashboard
shows live progress. After that, the built-in scheduler sends one report
per week on its own.

One honest heads-up for very large libraries: signals are keyed by
MusicBrainz release *groups*, and SoulSync stores release (edition) IDs,
so the node looks up each edition's group on MusicBrainz at about one per
second. It does at most **5,000 fresh lookups per run** and caches every
answer forever — a typical library finishes in its first run, and even a
~71,000-album library fully warms up over its first runs instead of
needing attention.

## What SIN collects

Eight signals, computed on your machine, keyed **only by MusicBrainz
IDs** — one shared ID per album family, identical across every install
and every music source. Each weekly report covers the **last full week**
and lists that week's events, never your library:

| Signal | Meaning | Key |
|---|---|---|
| `watchlist_add` | You started watching this artist this week | artist MBID |
| `wishlist_add` | You wishlisted this album this week | release-group MBID |
| `hunt_demand` | Wishlisted 7+ days ago, tried by SoulSync, still not downloaded, and not already in your library | release-group MBID |
| `import_keep` | Reached 14 days in your library this week (sent once per album) | release-group MBID |
| `recent_add` | Landed in your library this week | release-group MBID |
| `download_grab` | Re-grabbed, upgraded, or gap-filled an album you already own this week (new arrivals are `recent_add`'s job) | release-group MBID |
| `download_churn` | **Optional, off by default.** Downloaded 14+ days ago, then removed from your library — confirmed by two weekly reads in a row | release-group MBID |
| `listen_album` | **Optional, off by default.** You played 3+ different tracks from this album this week (at most your 50 most-played albums; only SoulSync's shared listening history, never a profile that keeps its own) | release-group MBID |

A week with more than 500 of one kind of event (a bulk import, a
migration) is held back rather than reported, so a report is never a
listing of your library or wishlist.

**Never collected, never sent:** listening history, play counts or play
times (the optional `listen_album` is one yes/no per album per week),
track titles or artist/album names as text, file paths, library listings or
library size, usernames, emails, IP-based identity, or your SoulSync URL.

### The privacy rules

- **Binary contributions.** Per item, per week, your node reports 1 or
  nothing. A count means "this many distinct installs", full stop.
- **Rotating dedupe token.** Each report carries `hash(node secret +
  week)`, used only to dedupe a double-submit within that week. Next
  week's token is unlinkable to this week's — there is no cross-week
  tracking.
- **k = 15 publish threshold.** An item appears in a chart only if at
  least 15 distinct nodes reported it. Below that, the data point does
  not exist publicly.
- **Reports are counted on arrival, never stored.** The server adds +1
  per item to a running tally and keeps only `(week, signal, MBID,
  node_count)`; the weekly token is kept apart, only to accept one
  report per install per week.
- **Keys are hashes.** The portal stores SHA-256 hashes of keys, never
  the keys themselves. Your node signs its requests with its own Ed25519
  key; the pool trades your pool key for a 24-hour access token at
  exchange time, so the key itself is almost never sent — and it works
  only from the one node you approved, never presented directly.
- **Users can never publish.** Nodes only push their own signals and pull
  data. The shared charts are composed and published by the pool server
  itself — no node can publish.

## What you get back

Every week the server composes charts from the counts: **Trending in
SoulSync**, **Most Wanted**, **Breakout Artists**, **Loved & Kept**,
**Just Landed**, **Rising**, **On Repeat** (from the optional listening
signal), plus genre and decade editions when enough
charted albums share a tag. The shared work happens once, on the server:
only charted albums get looked up on MusicBrainz (track lists, genre,
year), and each contributes up to its 5 most popular tracks, ranked by
ListenBrainz's public listening statistics — SIN itself never collects
track data.
Everything is cached for everyone. While the network is young, Trending
and Just Landed fill their empty slots from ListenBrainz's public weekly
charts. The playlist name says so ("Trending · from ListenBrainz"), and
network data replaces public data automatically as installs report. Playlists are 50 tracks, led by every album's best song, and come in
genre and decade editions when enough charted albums share a tag. There
is also **Top Tracks · from ListenBrainz**, ListenBrainz's public weekly
song chart with at most 2 songs per artist.

Your node pulls the finished charts, and SoulSync fetches them from your
node once a day, matches every track in the background, and shows the
ready ones as **SoulSync Charts** on its dashboard and Discover page.
Opening one gives you that playlist, already matched. **Nothing is synced
or downloaded until you press Sync & download** on it.

## How a report flows

1. Early each Monday (UTC), the node reads your watchlist, wishlist,
   library, and last week's download (and, if you opted in, listening)
   counts from your SoulSync's API — locally, on your machine.
2. It computes last week's signals, bridging edition IDs to
   release-group IDs via MusicBrainz where needed (cached permanently).
3. It exchanges your pool key + a node signature for a 24-hour token,
   then sends one signed report per joined pool.
4. The pool validates the report and counts it on arrival — one +1 per
   item, one report per install per week; nothing per install is stored.
5. Every day a scheduled job runs the composer, which turns counts into
   charts (only items at or above k = 15) and publishes them for nodes
   to pull, then checks the network's vital signs. If there are no
   charts, or reports stopped (none this week after some the week
   before), the job fails and GitHub emails the owner.

## SIN backend API (for the curious)

Pool URL is shown on your portal dashboard after sign-in.

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | public | `{ ok, service: "sin", version, build: { commit, dirty, source, deployedAt } }`: the exact public commit the live server runs, set from git at deploy |
| `GET /v1/auth/public-key` | public | The pool's token-signing public key, so a node can verify its access token |
| `POST /v1/auth/exchange` | pool key + node signature | Trades key + signature for a 24h token; unbound keys get `202 binding_pending` and no token |
| `POST /v1/report` | token + signature (or the operator's env key) | One weekly report: `{ week, token, signals: [{ type, mbid }], churn_optin? }` (`churn_optin: true` only from a node sharing drop signals; counted per week, never stored per node) for the closed or current week, once per token — max 5,000 signals (1,000 per type, 100 `listen_album`) / 1 MB, strict validation, unknown fields rejected |
| `GET /v1/whoami` | token or env key | Connection test for a node |
| `GET /v1/index/trending` · `most-wanted` · `breakouts` | token or env key | The public chart indexes for a week |
| `GET /v1/playlists` | token or env key | The latest published charts as full track lists |
| `POST /v1/aggregate` | admin only | Run one aggregation pass |
| `POST /v1/compose` | admin only | Build and publish the closed week's charts (budget-resumable); runs daily from `.github/workflows/compose.yml` |
| `GET /v1/admin/status` | admin key only | The closed and current week's report counts and the closed week's chart keys, for the daily job's health check |

## Development

GitHub Actions runs all three suites on every push and pull request
(`.github/workflows/tests.yml`). Docker images publish from
`.github/workflows/docker-publish.yml`, mirroring SoulSync: every push
to main builds `:latest`, and a manual dispatch with a version tag cuts
a numbered release — both to Docker Hub (`boulderbadgedad/metasync`)
and GHCR (`ghcr.io/nezreka/metasync`), for amd64 and arm64.

```bash
npm run build   # tsc --noEmit (strict) over the backend + tests
npm test        # backend tests — pure logic + routes, no live DB needed

cd node
python3 -m unittest discover -s tests -v   # node tests, all on localhost mocks
```

Design docs: [DESIGN-NODE.md](DESIGN-NODE.md) (the node),
[PORTAL-DESIGN.md](PORTAL-DESIGN.md) (portal, keys, and the key
exchange), [PORTAL-EXPLAINED.md](PORTAL-EXPLAINED.md) (the plain-English
version), [DESIGN-DOWNLOAD-SIGNALS.md](DESIGN-DOWNLOAD-SIGNALS.md) and
[DESIGN-LISTEN-SIGNALS.md](DESIGN-LISTEN-SIGNALS.md) (the optional
signals), [DATA.md](DATA.md) (the data map: exactly what data exists,
where it flows, and how it is used).

---

MetaSync is the companion app for [SoulSync](https://github.com/Nezreka/SoulSync).
Installing MetaSync is the opt-in; connecting it to SoulSync joins
nothing — joining a pool is always a separate, deliberate step that takes
both a pool URL and a pool key.

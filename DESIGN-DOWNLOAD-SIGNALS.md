# Download-based signals for SIN — design

Status: **built, Oct 23, 2026.** Commissioned by Boulder after the
extra-data inventory of SoulSync's database. Boulder adopted the §7/§10
recommendations ("do what you think is best") and the build followed
§8: SIN backend (migration 0007, live), the SoulSync endpoint (PR
#1848, awaiting Boulder's merge), and the node. §4/§5 below describe
the contract as built; two build-time refinements are recorded there
(failed attempts deferred — they cannot be keyed to albums; churn
state is the node's own accumulated download history).

## 1. Why

The five current signals describe *wanting* and *having*: watchlist and
wishlist adds, hunt demand, library keeps, recent arrivals. SoulSync
also records **acquisition** — every track it downloads — and acquisition
carries two facts the current signals can't express:

- **Heat with proof — the slice the library can't see.** A download
  that lands in the library is already counted: it fires `recent_add`
  the week it arrives and `import_keep` after. What no current signal
  sees is acquisition aimed at albums **already owned** — re-grabs,
  quality upgrades, gap-fills from playlist syncs — and grabs that
  complete but never land in the library snapshot at all. When a
  remaster drops and fifty installs re-grab an album they have owned
  for years, that is real heat, and today it is invisible.
- **Churn.** Downloaded, then deleted. The negative image of
  `import_keep`, and a chart-quality lever nobody else in this space
  uses: an album many installs grabbed and many threw away should not
  rank like one they kept.

Both are the same privacy family as the existing signals (acquisition,
not listening). Listening history exists in SoulSync too and stays
**out** — it is a different category and needs its own explicit call.

## 2. The signals

Two new signal types, both keyed by **release-group MBID**, both binary
per node per week, same as everything else in SIN:

| Signal | Rule (computed on the node) |
|---|---|
| `download_grab` | At least one track of this album finished downloading this week (status completed) **and the album was not newly added to the library this week** — re-grabs, upgrades, gap-fills, and grabs that never landed. New arrivals are `recent_add`'s job; the two never fire for the same album-week on one node. |
| `download_churn` | This album was downloaded 14+ days ago, was in the library afterwards, and has now been absent from **two consecutive complete** library reads. Reported once per node per album (see §5). |

Plus one rule extension, no new type — **deferred at build time**
(see §4): `hunt_demand` would also fire when a wishlisted album had
a failed download attempt this week and still has no successful
download. A failed grab is stronger evidence than age, but failure
records cannot be keyed to albums today, so v1 does not build it.

Vocabulary discipline: SIN shipped with four types, gained `recent_add`
as the fifth, and these would make seven. That is the cap this design
proposes — no further types without retiring one. **[Amended Oct 23,
2026: the cap is eight, adding `listen_album` — see
DESIGN-LISTEN-SIGNALS.md §8.]**

## 3. Where the data lives (verified Oct 23, 2026)

SoulSync's database (`database/music_database.py`):

- **`track_downloads`** — one row per downloaded track: `status`
  (default `completed`), `created_at`, `file_path`, source service,
  and — written at post-processing — external IDs including
  `musicbrainz_recording_id`, `soul_id`, ISRC, and per-service track
  IDs. **No release or release-group ID.**
- **`library_history`** — `download`/`import` events with title,
  artist, album as *text* plus `file_path`. No IDs. Not used by this
  design (text never keys a signal).
- **`sync_history`** — playlist syncs with Spotify-shaped `tracks_json`
  and downloaded/failed counts. Not used in v1: keying it means
  resolving source-shaped tracks through the library, a separate
  project. (Downloads that came from a sync still land in
  `track_downloads`, so their albums count as grabs anyway.)
- **`listening_history`** — plays, joins cleanly to library tracks.
  Deliberately untouched (see §1).

The join that makes downloads keyable lives **inside SoulSync**:
`track_downloads.file_path` ↔ library `tracks.file_path` →
`tracks.album_id` → `albums.musicbrainz_release_id`. The node cannot do
this join — it has no path data and should never need any.

## 4. SoulSync API addition (one endpoint, SoulSync PR)

The node reads everything through SoulSync API v1; download facts are
not exposed today. New endpoint, same `sk_` admin-scope auth as the
other MetaSync reads:

```
GET /api/v1/library/download-activity?since=<ISO-8601>
→ {
    "albums": [
      {
        "musicbrainz_release_id": "<uuid>",
        "completed_downloads": 12,
        "completed_since": 3,
        "first_downloaded_at": "2026-09-30 10:00:00",
        "last_downloaded_at": "2026-10-21 09:30:00"
      }, …
    ]
  }
```

Rules for the endpoint, as built (SoulSync PR #1848):

- Aggregated **per album inside SoulSync** (the join above). One row
  per library album with at least one completed download. Bounded by
  the user's downloaded-album count; no pagination in v1.
- Only IDs, counts, and timestamps cross the wire — the same
  ID-only contract as every other MetaSync read. No titles, no paths.
- `since` is the server-side week window (the cleaner option §5
  hoped for): the node passes the report week's start and SoulSync
  counts completions at or after it into `completed_since`, compared
  as UTC instants, never as text. A malformed `since` is a 400.
  Without `since`, `completed_since` equals `completed_downloads`.
- Albums whose tracks have left the library no longer join and drop
  out of the response. That is load-bearing, not a gap: the node
  accumulates its own download history from weekly reads (§5), so a
  row disappearing is part of how churn is detected.
- **Failed attempts are deferred.** Verified at build time:
  `track_downloads` reliably records completions, while failure
  records (`download_decisions`) cannot be keyed to albums — so the
  §2 `hunt_demand` failed-attempt extension is out of v1 rather than
  built on a fuzzy join.

This is the only SoulSync change. Everything else is node + SIN
backend, in the same repo as the rest of MetaSync.

## 5. Node computation

New read in the weekly report flow, after the library read:

1. Fetch download-activity once; keep it with the run's other reads.
2. Bridge each row's release ID to a group with the existing
   release→group machinery. Rows whose album is *in the library* are
   already bridged by the main pass. Download-only releases (grabbed
   and already gone, or never imported) resolve **first** in the
   fresh-lookup budget — they are few and high-value.
3. `download_grab`: any row with `completed_since ≥ 1` (SoulSync
   counted this week's completions server-side against the `since`
   the node passed) whose group did **not** produce a `recent_add`
   this run — the exclusion is computed locally from the same run's
   library read (album in the library with `created_at` in the
   report week), so grab and recent_add stay disjoint by
   construction.
4. `download_churn` (only when the §7 toggle is on), as built:
   - Candidate: the group is in the node's own accumulated download
     history with a first download ≥ 14 days ago, **and** it was
     present in the library snapshot from **two runs ago**, **and**
     absent from the previous run's snapshot **and** this run's
     (two consecutive absences — a rescan gap or a drive dropping
     offline must never read as churn).
   - State: the node keeps `downloaded_groups` (group → first/last
     download, accumulated from weekly endpoint reads while albums
     live — the endpoint itself forgets deleted albums) and
     `library_group_snapshots` (the last two **complete** reads).
     Snapshots rotate only on complete reads.
   - The library read must be **complete**: if `albums_truncated` is
     set, churn is suppressed that run (candidates still counted in
     diagnostics, fired = 0).
   - Fires **once** per drop by construction: after firing, the
     two-absence window has moved past the last present snapshot.
     If the album **returns**, it re-enters the snapshots and a
     later genuine drop can count again (heals silently).
   - Candidates come only from accumulated state, which only ever
     contains groups resolved while the album lived in the library —
     zero extra lookups, and pre-node deletions are simply unknowable.
5. Diagnostics gain: download rows seen, unjoined rows, grabs
   computed, churn candidates vs fired, and `downloads_available`
   (false when the SoulSync build has no endpoint yet — the report
   still runs, minus download signals).

The Payload preview shows the download counts in its diagnostics and
the outgoing payload lists the grab/churn signals like every other
signal. Dry-run behavior is unchanged: computed, shown, not sent.

## 6. SIN backend + composer

- **Migration 0007** extends the `signal` CHECK constraints on
  `raw_reports` and `aggregates` with the two new types (same pattern
  as 0005 for `recent_add`). `SIGNAL_TYPES` in `src/sin/logic.ts`
  grows to seven. Caps unchanged (100,000 signals / 16 MB).
- **Trending heat** becomes `watchlist_add + max(wishlist_add,
  download_grab)` per album. One node wanting an album and then
  grabbing it is one node's heat, not two — the max() keeps a single
  node's desire from double-counting while still letting grabs from
  non-wishlisters count in full. (Grab is already disjoint from
  `recent_add` by construction — §5 — so new-arrival heat is never
  counted twice either.)
- **Loved & Kept** ranks by `import_keep − download_churn` and
  excludes albums whose churn meets or beats their keeps. Churn is a
  **ranking input only in v1 — never a displayed number.** Reason
  strings keep citing keeps ("Kept by N collectors"), never drops.
- **Population rule:** churn aggregates only exist for albums where
  ≥ 15 *toggle-on* nodes reported churn (k applies per signal as
  usual). Where no churn aggregate exists, no dampening is applied —
  the design does not compare churn from the opt-in population
  against keeps from the whole network and call the ratio truth.
- Most Wanted, Rising, Breakouts, genre/decade editions: unchanged,
  except they inherit the improved Loved & Kept / Trending inputs
  where they already consume them.

## 7. Consent design (the "is this optional?" question)

SIN as a whole is opt-in. Within it, this design splits the two new
signals by sensitivity:

- **`download_grab` joins the base signal set.** It says "I acquired
  this album this week" — the same statement `recent_add` already
  makes for imports, just covering all download paths. Same family,
  same anonymity shape.
- **`download_churn` gets its own switch in the node, default OFF.**
  Churn reports *deletions* — what you threw away. That is a step
  more personal, and it is the user's call, per node, in plain words:

  > **Share drop signals (optional).** Once a week, also report
  > albums you downloaded and later removed from your library, as
  > anonymous counts. This helps charts sink albums people grab and
  > throw away. Off by default; the exact signals are always visible
  > on the Payload preview page before anything is sent.

- **No silent vocabulary growth, ever.** An existing node never
  starts sending a signal type it didn't send before just because it
  updated. New base-set types are announced in the node's changelog
  and appear in the preview before the first send; toggle-gated types
  stay off until the user turns them on. This rule is part of the
  design, not a launch detail.

## 8. Build order

1. **SIN backend**: migration 0007 + validation + composer scoring
   changes + tests. Deploy first (an old backend rejects new types;
   a new backend accepts old reports fine).
2. **SoulSync**: `GET /api/v1/library/download-activity` + tests
   (normal SoulSync PR against `dev`, Boulder merges).
3. **Node**: download-activity read, signal computation, churn state,
   the §7 toggle + UI copy, preview section, diagnostics + tests.
4. **Composer verification**: synthetic two-population fixtures
   (toggle-on vs not) proving the population rule and the max() heat
   blend before any live use.

## 9. Risks and edges, honestly

- **False churn is the design's enemy.** The two-consecutive-absence
  rule, the complete-read guard, and the heal-on-return rule all exist
  because a wrongful "people hate this album" signal is worse than a
  missing one. If these guards make churn rare, that is the correct
  failure direction.
- **[Review, Oct 23, 2026]** Two of the "as built" rules above changed
  in DESIGN-NODE.md §12: the report now covers the closed week (so the
  endpoint is read with `since` **and** `until`), and the churn
  snapshots are one per reported week instead of one per run. Before
  that, two runs minutes apart counted as the "two consecutive
  absences".
- **Re-downloads and upgrades.** Quality upgrades delete and
  re-import; the album is present, so no churn, and the re-grab counts
  as a grab. Correct on both ends.
- **Coverage.** `track_downloads` external IDs were added by a later
  migration; older SoulSync installs have thinner rows. The join is
  by file path inside SoulSync, not by those IDs, so coverage follows
  download history itself, not the ID migration.
- **Albums downloaded but never imported** (failed post-processing)
  can grab but never churn — they were never in a library snapshot.
  Correct: you can't drop what you never had.

## 10. Open questions for Boulder

1. The §7 split — `download_grab` in the base set, `download_churn`
   behind its own default-off switch — approve, or should both be
   optional? (Design recommends the split.)
2. Churn as a silent ranking dampener in v1, never a displayed
   number — approve?
3. The two-consecutive-absence rule for churn (slower, much safer)
   — approve?

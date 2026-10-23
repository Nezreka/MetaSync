# Download-based signals for SIN — design

Status: **design, Oct 23, 2026.** Commissioned by Boulder after the
extra-data inventory of SoulSync's database. Nothing here is built yet.
Decisions needed from Boulder are in §7 and §10.

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

Plus one rule extension, no new type:

- `hunt_demand` also fires when a **wishlisted album had a failed
  download attempt this week** and still has no successful download.
  Today it only fires on wishlist age (7+ days). A failed grab is
  stronger evidence than age, and the meaning of the signal does not
  change: *wanted, still not got*.

Vocabulary discipline: SIN shipped with four types, gained `recent_add`
as the fifth, and these would make seven. That is the cap this design
proposes — no further types without retiring one.

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
GET /api/v1/library/download-activity
→ {
    "albums": [
      {
        "musicbrainz_release_id": "<uuid>",
        "completed_downloads": 12,
        "failed_downloads": 1,
        "first_downloaded_at": "2026-09-30T…",
        "last_downloaded_at": "2026-10-21T…",
        "in_library": true
      }, …
    ]
  }
```

Rules for the endpoint:

- Aggregated **per album inside SoulSync** (the join above). One row
  per album that has any download record, ever. Bounded by the user's
  downloaded-album count; no pagination in v1.
- Only IDs, counts, and timestamps cross the wire — the same
  ID-only contract as every other MetaSync read. No titles, no paths.
- `in_library` is SoulSync's own current truth for that album.
- Failed = a download record that never reached completed for that
  album in the window the node asks about; SoulSync counts from the
  rows it has, the node applies the week windows.

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
3. `download_grab`: any row with a download completed this week
   whose group did **not** produce a `recent_add` this run — the
   exclusion is computed locally from the same run's library read
   (album in the library with `created_at` in the report week), so
   grab and recent_add stay disjoint by construction. (SoulSync's
   per-album `last_downloaded_at` plus a `completed_this_week` count
   would be cleaner; if the endpoint can cheaply add
   `completed_downloads_this_week` computed server-side against a
   `?week=` parameter, prefer that — decide at build time, the
   node-side window check is the fallback.)
4. `download_churn` (only when the §7 toggle is on):
   - Candidate: completed download with `last_downloaded_at` ≥ 14
     days before the report week, **and** the group was present in a
     previous week's library snapshot, **and** the group is absent
     from this week's library groups, **and** the group was also
     absent from the *previous* completed run's library groups
     (two consecutive absences — a rescan gap or a drive dropping
     offline must never read as churn).
   - The library read must be **complete**: if `albums_truncated` is
     set, or the read errored, churn is skipped entirely that run.
   - State: the node keeps `churn_reported` (set of group MBIDs) and
     `last_library_groups` in its state store. A churn is reported
     once; if the album **returns** to the library, the group leaves
     `churn_reported`, so a later genuine drop can count again.
   - v1 resolves churn candidates from the **cache only**: a group
     the node never resolved while the album lived in the library
     cannot churn. That bounds lookups to zero extra and keeps
     precision high; pre-node download history warms up as albums
     are seen in the library over time.
5. Diagnostics gain: download rows seen, grabs computed, churn
   candidates suppressed (and why: truncated read, first absence,
   unresolved), download rows with no joinable album.

The Payload preview page gains a **Downloads** section listing exactly
the grab/churn signals in the outgoing report, same as the other
signals. Dry-run behavior is unchanged: computed, shown, not sent.

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

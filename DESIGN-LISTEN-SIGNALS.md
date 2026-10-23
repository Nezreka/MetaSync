# Listening signals for SIN — design

Status: **built, Oct 23, 2026**, as part of the full review (Boulder:
"we are not currently using listen history which I feel could be
useful, but I want user data protected"). Built across the SIN backend
(migration 0008, `listen_album`, the On Repeat chart), SoulSync
(`GET /api/v1/library/listening-activity`, branch
`metasync/listening-activity`), and the node (the `share_listening`
switch). Nothing is live until those three ship, in that order (§8).

## 1. Why

Every signal before this one describes *wanting* (watchlist, wishlist,
hunt), *having* (recent_add, import_keep) or *acquiring*
(download_grab, churn). None of them can say whether anyone actually
**plays** the album. The download design (DESIGN-DOWNLOAD-SIGNALS.md
§1) deliberately left listening out "until it gets its own explicit
call". This is that call.

Listening is the strongest taste evidence SoulSync holds, and it is
also the most personal. This design takes the useful part, "installs
are playing this album", and leaves out everything that makes
listening history sensitive: what, when, how often, in what order, and
who.

## 2. Never sent, never read off the box

Play counts · play times or time of day · individual tracks or their
order · how many albums you played in total · scrobble source or media
server · which SoulSync profile played it · anything from a profile
that keeps its own listening history.

## 3. The signal

| Signal | Key | Fires when |
|---|---|---|
| `listen_album` | release-group MBID | at least **3 distinct tracks** of the album were played during the closed report week, in SoulSync's **shared** listening pile — for at most the **50** most-played such albums |

It is binary per install per week, like every SIN signal: an album
played 3 times and an album played 300 times each contribute one
install.

**Why 3 distinct tracks.** One track on repeat is a song, not an
album. Shuffle and radio also scatter single plays across hundreds of
albums. Requiring three different tracks keeps the signal about albums
someone actually sat with. It also keeps the long tail of one-off
plays, the most identifying part of a listening history, from leaving
the machine at all. An EP or single with fewer than 3 tracks can never
qualify. That is accepted in v1.

**Why at most 50.** One report is linkable to itself within the week
(that is what the dedupe token does), so whatever a report contains,
the server sees together at arrival. Even without names, a full list
of everything someone played is a fingerprint. Fifty albums a week is
plenty for a chart and far short of a full listening profile. When
more than 50 qualify, the most played (by plays, then distinct days)
are sent.

## 4. Where the data comes from

The node reads a new SoulSync endpoint, and only when the switch is on:

```
GET /api/v1/library/listening-activity?since=<week start>&until=<week end>
→ { "albums": [ { "musicbrainz_release_id": "<uuid>",
                  "plays": 14, "distinct_tracks": 9, "distinct_days": 3 } ] }
```

- **Aggregated inside SoulSync**: `listening_history` joined to library
  tracks (`db_track_id`) and to albums. No titles, no play rows, and no
  timestamps cross the API. These counts stay on the node: the node
  turns them into the binary signal and sends nothing else.
- **Shared pile only.** SoulSync files every play into a pile (#1293).
  The admin reads the *shared* pile. A profile with its own ListenBrainz,
  Last.fm or media-server account keeps its own pile, out of the
  admin's view. The endpoint reads only the shared pile
  (`profile_id = SHARED_OWNER`), so an admin opting in never shares a
  housemate's personal history.
- **Bounded window.** Both bounds are required and the window is at
  most 31 days. The endpoint answers "this week", never "everything ever
  played".
- Plays that matched no library track (`db_track_id` NULL) cannot be
  keyed and are skipped. Albums without a MusicBrainz release ID never
  appear.

## 5. Privacy layers

The layers are independent. Each one would still help if another failed.

1. **Consent.** A separate switch, *Share listening signals
   (optional)*, **off by default**, settable on the dashboard or with
   `METASYNC_SHARE_LISTENING`. Off means the node does not even call
   the endpoint. An updated node never starts sending listening on its
   own (DESIGN-DOWNLOAD-SIGNALS.md §7: no silent vocabulary growth).
2. **Minimization at the source.** SoulSync hands the node per-album
   counts for one week, never play rows.
3. **Thresholding on the node.** At least 3 distinct tracks (§3).
4. **Cap on the node.** At most 50 per report (§3), visible in the
   Payload preview before anything is sent.
5. **Cap at the pool.** The SIN backend refuses a report with more than
   100 `listen_album` signals (`MAX_LISTEN_SIGNALS`). A modified node
   cannot upload a listening history.
6. **No per-install storage.** Since migration 0008 the pool counts a
   report the moment it arrives: one +1 per album on a running tally
   that holds no token. The weekly token is kept apart, only to refuse
   a second report for the same week. No row anywhere links one
   install's listening albums together, even briefly.
7. **k = 15.** An album appears on On Repeat only when at least 15
   opted-in installs played it that week. Below that, the data point
   does not exist publicly.

## 6. Honest limits

- **At arrival.** The pool processes one request carrying a node's
  whole weekly report, listening albums included. Nothing is stored
  per install, but the operator of the backend is in a position to log
  requests (DATA.md §15.1). The 50-album cap is what bounds that
  exposure.
- **Rare taste.** An album only a handful of installs play never
  publishes (k), but it does arrive. That is the point of the cap and
  the 3-track threshold: the rare one-off plays are the ones that stay
  home.
- **Shared accounts.** The shared pile can hold plays by people who
  share the admin's media-server account without a linked profile.
  That is SoulSync's own attribution model. The dashboard text says
  "nothing from a SoulSync profile that keeps its own listening
  history" so the admin knows what the switch covers.
- **Self-selected population.** On Repeat reflects opted-in installs
  only, like churn (DESIGN-DOWNLOAD-SIGNALS.md §6). That is stated, not
  hidden.
- **A stronger option, not built.** Randomized response, where each
  node flips each listening bit with a known probability before
  sending, would give every individual report plausible deniability at
  the cost of noisier counts and a higher effective k. It is the right
  next step if listening ever widens beyond one capped, binary,
  album-level signal.

## 7. Charts

**On Repeat**: the albums the most opted-in installs played this week,
top 25. In v1 `listen_album` feeds only On Repeat. Mixing listening
into Trending or Loved & Kept would blend a self-selected population
into whole-network charts, and that needs its own population rule
first (the same reasoning as churn).

## 8. Vocabulary and rollout

This is SIN's eighth signal type. DESIGN-DOWNLOAD-SIGNALS.md §2 capped
the vocabulary at seven. This design amends that cap to eight, for
the same reason the cap exists: every type is a public commitment
about what leaves people's machines.

Ship order (an old backend rejects the new type; a new backend accepts
old reports):

1. **SIN backend**: deploy with migration 0008.
2. **SoulSync**: merge `metasync/listening-activity`. A node on an older
   SoulSync gets a 404, shows "listening data was not available", and
   sends no listening signals that run.
3. **Node**: release. The switch starts off.

"""Signal computation — pure.

Inputs are the REAL v1 response shapes verified on SoulSync dev bd840487
(see soulsync.py). Where the API does not expose what a signal needs, the
item is skipped and counted in diagnostics["gaps"] — never invented. The
gaps (also in DESIGN-NODE.md):

GAP-1 (wishlist MBID): closed by SoulSync's metasync/release-group-enrichment
  branch — its MusicBrainz worker matches each wishlist row's album in the
  background and serves ``musicbrainz_release_group_id`` on the row. Until
  that runs (or on an older SoulSync) a row only contributes when its raw
  track_data carries a release-group MBID under one of
  WISHLIST_RG_MBID_KEYS. A row added in the week but matched only later
  is not lost: it waits in ``late_pending`` and fires in the report of the
  week it is matched, up to LATE_WINDOW after it was added. Library albums
  whose release group resolves late get the same treatment for recent_add.

GAP-2 (album release-group): SIN keys album signals by RELEASE-GROUP mbid.
  SoulSync now serves ``musicbrainz_release_group_id`` on library albums
  (same branch as GAP-1); the caller takes those as-is and only asks
  MusicBrainz for releases SoulSync has no group for yet. Albums whose
  release id is missing or unresolved are skipped.

GAP-3 (import date): an album's created_at is when its row entered the
  library DB, the import-date proxy; there is no dedicated imported_at. A
  re-scan or migration can reset it, so the node keeps ``first_seen``:
  per release group, the earliest of every created_at it has read. An
  album whose group was seen before the week is not a recent_add again,
  and import_keep counts 14 days from the earliest date, so a reset never
  re-sends a keeper either.

GAP-4 (hunt_demand success): SoulSync removes fulfilled wishlist items,
  so "still on the wishlist" means "not downloaded". A row is a hunt only
  when SoulSync has actually tried it (retry_count >= 1: searched, and
  nothing found or kept) and its album is not already in the library (a
  missing track of an owned album is not demand for the album).

THE REPORT IS ONE CLOSED WEEK (DESIGN-NODE.md §12). The node reports the
ISO week that just ended, [week_start, week_end): "this week" signals are
events inside it, and the age rules (7 / 14 days) are measured at
week_end. A report describes what happened, never what the library IS:
every event signal fires once (import_keep in the week an album crosses
its 14-day mark, churn in the week of the second absence), and a week
with more than BULK_EVENT_LIMIT events of one type is a bulk operation —
an import, a migration, a drive coming back — that is suppressed rather
than reported, so no report is ever a library listing.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from .weekutil import iso_week_string, parse_iso

SIGNAL_TYPES = (
    "watchlist_add", "wishlist_add", "import_keep", "hunt_demand",
    "recent_add", "download_grab", "download_churn", "listen_album",
)

# Event signals: more than this many of one type in a week is a bulk
# operation (a library import, a wishlist migration, a drive remounting),
# not taste. The whole type is suppressed for the week and the count is
# shown in diagnostics; reporting it would send a library listing. The
# SIN backend refuses more than 1,000 of a type in any case.
BULK_EVENT_LIMIT = 500
EVENT_TYPES = (
    "watchlist_add", "wishlist_add", "import_keep", "recent_add",
    "download_grab", "download_churn",
)
# hunt_demand is a standing state ("still wanted"), not an event: a big
# wishlist is real demand, so it is sampled down to this many instead of
# dropped. The sample is a hash of (week, mbid): deterministic within a
# week, rotating across weeks, and the same for every node.
HUNT_DEMAND_LIMIT = 500
# listen_album (DESIGN-LISTEN-SIGNALS.md): an album counts as listened when
# at least this many distinct tracks of it were played in the week, and
# a report carries at most LISTEN_LIMIT albums (the most played).
LISTEN_MIN_DISTINCT_TRACKS = 3
LISTEN_LIMIT = 50

KEEP_AFTER = timedelta(days=14)
HUNT_AFTER = timedelta(days=7)
# How long a wishlist row or library album unmatched in its own week waits
# for its release group before it is forgotten (GAP-1).
LATE_WINDOW = timedelta(days=14)

_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

WISHLIST_RG_MBID_KEYS = (
    "musicbrainz_release_group_id",
    "musicbrainz_releasegroup_id",
    "release_group_mbid",
)


def is_uuid_shape(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_SHAPE.match(value))


def wishlist_release_group_mbid(track: dict[str, Any]) -> str | None:
    # SoulSync's MusicBrainz worker matches wishlist rows in the background
    # and serves the result on the row itself; raw track_data keys are the
    # fallback for builds without that worker (GAP-1).
    served = track.get("musicbrainz_release_group_id")
    if is_uuid_shape(served):
        return served.lower()
    track_data = track.get("track_data")
    if not isinstance(track_data, dict):
        return None
    for key in WISHLIST_RG_MBID_KEYS:
        value = track_data.get(key)
        if is_uuid_shape(value):
            return value.lower()
    return None


def _instant(value: Any) -> datetime | None:
    """A SoulSync timestamp as an aware UTC instant (naive reads as UTC);
    None for anything missing or malformed — a bad date skips its item,
    never the report."""
    return parse_iso(value) if isinstance(value, str) else None


def _row_key(row: dict[str, Any]) -> str | None:
    """SoulSync's own row id: stable across reads, says nothing outside
    this install, and never leaves the node."""
    value = row.get("id")
    return str(value) if value not in (None, "") else None


def _hunt_sample_key(week: str, mbid: str) -> str:
    return hashlib.sha256(f"{week}:{mbid}".encode()).hexdigest()


def compute_signals(
    *,
    watchlist: list[dict[str, Any]],
    wishlist: list[dict[str, Any]],
    albums: list[dict[str, Any]],
    release_to_group: dict[str, str],
    week_start: datetime,
    downloads: list[dict[str, Any]] | None = None,
    downloaded_groups: dict[str, dict[str, str]] | None = None,
    library_groups_prev: set[str] | None = None,
    library_groups_prev2: set[str] | None = None,
    share_churn: bool = False,
    library_complete: bool = True,
    listening: list[dict[str, Any]] | None = None,
    first_seen: dict[str, str] | None = None,
    late_pending: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The signals for the closed week starting at ``week_start`` (a UTC
    Monday 00:00). ``listening`` is None unless the user opted in.
    ``first_seen`` (group -> earliest created_at seen) and ``late_pending``
    are the node's own memory from earlier runs (GAP-1, GAP-3); the
    result carries the updated versions for the caller to store."""
    week_end = week_start + timedelta(days=7)
    week = iso_week_string(week_start)
    found: dict[str, list[str]] = {t: [] for t in SIGNAL_TYPES}

    def add(signal_type: str, mbid: str) -> None:
        found[signal_type].append(mbid.lower())

    def in_week(value: Any) -> bool:
        when = _instant(value)
        return when is not None and week_start <= when < week_end

    def group_of(album: dict[str, Any]) -> str | None:
        release_id = album.get("musicbrainz_release_id")
        if not is_uuid_shape(release_id):
            return None
        return release_to_group.get(release_id.lower())

    gaps = {
        "watchlist_no_mbid": 0,
        "wishlist_no_mbid": 0,
        "albums_no_release_mbid": 0,
        "albums_unresolved_group": 0,
    }

    for artist in watchlist:
        if not in_week(artist.get("date_added") or artist.get("created_at")):
            continue
        mbid = artist.get("musicbrainz_artist_id")
        if not is_uuid_shape(mbid):
            gaps["watchlist_no_mbid"] += 1
            continue
        add("watchlist_add", mbid)

    # The library's release groups right now (resolved ones only) — the
    # churn rule and the caller's snapshot rotation both key off this set.
    # Coverage gaps are counted over the whole library.
    seen_before = dict(first_seen or {})
    prior_late = late_pending or {}
    prior_late_albums = prior_late.get("albums") or {}
    prior_late_wishlist = prior_late.get("wishlist") or {}
    new_first_seen = dict(seen_before)
    new_late: dict[str, dict[str, dict[str, str]]] = {"albums": {}, "wishlist": {}}
    late_fired = {"recent_add": 0, "wishlist_add": 0}

    def waiting(prior: dict[str, Any], key: str | None) -> bool:
        # Still waiting, or fired for THIS week (a re-run of the same
        # week, e.g. a dry-run preview then the real send, must give the
        # same report). Fired for an earlier week: done, never again.
        entry = prior.get(key) if key else None
        if entry is None:
            return False
        fired = entry.get("fired") if isinstance(entry, dict) else None
        return fired is None or fired == week

    def remember(bucket: str, key: str, when: datetime, fired: bool = False) -> None:
        entry = {"at": when.isoformat()}
        if fired:
            entry["fired"] = week
        new_late[bucket][key] = entry

    def recently(when: datetime | None) -> bool:
        # Inside the late window, before this week (this week is on time).
        return when is not None and week_start - LATE_WINDOW <= when < week_start

    def new_to_library(rg: str, created: datetime) -> bool:
        # A group first seen (by an earlier read) before this album's own
        # date is an album the library already had: a re-scan, a
        # migration, a second edition. Never a recent_add.
        earlier = _instant(seen_before.get(rg))
        return earlier is None or earlier >= created - timedelta(days=1)

    current_groups: set[str] = set()
    albums_recent = 0
    recent_groups: set[str] = set()
    for album in albums:
        release_id = album.get("musicbrainz_release_id")
        rg = group_of(album)
        if not is_uuid_shape(release_id):
            gaps["albums_no_release_mbid"] += 1
        elif not rg:
            gaps["albums_unresolved_group"] += 1
        if rg:
            current_groups.add(rg)
        created = _instant(album.get("created_at"))
        if created is None:
            continue
        key = _row_key(album)
        if rg:
            stored = _instant(new_first_seen.get(rg))
            if stored is None or created < stored:
                new_first_seen[rg] = created.isoformat()
        # recent_add: landed in the library during the week.
        if week_start <= created < week_end:
            albums_recent += 1
            if rg and new_to_library(rg, created):
                add("recent_add", rg)
                recent_groups.add(rg)
            elif not rg and key:
                remember("albums", key, created)
        elif waiting(prior_late_albums, key) and recently(created):
            # Landed in an earlier week, matched only now (GAP-1).
            if rg:
                if new_to_library(rg, created):
                    add("recent_add", rg)
                    recent_groups.add(rg)
                    late_fired["recent_add"] += 1
                    remember("albums", key, created, fired=True)
            else:
                remember("albums", key, created)
        # import_keep: crossed its 14-day mark during the week and is still
        # here — once per album, the week it became a keeper, never again.
        # Counted from the earliest date the node ever saw for the group,
        # so a reset created_at cannot make an old album a keeper again.
        if rg:
            basis = min(created, _instant(seen_before.get(rg)) or created)
            if week_start <= basis + KEEP_AFTER < week_end:
                add("import_keep", rg)

    for track in wishlist:
        rg = wishlist_release_group_mbid(track)
        added = _instant(track.get("date_added"))
        key = _row_key(track)
        if rg is None:
            gaps["wishlist_no_mbid"] += 1
            if key and added is not None and (week_start <= added < week_end or (waiting(prior_late_wishlist, key) and recently(added))):
                remember("wishlist", key, added)
            continue
        if in_week(track.get("date_added")):
            add("wishlist_add", rg)
        elif waiting(prior_late_wishlist, key) and recently(added):
            add("wishlist_add", rg)  # added earlier, matched only now (GAP-1)
            late_fired["wishlist_add"] += 1
            remember("wishlist", key, added, fired=True)
        # hunt_demand (GAP-4): wanted for a week, tried by SoulSync without
        # success, and the album still not in the library.
        tried = int(track.get("retry_count") or 0) >= 1
        if added is not None and week_end - added >= HUNT_AFTER and tried and rg not in current_groups:
            add("hunt_demand", rg)

    # download_grab (DESIGN-DOWNLOAD-SIGNALS.md): acquisition this week
    # that recent_add cannot see — re-grabs, upgrades and gap-fills of
    # albums already owned, plus grabs that never landed in the library.
    # Disjoint from recent_add by construction: a group that produced a
    # recent_add this run never also grabs. ``downloads`` rows come from
    # SoulSync's download-activity endpoint with completed_since counted
    # over [week_start, week_end), so completed_since >= 1 IS "grabbed
    # this week"; no week math happens here.
    downloads_seen = 0
    downloads_unjoined = 0
    if downloads is not None:
        downloads_seen = len(downloads)
        for row in downloads:
            release_id = row.get("musicbrainz_release_id")
            if not is_uuid_shape(release_id):
                downloads_unjoined += 1
                continue
            if int(row.get("completed_since") or 0) < 1:
                continue
            rg = release_to_group.get(release_id.lower())
            if not rg:
                downloads_unjoined += 1
                continue
            if rg in recent_groups:
                continue
            add("download_grab", rg)

    # download_churn — opt-in only (share_churn), and the most guarded
    # rule in the node, because a false drop signal accuses a collector
    # of deleting music they merely moved: the album was downloaded 14+
    # days ago, sat in the library snapshot from two weeks ago, and has
    # now been absent from TWO consecutive complete weekly reads (last
    # week's snapshot and this one). Absence from a truncated or failed
    # read proves nothing, so an incomplete library disables the rule
    # for the run. A group that returns heals silently: it is in
    # current_groups and falls out of the snapshots on its own. The
    # two-absence window also makes each drop fire exactly once.
    churn_candidates = 0
    if share_churn and downloaded_groups:
        prev = library_groups_prev or set()
        prev2 = library_groups_prev2 or set()
        for rg, info in downloaded_groups.items():
            if rg in current_groups or rg in prev or rg not in prev2:
                continue
            churn_candidates += 1
            if not library_complete:
                continue
            first = _instant(info.get("first_downloaded_at"))
            if first is None or week_end - first < KEEP_AFTER:
                continue
            add("download_churn", rg)

    # listen_album — opt-in only (the caller passes ``listening`` only when
    # share_listening is on). Thresholded per album so one shuffle hit is
    # not an album, and capped so a week's report is never a full
    # listening profile: the LISTEN_LIMIT most-played albums, at most.
    listen_candidates = 0
    if listening is not None:
        by_group: dict[str, tuple[int, int, int]] = {}
        for row in listening:
            release_id = row.get("musicbrainz_release_id")
            if not is_uuid_shape(release_id):
                continue
            rg = release_to_group.get(release_id.lower())
            if not rg:
                continue
            plays = int(row.get("plays") or 0)
            tracks = int(row.get("distinct_tracks") or 0)
            days = int(row.get("distinct_days") or 0)
            # Two editions of one album sum into their group.
            p, t, d = by_group.get(rg, (0, 0, 0))
            by_group[rg] = (p + plays, t + tracks, max(d, days))
        qualified = [
            (rg, stats) for rg, stats in by_group.items()
            if stats[1] >= LISTEN_MIN_DISTINCT_TRACKS
        ]
        listen_candidates = len(qualified)
        qualified.sort(key=lambda item: (-item[1][0], -item[1][2], item[0]))
        for rg, _stats in qualified[:LISTEN_LIMIT]:
            add("listen_album", rg)

    # Binary per node per item per week, then the bulk rules.
    bulk_suppressed: dict[str, int] = {}
    signals: list[dict[str, str]] = []
    for signal_type in SIGNAL_TYPES:
        mbids = sorted(set(found[signal_type]))
        if signal_type in EVENT_TYPES and len(mbids) > BULK_EVENT_LIMIT:
            bulk_suppressed[signal_type] = len(mbids)
            continue
        if signal_type == "hunt_demand" and len(mbids) > HUNT_DEMAND_LIMIT:
            bulk_suppressed[signal_type] = len(mbids)
            mbids = sorted(sorted(mbids, key=lambda m: _hunt_sample_key(week, m))[:HUNT_DEMAND_LIMIT])
        signals.extend({"type": signal_type, "mbid": mbid} for mbid in mbids)
    signals.sort(key=lambda s: (s["type"], s["mbid"]))

    def fired(signal_type: str) -> int:
        return sum(1 for s in signals if s["type"] == signal_type)

    return {
        "week": week,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "signals": signals,
        "diagnostics": {
            "watchlist_seen": len(watchlist),
            "wishlist_seen": len(wishlist),
            "albums_seen": len(albums),
            "albums_recent": albums_recent,
            "downloads_seen": downloads_seen,
            "downloads_unjoined": downloads_unjoined,
            "download_grabs": fired("download_grab"),
            "churn_candidates": churn_candidates,
            "churn_fired": fired("download_churn"),
            "listening_shared": listening is not None,
            "listen_candidates": listen_candidates,
            "listen_sent": fired("listen_album"),
            "bulk_suppressed": bulk_suppressed,
            "library_groups_now": len(current_groups),
            "late_matched": late_fired,
            "late_waiting": {k: sum(1 for e in v.values() if "fired" not in e) for k, v in new_late.items()},
            "gaps": gaps,
        },
        "library_groups": sorted(current_groups),
        "first_seen": new_first_seen,
        "late_pending": new_late,
    }

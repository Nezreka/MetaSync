"""Signal computation — pure.

Inputs are the REAL v1 response shapes verified on SoulSync dev bd840487
(see soulsync.py). Where the API does not expose what a signal needs, the
item is skipped and counted in diagnostics["gaps"] — never invented. The
gaps (also in DESIGN-NODE.md):

GAP-1 (wishlist MBID): serialize_wishlist_track has NO MusicBrainz field.
  A wishlist item only contributes wishlist_add / hunt_demand when its raw
  track_data carries an explicit release-group MBID under one of
  WISHLIST_RG_MBID_KEYS. Spotify-shaped track_data (the common case) has
  none, so most wishlist items are skipped until SoulSync core exposes a
  release-group MBID on wishlist rows.

GAP-2 (album release-group): library albums expose musicbrainz_release_id
  (a RELEASE mbid), while SIN keys album signals by RELEASE-GROUP mbid.
  The caller resolves release -> release-group through MusicBrainz
  (deterministic lookup) and passes the map in; albums whose release id is
  missing or unresolved are skipped.

GAP-3 (import date): an album's created_at is when its row entered the
  library DB (the recently-added endpoint orders by it), so it is the
  import-date proxy for import_keep. There is no dedicated imported_at.

GAP-4 (hunt_demand success): "no successful download yet" is proxied by
  "still on the wishlist" — SoulSync removes fulfilled wishlist items;
  the API exposes no per-item download-success flag.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .weekutil import days_between, iso_week_string

SIGNAL_TYPES = (
    "watchlist_add", "wishlist_add", "import_keep", "hunt_demand",
    "recent_add", "download_grab", "download_churn",
)

_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

WISHLIST_RG_MBID_KEYS = (
    "musicbrainz_release_group_id",
    "musicbrainz_releasegroup_id",
    "release_group_mbid",
)


def is_uuid_shape(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_SHAPE.match(value))


def wishlist_release_group_mbid(track: dict[str, Any]) -> str | None:
    track_data = track.get("track_data")
    if not isinstance(track_data, dict):
        return None
    for key in WISHLIST_RG_MBID_KEYS:
        value = track_data.get(key)
        if is_uuid_shape(value):
            return value.lower()
    return None


def _in_week(iso_value: Any, week: str) -> bool:
    if not isinstance(iso_value, str) or not iso_value:
        return False
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        return False
    return iso_week_string(parsed) == week


def compute_signals(
    *,
    watchlist: list[dict[str, Any]],
    wishlist: list[dict[str, Any]],
    albums: list[dict[str, Any]],
    release_to_group: dict[str, str],
    now: datetime,
    downloads: list[dict[str, Any]] | None = None,
    downloaded_groups: dict[str, dict[str, str]] | None = None,
    library_groups_prev: set[str] | None = None,
    library_groups_prev2: set[str] | None = None,
    share_churn: bool = False,
    library_complete: bool = True,
) -> dict[str, Any]:
    week = iso_week_string(now)
    found: dict[str, dict[str, str]] = {}

    def add(signal_type: str, mbid: str) -> None:
        # The binary rule: at most one of each (type, mbid) per week.
        found[f"{signal_type}:{mbid.lower()}"] = {"type": signal_type, "mbid": mbid.lower()}

    gaps = {
        "watchlist_no_mbid": 0,
        "wishlist_no_mbid": 0,
        "albums_no_release_mbid": 0,
        "albums_unresolved_group": 0,
    }

    for artist in watchlist:
        added = artist.get("date_added") or artist.get("created_at")
        if not _in_week(added, week):
            continue
        mbid = artist.get("musicbrainz_artist_id")
        if not is_uuid_shape(mbid):
            gaps["watchlist_no_mbid"] += 1
            continue
        add("watchlist_add", mbid)

    for track in wishlist:
        rg = wishlist_release_group_mbid(track)
        if rg is None:
            gaps["wishlist_no_mbid"] += 1
            continue
        if _in_week(track.get("date_added"), week):
            add("wishlist_add", rg)
        date_added = track.get("date_added")
        if isinstance(date_added, str) and days_between(date_added, now) >= 7:
            add("hunt_demand", rg)

    for album in albums:
        created_at = album.get("created_at")
        if not isinstance(created_at, str) or days_between(created_at, now) < 14:
            continue
        release_id = album.get("musicbrainz_release_id")
        if not is_uuid_shape(release_id):
            gaps["albums_no_release_mbid"] += 1
            continue
        rg = release_to_group.get(release_id.lower())
        if not rg:
            gaps["albums_unresolved_group"] += 1
            continue
        add("import_keep", rg)

    # recent_add: albums that landed in the library during the report week.
    # The mirror image of import_keep's 14-day rule: a kept album is a
    # staple, a brand-new one is heat, and they chart separately ("Just
    # Landed"). Same release-group resolution, same binary rule.
    albums_recent = 0
    recent_groups: set[str] = set()
    for album in albums:
        if not _in_week(album.get("created_at"), week):
            continue
        albums_recent += 1
        release_id = album.get("musicbrainz_release_id")
        if not is_uuid_shape(release_id):
            continue
        rg = release_to_group.get(release_id.lower())
        if rg:
            add("recent_add", rg)
            recent_groups.add(rg)

    # The library's release groups right now (resolved ones only) — the
    # churn rule and the caller's snapshot rotation both key off this set.
    current_groups: set[str] = set()
    for album in albums:
        release_id = album.get("musicbrainz_release_id")
        if not is_uuid_shape(release_id):
            continue
        rg = release_to_group.get(release_id.lower())
        if rg:
            current_groups.add(rg)

    # download_grab (DESIGN-DOWNLOAD-SIGNALS.md): acquisition this week
    # that recent_add cannot see — re-grabs, upgrades and gap-fills of
    # albums already owned, plus grabs that never landed in the library.
    # Disjoint from recent_add by construction: a group that produced a
    # recent_add this run never also grabs. ``downloads`` rows come from
    # SoulSync's download-activity endpoint with completed_since counted
    # from the report week's start, so completed_since >= 1 IS "grabbed
    # this week"; no week math happens here.
    downloads_seen = 0
    downloads_unjoined = 0
    grabs = 0
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
            grabs += 1

    # download_churn — opt-in only (share_churn), and the most guarded
    # rule in the node, because a false drop signal accuses a collector
    # of deleting music they merely moved: the album was downloaded 14+
    # days ago, sat in the library snapshot from two runs ago, and has
    # now been absent from TWO consecutive complete library reads (the
    # previous run's snapshot and this one). Absence from a truncated or
    # failed read proves nothing, so an incomplete library disables the
    # rule for the run. A group that returns heals silently: it is in
    # current_groups and falls out of the snapshots on its own. The
    # two-absence window also makes each drop fire exactly once.
    churn_fired = 0
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
            first = info.get("first_downloaded_at")
            if not isinstance(first, str) or days_between(first, now) < 14:
                continue
            add("download_churn", rg)
            churn_fired += 1

    signals = sorted(found.values(), key=lambda s: (s["type"], s["mbid"]))
    return {
        "week": week,
        "signals": signals,
        "diagnostics": {
            "watchlist_seen": len(watchlist),
            "wishlist_seen": len(wishlist),
            "albums_seen": len(albums),
            "albums_recent": albums_recent,
            "downloads_seen": downloads_seen,
            "downloads_unjoined": downloads_unjoined,
            "download_grabs": grabs,
            "churn_candidates": churn_candidates,
            "churn_fired": churn_fired,
            "library_groups_now": len(current_groups),
            "gaps": gaps,
        },
        "library_groups": sorted(current_groups),
    }

"""Canonical playlists + local materialization.

A CANONICAL playlist is the pool-level definition published by the
official node: {key, name, tracks} where tracks are already in SoulSync's
Spotify-shaped sync format. User nodes pull these verbatim and
materialize LOCAL copies in their own SoulSync.

Local playlist identity is STABLE and minted by the node:
    metasync_<pool_id>_<key>     e.g. metasync_sin_trending
Every refresh syncs the SAME id with sync_mode "replace" (explicit —
SoulSync's omitted-mode default resolves to "reconcile"; see
soulsync.py). A refresh updates the playlist the user already has; it
never creates a new playlist per week.

ADMIN definition building (official node only): the three global
playlists are built from the pool index —
    trending    -> "SIN · Trending in SoulSync"
    most-wanted -> "SIN · Most Wanted"
    breakouts   -> "SIN · Breakout Artists"
Trending / Most Wanted items are release-group MBIDs (an artist MBID can
also appear in Trending, because the aggregator sums watchlist_add into
it — those resolve as artists). Breakouts are ARTIST MBIDs: they become a
playlist by sampling up to tracks_per_item tracks from each breakout
artist's newest Album release-group — a breakout is an artist rail, so
its playlist is a sampler, not the artist's catalogue.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .musicbrainz import MusicBrainzResolver
from .pool import PoolClient

PLAYLIST_KEYS = ("trending", "most-wanted", "breakouts")
PLAYLIST_NAMES = {
    "trending": "SIN · Trending in SoulSync",
    "most-wanted": "SIN · Most Wanted",
    "breakouts": "SIN · Breakout Artists",
}


def local_playlist_id(pool_id: str, key: str) -> str:
    safe_pool = re.sub(r"[^a-z0-9]+", "_", pool_id.lower()).strip("_") or "pool"
    return f"metasync_{safe_pool}_{key}"


def content_hash(tracks: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([[t.get("id"), t.get("name")] for t in tracks]).encode())
    return digest.hexdigest()


def _resolve_item_tracks(item: dict[str, Any], kind: str, resolver: MusicBrainzResolver, tracks_per_item: int) -> list[dict[str, Any]] | None:
    mbid = item["mbid"]
    if kind == "breakouts":
        artist = resolver.resolve_artist(mbid, tracks_per_item)
        return artist["tracks"] if artist and artist["tracks"] else None
    album = resolver.resolve_release_group(mbid, tracks_per_item)
    if album is not None:
        return album["tracks"] if album["tracks"] else None
    artist = resolver.resolve_artist(mbid, tracks_per_item)
    return artist["tracks"] if artist and artist["tracks"] else None


def build_canonical_playlists(
    pool: PoolClient,
    resolver: MusicBrainzResolver,
    *,
    max_items: int = 20,
    tracks_per_item: int = 3,
) -> dict[str, Any]:
    """ADMIN flow: index -> canonical playlist definitions via MusicBrainz."""
    playlists: list[dict[str, Any]] = []
    unresolved: dict[str, list[str]] = {}
    week: str | None = None
    for key in PLAYLIST_KEYS:
        index = pool.get_index(key)
        week = week or index.get("week")
        tracks: list[dict[str, Any]] = []
        seen: set[str] = set()
        missing: list[str] = []
        for item in (index.get("items") or [])[:max_items]:
            item_tracks = _resolve_item_tracks(item, key, resolver, tracks_per_item)
            if not item_tracks:
                missing.append(item["mbid"])
                continue
            for track in item_tracks:
                if track["id"] in seen:
                    continue
                seen.add(track["id"])
                tracks.append(track)
        unresolved[key] = missing
        playlists.append({"key": key, "name": PLAYLIST_NAMES[key], "tracks": tracks})
    return {"week": week, "playlists": playlists, "unresolved": unresolved}

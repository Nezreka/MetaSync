"""MusicBrainz resolver — used by the ADMIN (official) flow only.

The SIN index carries MBIDs only. Turning the index into canonical
playlist definitions (titles, artists, track lists) is definition
building, and Boulder's permission model puts that on the official node —
user nodes pull finished playlist data from the pool instead of resolving
MBIDs themselves. The one exception is a deterministic lookup every node
needs for its own signals: release MBID -> release-group MBID
(GAP-2 in signals.py), which maps an id the user already has locally and
publishes nothing.

Rules honoured: proper User-Agent, <= 1 req/sec (serialised + min
interval, injectable for tests), responses cached (the cache dict is the
state store's, so it persists across runs). Cover art uses the Cover Art
Archive's deterministic release-group URL.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests


class MusicBrainzError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _artist_credit_name(credits: Any) -> str:
    if not isinstance(credits, list) or not credits:
        return "Unknown Artist"
    names = []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or (credit.get("artist") or {}).get("name")
            if name:
                names.append(str(name))
    return ", ".join(names) or "Unknown Artist"


def cover_url(release_group_mbid: str) -> str:
    return f"https://coverartarchive.org/release-group/{release_group_mbid}/front-250"


class MusicBrainzResolver:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        min_interval_s: float = 1.1,
        cache: dict[str, Any] | None = None,
        session: requests.Session | None = None,
        sleep=time.sleep,
        timeout_s: int = 20,
    ):
        self.base = base_url.rstrip("/")
        self.user_agent = user_agent
        self.min_interval_s = min_interval_s
        self.cache: dict[str, Any] = cache if cache is not None else {}
        self.session = session or requests.Session()
        self._sleep = sleep
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._last_call_at = 0.0

    def _get_json(self, path: str, *, keep: bool = True) -> dict[str, Any]:
        """GET a MusicBrainz path, paced. ``keep=False`` skips the response
        cache for lookups whose caller keeps the answer itself."""
        if path in self.cache:
            return self.cache[path]
        with self._lock:
            if path in self.cache:
                return self.cache[path]
            wait = self.min_interval_s - (time.monotonic() - self._last_call_at)
            if wait > 0:
                self._sleep(wait)
            self._last_call_at = time.monotonic()
            try:
                res = self.session.get(
                    f"{self.base}{path}",
                    headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                    timeout=self.timeout_s,
                )
            except requests.RequestException as err:
                raise MusicBrainzError(f"MusicBrainz unreachable: {err}") from err
            if res.status_code == 404:
                raise MusicBrainzError(f"MusicBrainz 404 for {path}", status=404)
            if not res.ok:
                raise MusicBrainzError(f"MusicBrainz HTTP {res.status_code} for {path}", status=res.status_code)
            body = res.json()
            if keep:
                self.cache[path] = body
            return body

    # -- deterministic id mapping (all nodes, for their own signals) -------
    def release_to_group(self, release_mbid: str) -> str | None:
        # The caller keeps the answer (state["release_to_group"]); caching
        # the whole release JSON as well cost ~3 KB per album forever.
        try:
            body = self._get_json(f"/release/{release_mbid}?inc=release-groups&fmt=json", keep=False)
        except MusicBrainzError as err:
            if err.status == 404:
                return None
            raise
        group = body.get("release-group") or {}
        return group.get("id")

    # -- definition building (admin flow) ----------------------------------
    def _tracks_for_release_group(self, rg_mbid: str, album_title: str, limit: int) -> list[dict[str, Any]]:
        body = self._get_json(f"/release-group/{rg_mbid}?inc=releases&fmt=json")
        releases = [
            r for r in (body.get("releases") or [])
            if isinstance(r, dict) and (r.get("status") in (None, "Official"))
        ]
        releases.sort(key=lambda r: str(r.get("date") or "9999"))
        if not releases or not releases[0].get("id"):
            return []
        release = self._get_json(f"/release/{releases[0]['id']}?inc=recordings+artist-credits&fmt=json")
        out: list[dict[str, Any]] = []
        for medium in release.get("media") or []:
            for track in (medium or {}).get("tracks") or []:
                if len(out) >= limit:
                    return out
                recording = track.get("recording") or {}
                title = track.get("title") or recording.get("title")
                if not title:
                    continue
                out.append({
                    "id": str(recording.get("id") or track.get("id") or f"{rg_mbid}-{len(out)}"),
                    "name": str(title),
                    "artists": [{"name": _artist_credit_name(track.get("artist-credit"))}],
                    "album": {"name": album_title, "images": [{"url": cover_url(rg_mbid)}]},
                    "duration_ms": recording.get("length") if isinstance(recording.get("length"), int) else 0,
                    "image_url": cover_url(rg_mbid),
                })
        return out

    def resolve_release_group(self, rg_mbid: str, track_limit: int) -> dict[str, Any] | None:
        try:
            body = self._get_json(f"/release-group/{rg_mbid}?inc=artist-credits&fmt=json")
        except MusicBrainzError as err:
            if err.status == 404:
                return None
            raise
        title = str(body.get("title") or "")
        return {
            "kind": "release-group",
            "mbid": rg_mbid,
            "title": title,
            "artist_name": _artist_credit_name(body.get("artist-credit")),
            "tracks": self._tracks_for_release_group(rg_mbid, title, track_limit),
        }

    def resolve_artist(self, artist_mbid: str, track_limit: int) -> dict[str, Any] | None:
        try:
            body = self._get_json(f"/artist/{artist_mbid}?inc=release-groups&fmt=json")
        except MusicBrainzError as err:
            if err.status == 404:
                return None
            raise
        groups = [
            g for g in (body.get("release-groups") or [])
            if isinstance(g, dict)
            and g.get("primary-type") == "Album"
            and not (g.get("secondary-types") or [])
        ]
        groups.sort(key=lambda g: str(g.get("first-release-date") or ""), reverse=True)
        if not groups:
            return {"kind": "artist", "mbid": artist_mbid, "name": str(body.get("name") or ""),
                    "sampled_release_group": None, "tracks": []}
        top = groups[0]
        return {
            "kind": "artist",
            "mbid": artist_mbid,
            "name": str(body.get("name") or ""),
            "sampled_release_group": top.get("id"),
            "tracks": self._tracks_for_release_group(str(top["id"]), str(top.get("title") or ""), track_limit),
        }

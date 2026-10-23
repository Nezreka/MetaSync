"""SoulSync API v1 client.

Shapes verified against SoulSync dev head
bd8404875ae42caa6c61a510203d823ce0ec91e0 (api/__init__.py, api/helpers.py,
api/watchlist.py, api/wishlist.py, api/library.py, api/serializers.py,
api/playlists.py, api/source_playlists.py) on Oct 22, 2026, via the
GitHub API:

- Base: <SOULSYNC_URL>/api/v1, auth `Authorization: Bearer sk_...`.
- Envelope: { success, data, error: {code,message} | None, pagination }.
- GET /watchlist -> data.artists[] (serialize_watchlist_artist; NOT paginated):
    { id, source, musicbrainz_artist_id, artist_name, date_added, created_at, ... }
- GET /wishlist -> data.tracks[] (serialize_wishlist_track), PAGINATED
  (?page&limit, default 50, max 200; pagination {page, limit, total,
  total_pages, has_next, has_prev}):
    { id, track_id, track_name, artist_name, album_name, track_data,
      date_added, last_attempted, ... }
    (track_data = the raw provider track object; NO MusicBrainz field on
    the row itself — see GAP-1 in signals.py.)
- GET /library/albums -> data.albums[] (serialize_album), PAGINATED:
    { id, title, created_at, musicbrainz_release_id, ... }
    NOTE: albums expose a MusicBrainz RELEASE id, not a release-GROUP id.
- POST /playlists/<id>/sync with body
    { playlist_name, tracks, image_url?, sync_mode? }
  STARTS an async sync in SoulSync (this is how the Companion
  creates/updates a playlist). Track objects are Spotify-shaped:
    { id, name, artists: [{name}] | [str], album: {name, images:[{url}]} | str,
      duration_ms, image_url }
  sync_mode "replace" deletes the server playlist and recreates it from
  the payload; when sync_mode is OMITTED SoulSync resolves it to its
  configured default "reconcile" (api/source_playlists.py
  start_playlist_sync_from_payload), so the node always sends "replace".
- GET /playlists and /playlists/<id> list Spotify/Tidal SOURCE playlists
  only — not the server playlists a sync creates — so the node tracks the
  playlist ids it manages in its own state.

HARD RULE: this client is the node's ONLY way at SoulSync data.
The node never opens SoulSync's database.
"""
from __future__ import annotations

from typing import Any

import requests


class SoulSyncError(Exception):
    def __init__(self, message: str, code: str = "SOULSYNC_ERROR", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class SoulSyncClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: int = 15, session: requests.Session | None = None):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        # Paths whose latest paged read stopped at the page cap while the
        # server still had more: callers must surface this, never undercount
        # silently.
        self.truncated_paths: set[str] = set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            res = self.session.get(
                f"{self.base}{path}", params=params or {}, headers=self._headers(), timeout=self.timeout_s
            )
        except requests.RequestException as err:
            raise SoulSyncError(f"SoulSync unreachable at {path}: {err}", code="UNREACHABLE") from err
        try:
            body = res.json()
        except ValueError as err:
            raise SoulSyncError(f"SoulSync {path} returned non-JSON (HTTP {res.status_code})", code="BAD_RESPONSE", status=res.status_code) from err
        if not res.ok or not body.get("success") or body.get("data") is None:
            error = body.get("error") or {}
            code = error.get("code") or f"HTTP_{res.status_code}"
            message = error.get("message") or f"HTTP {res.status_code}"
            raise SoulSyncError(f"SoulSync {path} failed: {code}: {message}", code=code, status=res.status_code)
        return body

    def get_all_pages(self, path: str, key: str, max_pages: int = 5000, on_page=None) -> list[dict[str, Any]]:
        self.truncated_paths.discard(path)
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            env = self.get(path, {"page": page, "limit": 200})
            out.extend((env["data"] or {}).get(key) or [])
            pagination = env.get("pagination") or {}
            if on_page is not None:
                # Live progress for long reads: (page, items so far, total pages if known).
                on_page(page, len(out), pagination.get("total_pages"))
            if not pagination.get("has_next"):
                return out
            if page >= max_pages:
                self.truncated_paths.add(path)
                return out
            page += 1

    def list_watchlist(self) -> list[dict[str, Any]]:
        env = self.get("/watchlist")
        return (env["data"] or {}).get("artists") or []

    def list_wishlist(self, max_pages: int = 5000, on_page=None) -> list[dict[str, Any]]:
        return self.get_all_pages("/wishlist", "tracks", max_pages, on_page=on_page)

    def list_albums(self, max_pages: int = 5000, on_page=None) -> list[dict[str, Any]]:
        return self.get_all_pages("/library/albums", "albums", max_pages, on_page=on_page)

    def test_connection(self) -> dict[str, Any]:
        """Cheap read-only probe used by the UI's Test button."""
        env = self.get("/watchlist")
        artists = (env["data"] or {}).get("artists") or []
        return {"ok": True, "watchlist_artists": len(artists)}

    def sync_playlist(self, playlist_id: str, playlist_name: str, tracks: list[dict[str, Any]], sync_mode: str = "replace") -> None:
        """Start a playlist sync in SoulSync. Always explicit sync_mode."""
        from urllib.parse import quote

        url = f"{self.base}/playlists/{quote(playlist_id, safe='')}/sync"
        try:
            res = self.session.post(
                url,
                json={"playlist_name": playlist_name, "tracks": tracks, "image_url": "", "sync_mode": sync_mode},
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as err:
            raise SoulSyncError(f"SoulSync unreachable at playlist sync: {err}", code="UNREACHABLE") from err
        body: dict[str, Any] = {}
        try:
            body = res.json()
        except ValueError:
            pass
        if not res.ok or (body and not body.get("success")):
            error = body.get("error") or {}
            code = error.get("code") or f"HTTP_{res.status_code}"
            message = error.get("message") or f"HTTP {res.status_code}"
            raise SoulSyncError(f"Playlist sync {playlist_id} failed: {code}: {message}", code=code, status=res.status_code)

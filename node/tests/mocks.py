"""Mock SoulSync / pool / MusicBrainz servers (stdlib http.server, threaded).

Fixtures reproduce the REAL v1 serializer shapes verified against
SoulSync dev head bd840487 (see metasync_node/soulsync.py): envelope
{success, data, error, pagination}, watchlist artists with
musicbrainz_artist_id + date_added, wishlist tracks with track_data +
date_added (NO MusicBrainz field — the known GAP-1), library albums with
musicbrainz_release_id + created_at.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ART_A = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ART_NEW = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
ART_OLD = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
REL_A = "ffffffff-ffff-4fff-8fff-ffffffffffff"
REL_B = "12121212-1212-4121-8121-121212121212"
REL_UNKNOWN = "99999999-9999-4999-8999-999999999999"

CANONICAL_PLAYLISTS = {
    "week": "2026-W43",
    "playlists": [
        {"key": "trending", "name": "SIN · Trending in SoulSync", "tracks": [
            {"id": "rec-1", "name": "Song One", "artists": [{"name": "Fixture Artist"}],
             "album": {"name": "Fixture Album", "images": [{"url": "https://coverartarchive.org/release-group/x/front-250"}]},
             "duration_ms": 201000, "image_url": ""},
            {"id": "rec-2", "name": "Song Two", "artists": [{"name": "Fixture Artist"}],
             "album": {"name": "Fixture Album", "images": [{"url": ""}]}, "duration_ms": 187000, "image_url": ""},
        ]},
        {"key": "most-wanted", "name": "SIN · Most Wanted", "tracks": [
            {"id": "rec-3", "name": "Other Song", "artists": [{"name": "Fixture Artist"}],
             "album": {"name": "Second Album", "images": [{"url": ""}]}, "duration_ms": 190000, "image_url": ""},
        ]},
        {"key": "breakouts", "name": "SIN · Breakout Artists", "tracks": []},
    ],
}

SIN_INDEX = {
    "trending": {"week": "2026-W43", "items": [{"mbid": RG_A, "addsThisWeek": 57, "score": 7.4, "reason": "57 adds this week"}]},
    "most-wanted": {"week": "2026-W43", "items": [{"mbid": RG_B, "nodeCount": 31, "huntDemand": 12, "reason": "on 31 nodes' wishlists"}]},
    "breakouts": {"week": "2026-W43", "items": [{"mbid": ART_A, "nodeCount": 22, "previousNodeCount": 4, "growth": 4.5, "reason": "watchlisted by 22 nodes"}]},
}

WATCHLIST = [
    {"id": 1, "source": "manual", "musicbrainz_artist_id": ART_NEW, "image_url": None, "last_synced": None,
     "external_ids": {}, "artist_name": "New Artist", "date_added": "2026-10-20T10:00:00", "created_at": "2026-10-20T10:00:00"},
    {"id": 2, "source": "manual", "musicbrainz_artist_id": ART_OLD, "image_url": None, "last_synced": None,
     "external_ids": {}, "artist_name": "Old Artist", "date_added": "2026-10-05T10:00:00", "created_at": "2026-10-05T10:00:00"},
    {"id": 3, "source": "manual", "musicbrainz_artist_id": None, "image_url": None, "last_synced": None,
     "external_ids": {}, "artist_name": "No MBID Artist", "date_added": "2026-10-21T10:00:00", "created_at": "2026-10-21T10:00:00"},
    {"id": 4, "source": "manual", "musicbrainz_artist_id": ART_NEW, "image_url": None, "last_synced": None,
     "external_ids": {}, "artist_name": "New Artist", "date_added": "2026-10-21T11:00:00", "created_at": "2026-10-21T11:00:00"},
]

WISHLIST = [
    {"id": 1, "track_id": "sp1", "track_name": "Spotify Song", "artist_name": "Someone", "album_name": "Some Album",
     "track_data": {"id": "sp1", "name": "Spotify Song", "artists": [{"name": "Someone"}], "album": {"name": "Some Album"}},
     "date_added": "2026-10-20T09:00:00", "last_attempted": None},
    {"id": 2, "track_id": "mb1", "track_name": "Wanted Song", "artist_name": "Fixture Artist", "album_name": "Fixture Album",
     "track_data": {"name": "Wanted Song", "musicbrainz_release_group_id": RG_A},
     "date_added": "2026-10-20T09:30:00", "last_attempted": None},
    {"id": 3, "track_id": "mb2", "track_name": "Hunted Song", "artist_name": "Fixture Artist", "album_name": "Fixture Album",
     "track_data": {"name": "Hunted Song", "musicbrainz_release_group_id": RG_A},
     "date_added": "2026-10-12T09:30:00", "last_attempted": "2026-10-21T09:30:00"},
    {"id": 4, "track_id": "mb3", "track_name": "Fresh Want", "artist_name": "Other", "album_name": "Second Album",
     "track_data": {"name": "Fresh Want", "musicbrainz_release_group_id": RG_B},
     "date_added": "2026-10-19T09:30:00", "last_attempted": None},
]

ALBUMS = [
    {"id": 1, "title": "Fixture Album", "album_artist_id": 1, "created_at": "2026-09-01T08:00:00",
     "file_path": "/music/fixture", "track_count": 10, "total_duration": 2000, "musicbrainz_release_id": REL_A},
    {"id": 2, "title": "New Import", "album_artist_id": 1, "created_at": "2026-10-20T08:00:00",
     "file_path": "/music/new", "track_count": 8, "total_duration": 1600, "musicbrainz_release_id": REL_A},
    {"id": 3, "title": "Untagged Rip", "album_artist_id": 2, "created_at": "2026-09-01T08:00:00",
     "file_path": "/music/rip", "track_count": 5, "total_duration": 900, "musicbrainz_release_id": None},
    {"id": 4, "title": "Obscure Pressing", "album_artist_id": 2, "created_at": "2026-09-01T08:00:00",
     "file_path": "/music/obscure", "track_count": 5, "total_duration": 900, "musicbrainz_release_id": REL_UNKNOWN},
]


def _envelope(data, pagination=None):
    return {"success": True, "data": data, "error": None, "pagination": pagination}


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep tests quiet
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body_raw(self):
        if not hasattr(self, "_raw_body_cache"):
            length = int(self.headers.get("Content-Length") or 0)
            self._raw_body_cache = self.rfile.read(length) or b""
        return self._raw_body_cache

    def _body(self):
        return json.loads(self._body_raw() or b"{}")


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class MockSoulSync:
    def __init__(self, watchlist=WATCHLIST, wishlist=WISHLIST, albums=ALBUMS, fail_sync=False, downloads=None):
        outer = self
        self.syncs: list[dict] = []
        self.downloads = downloads

        class Handler(_Base):
            def do_GET(self):
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/api/v1/watchlist":
                    return self._send(200, _envelope({"artists": watchlist}))
                if parsed.path == "/api/v1/library/download-activity":
                    return self._send(200, _envelope({"albums": outer.downloads or []}))
                if parsed.path in ("/api/v1/wishlist", "/api/v1/library/albums"):
                    key = "tracks" if parsed.path.endswith("wishlist") else "albums"
                    all_items = wishlist if key == "tracks" else albums
                    page = int(params.get("page", 1))
                    limit = int(params.get("limit", 50))
                    items = all_items[(page - 1) * limit: page * limit]
                    total_pages = max(1, -(-len(all_items) // limit))
                    return self._send(200, _envelope({key: items}, {
                        "page": page, "limit": limit, "total": len(all_items),
                        "total_pages": total_pages, "has_next": page < total_pages, "has_prev": page > 1,
                    }))
                return self._send(404, {"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "nope"}, "pagination": None})

            def do_POST(self):
                parsed = urlparse(self.path)
                if "/playlists/" in parsed.path and parsed.path.endswith("/sync"):
                    playlist_id = parsed.path.split("/playlists/")[1].split("/sync")[0]
                    body = self._body()
                    if fail_sync:
                        return self._send(409, {"success": False, "data": None, "error": {"code": "SYNC_IN_PROGRESS", "message": "A sync is already in progress"}, "pagination": None})
                    outer.syncs.append({"playlist_id": playlist_id, "body": body})
                    return self._send(200, _envelope({"playlist_id": playlist_id, "status": "started"}))
                return self._send(404, {"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "nope"}, "pagination": None})

        self.server, self.url = _serve(Handler)

    def close(self):
        self.server.shutdown()


class MockPool:
    """SIN-shaped pool. playlists_endpoint toggles GET/POST /v1/playlists.

    ``exchange_mode="legacy"`` (the default) 404s the exchange endpoint,
    preserving the pre-§3A direct-key contract used by the original suite.
    ``exchange_mode="token"`` implements the exchange: it captures every
    request's headers + raw body, can answer 202 for the first
    ``pending_exchanges`` exchange calls, then issues short-lived bearer
    tokens and requires them (plus node-signature headers) afterwards.
    """

    def __init__(
        self,
        index=SIN_INDEX,
        playlists=CANONICAL_PLAYLISTS,
        playlists_endpoint=True,
        exchange_mode: str = "legacy",
        pending_exchanges: int = 0,
        token_ttl_s: float = 3600,
    ):
        outer = self
        self.reports: list[dict] = []
        self.published: list[dict] = []
        self.aggregate_calls = 0
        self.hits: list[str] = []
        self.exchange_mode = exchange_mode
        self.pending_exchanges = pending_exchanges
        self.token_ttl_s = token_ttl_s
        self.exchange_calls = 0
        self.issued_tokens: dict[str, float] = {}
        self.request_headers: list[dict] = []
        self.request_bodies: list[bytes] = []

        class Handler(_Base):
            def _record(self):
                outer.request_headers.append({
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "headers": dict(self.headers.items()),
                })
                outer.request_bodies.append(self._body_raw() if self.command == "POST" else b"")

            def _node_headers_present(self):
                return all(self.headers.get(name) for name in (
                    "X-Node-Id", "X-Node-Signature", "X-Node-Timestamp", "X-Node-Nonce",
                ))

            def _bearer_valid(self):
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Bearer "):
                    return False
                expiry = outer.issued_tokens.get(auth.removeprefix("Bearer "))
                return expiry is not None and expiry > time.time()

            def _token_gate(self):
                return outer.exchange_mode != "token" or (
                    self._bearer_valid() and self._node_headers_present()
                )

            def do_GET(self):
                parsed = urlparse(self.path)
                outer.hits.append(f"GET {parsed.path}")
                self._record()
                if parsed.path == "/v1/whoami":
                    if not self._token_gate():
                        return self._send(401, {"error": "invalid_token"})
                    return self._send(200, {"ok": True, "node_id": self.headers.get("X-Node-Id")})
                if parsed.path.startswith("/v1/index/"):
                    if not self._token_gate():
                        return self._send(401, {"error": "invalid_token"})
                    kind = parsed.path.rsplit("/", 1)[1]
                    if kind in index:
                        return self._send(200, index[kind])
                if parsed.path == "/v1/playlists":
                    if not playlists_endpoint:
                        return self._send(404, {"error": "not_found"})
                    if not self._token_gate():
                        return self._send(401, {"error": "invalid_token"})
                    return self._send(200, playlists)
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                outer.hits.append(f"POST {parsed.path}")
                self._record()
                if parsed.path == "/v1/auth/exchange":
                    outer.exchange_calls += 1
                    if outer.exchange_mode == "legacy":
                        return self._send(404, {"error": "not_found"})
                    if not self.headers.get("X-SIN-Key") or not self._node_headers_present():
                        return self._send(401, {"error": "invalid_exchange"})
                    if outer.pending_exchanges > 0:
                        outer.pending_exchanges -= 1
                        return self._send(202, {
                            "status": "binding_pending",
                            "node_fingerprint": (self.headers.get("X-Node-Id") or "")[:12],
                        })
                    token = f"pool-token-{outer.exchange_calls}-{secrets.token_hex(4)}"
                    outer.issued_tokens[token] = time.time() + outer.token_ttl_s
                    return self._send(200, {
                        "access_token": token,
                        "expires_in": outer.token_ttl_s,
                        "token_type": "Bearer",
                        "binding_status": "bound",
                        "key_prefix": "sin_live_mock",
                        "node_id": self.headers.get("X-Node-Id"),
                    })
                if parsed.path == "/v1/report":
                    if outer.exchange_mode == "token":
                        if not self._token_gate():
                            return self._send(401, {"error": "invalid_token"})
                    elif not self.headers.get("X-SIN-Key"):
                        return self._send(401, {"error": "invalid_key"})
                    outer.reports.append(self._body())
                    return self._send(201, {"accepted": 1, "duplicatesIgnored": 0, "week": "2026-W43"})
                if parsed.path == "/v1/aggregate":
                    if not self._token_gate():
                        return self._send(401, {"error": "invalid_token"})
                    outer.aggregate_calls += 1
                    self._body()
                    return self._send(200, {"aggregatesWritten": 3, "rawDeleted": 0, "week": "2026-W43"})
                if parsed.path == "/v1/playlists":
                    if not playlists_endpoint:
                        return self._send(404, {"error": "not_found"})
                    if not self._token_gate():
                        return self._send(401, {"error": "invalid_token"})
                    outer.published.append(self._body())
                    return self._send(201, {"stored": 3})
                return self._send(404, {"error": "not_found"})

        self.server, self.url = _serve(Handler)

    def expire_tokens(self):
        for token in list(self.issued_tokens):
            self.issued_tokens[token] = 0

    def close(self):
        self.server.shutdown()


class MockMusicBrainz:
    def __init__(self):
        outer = self
        self.hits: list[str] = []

        def album_body(rg, with_releases):
            body = {"id": rg, "title": "Fixture Album" if rg == RG_A else "Second Album",
                    "artist-credit": [{"name": "Fixture Artist", "artist": {"id": ART_A, "name": "Fixture Artist"}}]}
            if with_releases:
                body["releases"] = [{"id": REL_A if rg == RG_A else REL_B, "status": "Official", "date": "2020-01-01"}]
            return body

        class Handler(_Base):
            def do_GET(self):
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.hits.append(parsed.path)
                inc = params.get("inc", "")
                if parsed.path.startswith("/ws/2/release-group/"):
                    rg = parsed.path.rsplit("/", 1)[1]
                    if rg in (RG_A, RG_B):
                        return self._send(200, album_body(rg, "releases" in inc))
                if parsed.path.startswith("/ws/2/release/"):
                    rel = parsed.path.rsplit("/", 1)[1]
                    if rel == REL_A and "release-groups" in inc:
                        return self._send(200, {"id": REL_A, "release-group": {"id": RG_A}})
                    if rel in (REL_A, REL_B) and "recordings" in inc:
                        a = rel == REL_A
                        tracks = [
                            {"id": "t1" if a else "t3", "title": "Song One" if a else "Other Song", "length": 201000,
                             "recording": {"id": "rec-1" if a else "rec-3", "title": "Song One" if a else "Other Song", "length": 201000},
                             "artist-credit": [{"name": "Fixture Artist"}]},
                            {"id": "t2" if a else "t4", "title": "Song Two" if a else "Another Song", "length": 187000,
                             "recording": {"id": "rec-2" if a else "rec-4", "title": "Song Two" if a else "Another Song", "length": 187000},
                             "artist-credit": [{"name": "Fixture Artist"}]},
                        ]
                        return self._send(200, {"id": rel, "title": "Fixture Album" if a else "Second Album", "media": [{"tracks": tracks}]})
                if parsed.path.startswith("/ws/2/artist/"):
                    artist = parsed.path.rsplit("/", 1)[1]
                    if artist == ART_A:
                        return self._send(200, {"id": ART_A, "name": "Fixture Artist", "release-groups": [
                            {"id": RG_A, "title": "Fixture Album", "primary-type": "Album", "secondary-types": [], "first-release-date": "2020-01-01"},
                            {"id": RG_B, "title": "Older Album", "primary-type": "Album", "secondary-types": [], "first-release-date": "2018-01-01"},
                        ]})
                return self._send(404, {"error": "Not Found"})

        self.server, self.url = _serve(Handler)
        self.url += "/ws/2"

    def close(self):
        self.server.shutdown()

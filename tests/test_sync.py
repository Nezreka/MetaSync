"""End-to-end: walk a stub SoulSync API, produce claims, checkpoint, resume."""

import base64
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync import soulid  # noqa: E402
from metasync.config import Config  # noqa: E402
from metasync.soulsync import SoulSyncClient, SoulSyncError  # noqa: E402
from metasync.store import ClaimStore  # noqa: E402
from metasync.sync import sync_all  # noqa: E402

API_KEY = "sk_test_key"
TITLES = ["Dogs", "Pigs", "Sheep", "Animals Reprise", "Wish"]


def _row(title):
    return {
        "soul_id": soulid.song_key("Pink Floyd", title),
        "album_soul_id": soulid.release_track_key("Pink Floyd", "Animals", title),
        "title": title, "artist_name": "Pink Floyd", "album_title": "Animals",
        "track_number": TITLES.index(title) + 1, "disc_number": 1,
        "duration": 600000, "explicit": False, "year": 1977,
        "spotify_track_id": f"spotify-{title.replace(' ', '')}",
        "spotify_match_status": "matched",
        # Present but unconfirmed — must not be published.
        "deezer_id": "999", "deezer_match_status": None,
        # Must never reach a claim.
        "id": f"pk-{title}", "file_path": f"/music/{title}.flac",
        "genius_lyrics": "secret words",
    }


class StubHandler(BaseHTTPRequestHandler):
    page_size = 2
    requests_seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        StubHandler.requests_seen.append(params)

        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            return self._json(401, {"success": False, "data": None,
                                    "error": {"code": "AUTH_REQUIRED", "message": "no key"}})

        entity = params.get("entity", [""])[0]
        if entity != "track":
            return self._json(200, {"success": True, "error": None,
                                    "data": {"entity": entity, "items": [],
                                             "next_cursor": "", "has_more": False}})

        cursor = params.get("cursor", [""])[0]
        start = int(base64.urlsafe_b64decode(cursor).decode()) if cursor else 0
        chunk = TITLES[start:start + self.page_size]
        next_index = start + len(chunk)
        self._json(200, {
            "success": True, "error": None,
            "data": {
                "entity": "track",
                "items": [_row(t) for t in chunk],
                "next_cursor": base64.urlsafe_b64encode(str(next_index).encode()).decode(),
                "has_more": next_index < len(TITLES),
            },
        })

    def _json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubHandler.requests_seen = []
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Config(soulsync_url=self.url, soulsync_api_key=API_KEY,
                             data_dir=Path(self._tmp.name), page_size=2)

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_walk_imports_every_row_exactly_once(self):
        result = sync_all(self.config, entities=["track"])
        stats = result["entities"]["track"]
        self.assertEqual(stats["rows"], len(TITLES))
        self.assertEqual(stats["claims"], len(TITLES))
        self.assertEqual(result["store"]["claims"]["track"], len(TITLES))

    def test_unconfirmed_ids_are_not_published(self):
        sync_all(self.config, entities=["track"])
        with ClaimStore(self.config.store_path) as store:
            resolved = store.resolve("track", soulid.release_track_key("pinkfloyd", "animals", "dogs"))
            self.assertIn("spotify", resolved)
            self.assertNotIn("deezer", resolved)

    def test_nothing_local_leaks_into_the_store(self):
        sync_all(self.config, entities=["track"])
        with ClaimStore(self.config.store_path) as store:
            blob = "".join(row[0] for row in store._conn.execute("SELECT payload FROM claims"))
        for secret in ("file_path", "/music/", "secret words", "pk-Dogs"):
            self.assertNotIn(secret, blob)

    def test_cursor_is_checkpointed_and_resumed(self):
        sync_all(self.config, entities=["track"])
        with ClaimStore(self.config.store_path) as store:
            self.assertNotEqual(store.get_cursor("track"), "")

        StubHandler.requests_seen = []
        sync_all(self.config, entities=["track"])
        # Resuming from the end cursor means one confirming request, not a
        # second full walk.
        self.assertEqual(len(StubHandler.requests_seen), 1)

    def test_full_flag_rewalks(self):
        sync_all(self.config, entities=["track"])
        StubHandler.requests_seen = []
        result = sync_all(self.config, entities=["track"], resume=False)
        self.assertEqual(result["entities"]["track"]["rows"], len(TITLES))

    def test_resync_does_not_inflate_observer_counts(self):
        """The same node syncing twice is still one observer."""
        sync_all(self.config, entities=["track"])
        sync_all(self.config, entities=["track"], resume=False)
        with ClaimStore(self.config.store_path) as store:
            resolved = store.resolve("track", soulid.release_track_key("pinkfloyd", "animals", "dogs"))
            self.assertEqual(resolved["spotify"][0]["observers"], 1)
            self.assertEqual(store.stats()["claims"]["track"], len(TITLES))

    def test_signatures_survive_the_round_trip(self):
        sync_all(self.config, entities=["track"])
        with ClaimStore(self.config.store_path) as store:
            checked, failures = store.verify_all()
        self.assertEqual(checked, len(TITLES))
        self.assertEqual(failures, [])

    def test_bad_api_key_is_reported_clearly(self):
        client = SoulSyncClient(self.url, "sk_wrong")
        with self.assertRaises(SoulSyncError) as ctx:
            client.export_page("track")
        self.assertIn("401", str(ctx.exception))

    def test_missing_api_key_is_caught_before_any_request(self):
        config = Config(soulsync_url=self.url, soulsync_api_key="",
                        data_dir=Path(self._tmp.name))
        with self.assertRaises(RuntimeError) as ctx:
            sync_all(config, entities=["track"])
        self.assertIn("SOULSYNC_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ResilienceTests(unittest.TestCase):
    """A walk of a large library takes minutes, and SoulSync restarts during
    minutes. Riding that out is a requirement, not a nicety."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_connection_refused_is_retried_then_reported_clearly(self):
        import metasync.soulsync as soulsync_module
        client = SoulSyncClient("http://127.0.0.1:1", API_KEY, timeout=2)
        client.MAX_CONNECT_RETRIES = 2
        original_sleep = soulsync_module.time.sleep
        soulsync_module.time.sleep = lambda _: None   # do not actually wait
        try:
            with self.assertRaises(SoulSyncError) as ctx:
                client.export_page("track")
        finally:
            soulsync_module.time.sleep = original_sleep
        self.assertIn("Is it running?", str(ctx.exception))

    def test_transient_failure_then_success(self):
        """The first call fails as if mid-restart; the retry succeeds."""
        import metasync.soulsync as soulsync_module
        client = SoulSyncClient(self.url, API_KEY, timeout=5)
        calls = {"n": 0}
        real = client._get_once

        def flaky(path, params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise soulsync_module._Unreachable("connection refused")
            return real(path, params)

        client._get_once = flaky
        original_sleep = soulsync_module.time.sleep
        soulsync_module.time.sleep = lambda _: None
        try:
            page = client.export_page("track", limit=2)
        finally:
            soulsync_module.time.sleep = original_sleep
        self.assertEqual(calls["n"], 2)
        self.assertTrue(page["items"])

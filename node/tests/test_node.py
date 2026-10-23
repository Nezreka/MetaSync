"""MetaSync node tests — everything against MOCK servers on localhost."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.config import Config  # noqa: E402
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from metasync_node.identity import NodeIdentity, canonical_request_string, load_or_create_identity, token_for_week  # noqa: E402
from metasync_node.musicbrainz import MusicBrainzResolver  # noqa: E402
from metasync_node.pool import PoolClient  # noqa: E402
from metasync_node.service import NodeService  # noqa: E402
from metasync_node.signals import compute_signals  # noqa: E402
from metasync_node.soulsync import SoulSyncClient  # noqa: E402
from metasync_node.store import Store  # noqa: E402
from metasync_node.weekutil import iso_week_string  # noqa: E402
from tests.mocks import (  # noqa: E402
    ALBUMS, ART_NEW, CANONICAL_PLAYLISTS, MockMusicBrainz, MockPool, MockSoulSync,
    RG_A, RG_B, REL_A, WATCHLIST, WISHLIST,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)  # Thursday of 2026-W43


def make_config(state_dir: str, **overrides) -> Config:
    config = Config(state_dir=state_dir, musicbrainz_min_interval_s=0.0, http_timeout_s=5)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class ServiceCase(unittest.TestCase):
    """Base: fresh state dir + mock SoulSync + mock pool + mock MusicBrainz."""

    pool_role = "node"
    pool_playlists_endpoint = True
    pool_exchange_mode = "legacy"
    pool_pending_exchanges = 0
    pool_token_ttl_s = 3600
    fail_sync = False
    dry_run = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.soulsync = MockSoulSync(fail_sync=self.fail_sync)
        self.pool = MockPool(
            playlists_endpoint=self.pool_playlists_endpoint,
            exchange_mode=self.pool_exchange_mode,
            pending_exchanges=self.pool_pending_exchanges,
            token_ttl_s=self.pool_token_ttl_s,
        )
        self.mb = MockMusicBrainz()
        self.addCleanup(self.soulsync.close)
        self.addCleanup(self.pool.close)
        self.addCleanup(self.mb.close)
        self.config = make_config(self.tmp.name, musicbrainz_base_url=self.mb.url)
        self.store = Store(self.tmp.name)
        self.store.settings["soulsync"] = {"url": self.soulsync.url, "api_key": "sk_test"}
        self.store.settings["pools"] = [{
            "id": "sin", "name": "SIN", "url": self.pool.url,
            "api_key": "pool-key", "role": self.pool_role, "enabled": True,
        }]
        self.store.settings["dry_run"] = self.dry_run
        self.store.save_settings()
        self.service = NodeService(self.config, store=self.store)

    def pool_dict(self):
        return self.store.settings["pools"][0]


class TestWeekAndIdentity(unittest.TestCase):
    def test_iso_week_known_dates(self):
        self.assertEqual(iso_week_string(datetime(2026, 10, 22, tzinfo=timezone.utc)), "2026-W43")
        self.assertEqual(iso_week_string(datetime(2026, 1, 1, tzinfo=timezone.utc)), "2026-W01")
        self.assertEqual(iso_week_string(datetime(2025, 12, 29, tzinfo=timezone.utc)), "2026-W01")
        self.assertEqual(iso_week_string(datetime(2026, 10, 26, tzinfo=timezone.utc)), "2026-W44")

    def test_token_rotates_weekly_and_identity_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            id1 = load_or_create_identity(tmp)
            id2 = load_or_create_identity(tmp)
            self.assertEqual(id1.key, id2.key)
            self.assertEqual(len(id1.fingerprint), 12)
            t43 = token_for_week(id1, "2026-W43")
            self.assertEqual(t43, token_for_week(id1, "2026-W43"))
            self.assertNotEqual(t43, token_for_week(id1, "2026-W44"))
            self.assertEqual(len(t43), 64)


class TestSignals(unittest.TestCase):
    def test_all_four_types_binary_rule_and_gaps(self):
        result = compute_signals(
            watchlist=WATCHLIST, wishlist=WISHLIST, albums=ALBUMS,
            release_to_group={REL_A: RG_A}, now=NOW,
        )
        self.assertEqual(result["week"], "2026-W43")
        keys = sorted(f"{s['type']}:{s['mbid']}" for s in result["signals"])
        self.assertEqual(keys, sorted([
            f"hunt_demand:{RG_A}", f"import_keep:{RG_A}", f"watchlist_add:{ART_NEW}",
            f"wishlist_add:{RG_A}", f"wishlist_add:{RG_B}",
        ]))
        self.assertEqual(len([s for s in result["signals"] if s["type"] == "watchlist_add"]), 1)
        gaps = result["diagnostics"]["gaps"]
        self.assertEqual(gaps, {
            "watchlist_no_mbid": 1, "wishlist_no_mbid": 1,
            "albums_no_release_mbid": 1, "albums_unresolved_group": 1,
        })


class TestReportFlow(ServiceCase):
    def test_pagination_followed_and_report_posted(self):
        many = [{"id": 100 + i, "track_id": f"b{i}", "track_name": f"Bulk {i}", "artist_name": "B",
                 "album_name": "B", "track_data": {"id": f"b{i}"},
                 "date_added": "2026-10-20T00:00:00", "last_attempted": None} for i in range(210)]
        self.soulsync.close()
        self.soulsync = MockSoulSync(wishlist=many)
        self.addCleanup(self.soulsync.close)
        self.store.settings["soulsync"] = {"url": self.soulsync.url, "api_key": "sk_test"}
        outcome = self.service.run_report(NOW)
        self.assertIsNone(outcome["error"])
        self.assertEqual(outcome["diagnostics"]["wishlist_seen"], 210)
        self.assertEqual(len(self.pool.reports), 1)
        report = self.pool.reports[0]
        self.assertEqual(report["week"], "2026-W43")
        self.assertEqual(len(report["token"]), 64)
        self.assertTrue(outcome["pools"]["sin"]["sent"])
        self.assertEqual(self.store.state["last_report"]["sin"]["week"], "2026-W43")

    def test_envelope_error_raises(self):
        client = SoulSyncClient("http://127.0.0.1:1", "sk_x", timeout_s=1)
        with self.assertRaises(Exception):
            client.list_watchlist()


class TestDryRun(ServiceCase):
    dry_run = True

    def test_dry_run_previews_and_sends_nothing(self):
        outcome = self.service.run_report(NOW)
        self.assertIsNone(outcome["error"])
        self.assertGreater(outcome["signal_count"], 0)
        self.assertEqual(self.pool.reports, [])
        self.assertFalse(outcome["pools"]["sin"]["sent"])
        self.assertEqual(self.store.state["previews"]["sin"]["week"], "2026-W43")
        self.assertEqual(self.store.state.get("last_report"), {})

    def test_dry_run_refresh_syncs_nothing(self):
        outcome = self.service.run_refresh(NOW)
        self.assertEqual(self.soulsync.syncs, [])
        self.assertTrue(all(not p["synced"] or p["skipped_unchanged"] for p in outcome["pools"]["sin"]["playlists"]))


class TestFailureIsolation(ServiceCase):
    def test_pool_down_records_error_and_state_stays_valid(self):
        self.store.settings["pools"][0]["url"] = "http://127.0.0.1:1"
        outcome = self.service.run_report(NOW)
        self.assertFalse(outcome["pools"]["sin"]["sent"])
        self.assertTrue(outcome["pools"]["sin"]["error"])
        on_disk = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertTrue(on_disk["last_errors"])
        self.assertTrue(self.service.status()["ok"])

    def test_soulsync_down_report_fails_soft(self):
        self.store.settings["soulsync"] = {"url": "http://127.0.0.1:1", "api_key": "sk_x"}
        outcome = self.service.run_report(NOW)
        self.assertTrue(outcome["error"])
        self.assertTrue(self.service.status()["ok"])


class TestPermissionModel(ServiceCase):
    def test_node_role_refuses_admin_operations_before_any_http(self):
        client = PoolClient(self.pool_dict())
        hits_before = list(self.pool.hits)
        with self.assertRaises(PermissionError):
            client.publish_playlists("2026-W43", [])
        with self.assertRaises(PermissionError):
            client.post_aggregate()
        self.assertEqual(self.pool.hits, hits_before, "no HTTP request may be made for admin ops on a node pool")
        self.assertEqual(self.pool.aggregate_calls, 0)
        self.assertEqual(self.pool.published, [])


class TestUserRefresh(ServiceCase):
    def test_canonical_playlists_materialize_with_stable_ids_and_dev_payload_shape(self):
        outcome = self.service.run_refresh(NOW)
        entry = outcome["pools"]["sin"]
        self.assertEqual(entry["status"], "ok")
        by_id = {s["playlist_id"]: s["body"] for s in self.soulsync.syncs}
        self.assertIn("metasync_sin_trending", by_id)
        self.assertIn("metasync_sin_most-wanted", by_id)
        body = by_id["metasync_sin_trending"]
        self.assertEqual(body["playlist_name"], "SIN · Trending in SoulSync")
        self.assertEqual(body["sync_mode"], "replace")  # explicit; SoulSync's default would be reconcile
        track = body["tracks"][0]
        self.assertIsInstance(track["id"], str)
        self.assertIsInstance(track["artists"][0]["name"], str)
        self.assertIn("images", track["album"])
        self.assertIsInstance(track["duration_ms"], int)
        # breakouts canonical playlist is empty -> never synced (would wipe).
        self.assertNotIn("metasync_sin_breakouts", by_id)

    def test_unchanged_skips_and_changed_replaces_same_id(self):
        self.service.run_refresh(NOW)
        count = len(self.soulsync.syncs)
        second = self.service.run_refresh(NOW)
        self.assertEqual(len(self.soulsync.syncs), count)
        self.assertTrue(all(p["skipped_unchanged"] for p in second["pools"]["sin"]["playlists"] if p["track_count"]))

        changed = json.loads(json.dumps(CANONICAL_PLAYLISTS))
        changed["playlists"][0]["tracks"] = [dict(changed["playlists"][0]["tracks"][0], id="rec-9", name="New Song")]
        self.pool.close()
        self.pool = MockPool(playlists=changed)
        self.addCleanup(self.pool.close)
        self.store.settings["pools"][0]["url"] = self.pool.url
        self.service.run_refresh(NOW)
        trending = [s for s in self.soulsync.syncs if s["playlist_id"] == "metasync_sin_trending"]
        self.assertEqual(len(trending), 2)
        self.assertEqual(trending[1]["body"]["tracks"][0]["name"], "New Song")


class TestUserRefreshWithoutPoolPlaylists(ServiceCase):
    pool_playlists_endpoint = False

    def test_missing_playlist_endpoint_is_an_honest_status_not_a_fake_sync(self):
        outcome = self.service.run_refresh(NOW)
        entry = outcome["pools"]["sin"]
        self.assertEqual(entry["status"], "playlist_data_unavailable")
        self.assertEqual(self.soulsync.syncs, [])
        self.assertTrue(self.service.status()["ok"])


class TestAdminFlow(ServiceCase):
    pool_role = "admin"

    def test_admin_builds_definitions_publishes_and_materializes(self):
        outcome = self.service.run_refresh(NOW)
        entry = outcome["pools"]["sin"]
        self.assertEqual(entry["status"], "published")
        self.assertEqual(self.pool.published.__len__(), 1)
        published = self.pool.published[0]
        self.assertEqual(published["week"], "2026-W43")
        by_key = {p["key"]: p for p in published["playlists"]}
        trending_tracks = by_key["trending"]["tracks"]
        self.assertEqual([t["name"] for t in trending_tracks], ["Song One", "Song Two"])
        self.assertEqual(trending_tracks[0]["artists"][0]["name"], "Fixture Artist")
        breakout_tracks = by_key["breakouts"]["tracks"]
        self.assertEqual([t["name"] for t in breakout_tracks], ["Song One", "Song Two"])  # newest Album RG sampled
        # ...and the official node materializes the same definitions locally.
        self.assertIn("metasync_sin_trending", {s["playlist_id"] for s in self.soulsync.syncs})

    def test_admin_can_trigger_aggregate(self):
        client = PoolClient(self.pool_dict())
        client.post_aggregate()
        self.assertEqual(self.pool.aggregate_calls, 1)


class TestSyncConflictIsolation(ServiceCase):
    fail_sync = True

    def test_409_does_not_fail_other_work_or_the_node(self):
        outcome = self.service.run_refresh(NOW)
        entry = outcome["pools"]["sin"]
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(all(not p["synced"] for p in entry["playlists"]))
        self.assertTrue(any(e["leg"].startswith("refresh:sync:") for e in self.store.state["last_errors"]))
        self.assertTrue(self.service.status()["ok"])


class TestMusicBrainzCaching(ServiceCase):
    def test_repeat_resolution_uses_cache(self):
        resolver = MusicBrainzResolver(self.mb.url, "test-agent", min_interval_s=0.0, cache={})
        resolver.resolve_release_group(RG_A, 3)
        hits = len(self.mb.hits)
        self.assertGreater(hits, 0)
        resolver.resolve_release_group(RG_A, 3)
        self.assertEqual(len(self.mb.hits), hits)


class TestUiFlows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.soulsync = MockSoulSync()
        self.pool = MockPool()
        self.addCleanup(self.soulsync.close)
        self.addCleanup(self.pool.close)
        config = make_config(self.tmp.name, musicbrainz_base_url="http://127.0.0.1:1/ws/2")
        self.store = Store(self.tmp.name)
        self.service = NodeService(config, store=self.store)
        from metasync_node.app import create_app
        self.client = create_app(self.service).test_client()

    def test_setup_login_and_pages(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)  # -> setup
        self.client.post("/setup", data={"password": "test-password", "confirm": "test-password"})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/preview").status_code, 200)
        self.client.get("/logout")
        self.assertEqual(self.client.get("/").status_code, 302)  # -> login
        self.client.post("/login", data={"password": "wrong-password"})
        self.assertEqual(self.client.get("/").status_code, 302)
        self.client.post("/login", data={"password": "test-password"})
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_soulsync_and_pool_settings_are_distinct_and_persist(self):
        self.client.post("/setup", data={"password": "test-password", "confirm": "test-password"})
        self.client.post("/settings/soulsync", data={"url": self.soulsync.url, "api_key": "sk_abc", "action": "test"})
        settings = self.store.settings
        self.assertEqual(settings["soulsync"]["url"], self.soulsync.url)
        self.assertEqual(settings["soulsync"]["api_key"], "sk_abc")
        self.assertEqual(settings["pools"], [])  # connecting SoulSync joined nothing

        self.client.post("/pools", data={"name": "SIN", "url": self.pool.url, "api_key": "pool-key", "role": "node"})
        settings = self.store.settings
        self.assertEqual(len(settings["pools"]), 1)
        self.assertEqual(settings["pools"][0]["role"], "node")
        # Persisted to disk as two distinct sections.
        on_disk = json.loads((Path(self.tmp.name) / "settings.json").read_text())
        self.assertIn("soulsync", on_disk)
        self.assertIn("pools", on_disk)

    def test_preview_page_shows_exact_payload(self):
        self.client.post("/setup", data={"password": "test-password", "confirm": "test-password"})
        self.client.post("/settings/soulsync", data={"url": self.soulsync.url, "api_key": "sk_abc", "action": "save"})
        self.client.post("/pools", data={"name": "SIN", "url": self.pool.url, "api_key": "pool-key", "role": "node"})
        page = self.client.get("/preview")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("2026-W", html)
        self.assertIn("watchlist_add", html)
        self.assertIn(ART_NEW, html)


class TestSigningIdentity(unittest.TestCase):
    def test_hkdf_derivation_public_identity_and_known_canonical_vector(self):
        identity = NodeIdentity(key="11" * 32)
        same = NodeIdentity(key="11" * 32)
        self.assertEqual(identity.signing_seed, same.signing_seed)
        self.assertEqual(identity.public_key_b64url, same.public_key_b64url)
        self.assertEqual(identity.node_id, hashlib.sha256(identity.public_key_bytes).hexdigest())
        self.assertEqual(identity.fingerprint, identity.node_id[:12])
        self.assertEqual(len(identity.fingerprint), 12)

        # Known vector: SHA-256("{}") is the fixed digest in the last line.
        canonical = canonical_request_string(
            "post", "/v1/auth/exchange", 1700000000, "nonce-known", b"{}",
        )
        self.assertEqual(
            canonical,
            "POST\n/v1/auth/exchange\n1700000000\nnonce-known\n"
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        )

        headers = identity.sign_request(
            "POST", "/v1/auth/exchange", b"{}", timestamp=1700000000, nonce="nonce-known",
        )
        self.assertEqual(headers["X-Node-Id"], identity.node_id)
        self.assertEqual(headers["X-Node-Timestamp"], "1700000000")
        self.assertEqual(headers["X-Node-Nonce"], "nonce-known")
        signature = base64.urlsafe_b64decode(
            headers["X-Node-Signature"] + "=" * (-len(headers["X-Node-Signature"]) % 4)
        )
        public_key = identity.signing_public_key
        self.assertEqual(
            public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            identity.public_key_bytes,
        )
        public_key.verify(signature, canonical.encode("utf-8"))  # raises if wrong
        with self.assertRaises(InvalidSignature):
            public_key.verify(signature, canonical.replace("nonce-known", "other-nonce").encode("utf-8"))


class TestExchangeFlow(ServiceCase):
    pool_exchange_mode = "token"

    def _request_to(self, path):
        return [r for r in self.pool.request_headers if r["path"] == path]

    def test_exchange_then_token_report_whoami_and_index(self):
        client = self.service.pool_client(self.pool_dict())
        response = client.post_report({"week": "2026-W43", "token": "t", "signals": []})
        self.assertEqual(response["accepted"], 1)
        self.assertEqual(self.pool.exchange_calls, 1)
        self.assertEqual(len(self.pool.reports), 1)

        exchange = self._request_to("/v1/auth/exchange")[0]["headers"]
        self.assertEqual(exchange.get("X-SIN-Key"), "pool-key")
        self.assertEqual(exchange.get("X-Node-Public-Key"), self.service.identity.public_key_b64url)
        self.assertEqual(exchange.get("X-Node-Id"), self.service.identity.node_id)

        report = self._request_to("/v1/report")[-1]["headers"]
        auth_state = self.store.state["pool_auth"]["sin"]
        self.assertEqual(report.get("Authorization"), f"Bearer {auth_state['access_token']}")
        self.assertNotIn("X-SIN-Key", report)
        for header in ("X-Node-Id", "X-Node-Signature", "X-Node-Timestamp", "X-Node-Nonce"):
            self.assertTrue(report.get(header), header)

        # The token was persisted in the 0600 state store.
        on_disk = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(on_disk["pool_auth"]["sin"]["access_token"], auth_state["access_token"])
        self.assertEqual(on_disk["pool_auth"]["sin"]["binding_status"], "bound")

        self.assertEqual(client.get_whoami()["node_id"], self.service.identity.node_id)
        self.assertEqual(client.get_index("trending")["items"][0]["count"], 57)
        self.assertEqual(self.pool.exchange_calls, 1, "a fresh token must be reused")

    def test_expired_token_triggers_reexchange(self):
        client = self.service.pool_client(self.pool_dict())
        client.post_report({"week": "2026-W43", "token": "t", "signals": []})
        self.assertEqual(self.pool.exchange_calls, 1)
        self.store.state["pool_auth"]["sin"]["token_expires_epoch"] = 0
        client.get_whoami()
        self.assertEqual(self.pool.exchange_calls, 2)

    def test_token_401_clears_and_reexchanges_once(self):
        client = self.service.pool_client(self.pool_dict())
        client.post_report({"week": "2026-W43", "token": "t", "signals": []})
        self.pool.expire_tokens()
        client.post_report({"week": "2026-W43", "token": "t", "signals": []})
        self.assertEqual(self.pool.exchange_calls, 2)
        self.assertEqual(len(self.pool.reports), 2)


class TestLegacyExchangeFallback(ServiceCase):
    def test_404_exchange_falls_back_to_labelled_direct_key_mode(self):
        client = self.service.pool_client(self.pool_dict())
        client.post_report({"week": "2026-W43", "token": "t", "signals": []})
        self.assertEqual(self.pool.exchange_calls, 1)
        self.assertEqual(len(self.pool.reports), 1)
        status = self.service.status()["pools"][0]
        self.assertEqual(status["auth_mode"], "legacy")
        self.assertIn("Legacy direct-key dev fallback", status["status_note"])
        report = [r for r in self.pool.request_headers if r["path"] == "/v1/report"][-1]["headers"]
        self.assertEqual(report.get("X-SIN-Key"), "pool-key")


class TestBindingPending(ServiceCase):
    pool_exchange_mode = "token"
    pool_pending_exchanges = 1

    def test_pending_state_surfaces_and_no_report_is_sent_while_pending(self):
        from metasync_node.app import create_app

        outcome = self.service.run_report(NOW)
        self.assertEqual(outcome["pools"]["sin"]["status"], "binding_pending")
        self.assertFalse(outcome["pools"]["sin"]["sent"])
        self.assertEqual(self.pool.reports, [])

        status = self.service.status()
        pool_status = status["pools"][0]
        self.assertEqual(pool_status["binding_status"], "pending_approval")
        self.assertEqual(pool_status["pending_node_fingerprint"], self.service.identity.fingerprint)

        client = create_app(self.service).test_client()
        client.post("/setup", data={"password": "test-password", "confirm": "test-password"})
        dashboard = client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        html = dashboard.get_data(as_text=True)
        self.assertIn("Waiting for approval in the portal", html)
        self.assertIn(self.service.identity.fingerprint, html)
        pools_page = client.get("/pools")
        self.assertIn("Waiting for approval in the portal", pools_page.get_data(as_text=True))

        # The next exchange discovers the approval and the report can go.
        outcome = self.service.run_report(NOW)
        self.assertTrue(outcome["pools"]["sin"]["sent"])
        self.assertEqual(len(self.pool.reports), 1)
        self.assertEqual(self.service.status()["pools"][0]["binding_status"], "bound")


class TestLocalApiAndPairing(ServiceCase):
    def setUp(self):
        super().setUp()
        from metasync_node.app import create_app
        self.client = create_app(self.service).test_client()
        self.client.post("/setup", data={"password": "test-password", "confirm": "test-password"})

    @staticmethod
    def _auth(raw_key: str):
        return {"Authorization": f"Bearer {raw_key}"}

    def _shown_local_key(self) -> str:
        self.client.post("/local-keys/regenerate")
        html = self.client.get("/").get_data(as_text=True)
        match = re.search(r"mn_local_[A-Za-z0-9_-]+", html)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_local_key_required_shown_once_and_regenerate_kills_old(self):
        self.assertEqual(self.client.get("/local/status").status_code, 401)
        self.assertEqual(self.client.get("/local/status", headers=self._auth("wrong")).status_code, 401)

        first = self._shown_local_key()
        settings_text = (Path(self.tmp.name) / "settings.json").read_text()
        self.assertNotIn(first, settings_text, "the raw local key must never be stored")
        self.assertTrue(self.store.settings["local_api_key_hash"])
        status = self.client.get("/local/status", headers=self._auth(first))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["node_id"], self.service.identity.node_id)
        self.assertNotIn(first, self.client.get("/").get_data(as_text=True), "the key is shown only once")

        second = self._shown_local_key()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get("/local/status", headers=self._auth(first)).status_code, 401)
        self.assertEqual(self.client.get("/local/status", headers=self._auth(second)).status_code, 200)

        self.client.post("/local-keys/revoke")
        self.assertEqual(self.client.get("/local/status", headers=self._auth(second)).status_code, 401)

    def test_handoff_pool_key_exchanges_and_disconnect_clears_it(self):
        token_pool = MockPool(exchange_mode="token")
        self.addCleanup(token_pool.close)
        raw_key = self.service.issue_local_api_key()
        body = {"pool_url": token_pool.url, "pool_api_key": "pool-key", "pool_name": "SIN"}

        self.assertEqual(self.client.post("/local/handoff/pool-key", json=body).status_code, 401)
        response = self.client.post("/local/handoff/pool-key", json=body, headers=self._auth(raw_key))
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["exchange_status"], "bound")
        pool_id = payload["pool"]["id"]
        stored = next(p for p in self.store.settings["pools"] if p["id"] == pool_id)
        self.assertEqual(stored["api_key"], "pool-key")
        access_token = self.store.state["pool_auth"][pool_id]["access_token"]
        self.assertTrue(access_token)
        self.assertNotIn(access_token, response.get_data(as_text=True))

        disconnected = self.client.post(
            "/local/disconnect", json={"pool_id": pool_id}, headers=self._auth(raw_key),
        )
        self.assertEqual(disconnected.status_code, 200)
        self.assertEqual(stored["api_key"], "")
        self.assertEqual(self.store.state["pool_auth"][pool_id], {})

    def test_handoff_accepts_pool_key_spelling(self):
        # The §3A contract names the field pool_key; the node also keeps
        # accepting its original pool_api_key spelling.
        token_pool = MockPool(exchange_mode="token")
        self.addCleanup(token_pool.close)
        raw_key = self.service.issue_local_api_key()
        body = {"pool_url": token_pool.url, "pool_key": "pool-key", "pool_name": "SIN"}
        response = self.client.post("/local/handoff/pool-key", json=body, headers=self._auth(raw_key))
        self.assertEqual(response.status_code, 200)
        pool_id = response.get_json()["pool"]["id"]
        stored = next(p for p in self.store.settings["pools"] if p["id"] == pool_id)
        self.assertEqual(stored["api_key"], "pool-key")

    def test_pairing_happy_path_and_single_use_claim(self):
        request_response = self.client.post("/local/pair/request")
        self.assertEqual(request_response.status_code, 200)
        request_data = request_response.get_json()
        self.assertEqual(len(request_data["pairing_code"]), 6)
        self.assertTrue(request_data["pairing_code"].isdigit())
        self.assertEqual(request_data["node_fingerprint"], self.service.identity.fingerprint)

        claim_body = {
            "session_id": request_data["session_id"],
            "pairing_code": request_data["pairing_code"],
            "soulsync_name": "Home SoulSync",
            "soulsync_url": "http://soulsync.local:8000",
            "soulsync_api_key": "sk_pairing",
        }
        self.assertEqual(self.client.post("/local/pair/claim", json=claim_body).status_code, 401)

        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn(request_data["pairing_code"], dashboard)
        self.assertIn("Approve pairing", dashboard)
        confirm = self.client.post(
            "/local/pair/confirm",
            data={"session_id": request_data["session_id"], "decision": "approve"},
        )
        self.assertEqual(confirm.status_code, 302)

        claim = self.client.post("/local/pair/claim", json=claim_body)
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.get_json()
        self.assertTrue(claim_data["local_api_key"].startswith("mn_local_"))
        self.assertEqual(claim_data["node_id"], self.service.identity.node_id)
        self.assertEqual(self.store.settings["soulsync"]["api_key"], "sk_pairing")
        self.assertEqual(self.store.settings["soulsync"]["url"], "http://soulsync.local:8000")
        self.assertEqual(
            self.client.get("/local/status", headers=self._auth(claim_data["local_api_key"])).status_code,
            200,
        )
        self.assertEqual(self.client.post("/local/pair/claim", json=claim_body).status_code, 401)

    def test_pairing_wrong_code_and_expired_claim_fail_generically(self):
        request_data = self.client.post("/local/pair/request").get_json()
        self.client.post(
            "/local/pair/confirm",
            data={"session_id": request_data["session_id"], "decision": "approve"},
        )
        wrong_code = "000000" if request_data["pairing_code"] != "000000" else "111111"
        claim_body = {
            "session_id": request_data["session_id"],
            "pairing_code": wrong_code,
            "soulsync_name": "Home",
            "soulsync_url": "http://soulsync.local:8000",
            "soulsync_api_key": "sk_nope",
        }
        response = self.client.post("/local/pair/claim", json=claim_body)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"ok": False, "error": "unauthorized"})
        self.assertNotEqual(self.store.settings["soulsync"].get("api_key"), "sk_nope")

        session = self.store.state["pairing_session"]
        session["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.store.save_state()
        claim_body["pairing_code"] = request_data["pairing_code"]
        self.assertEqual(self.client.post("/local/pair/claim", json=claim_body).status_code, 401)


if __name__ == "__main__":
    unittest.main()

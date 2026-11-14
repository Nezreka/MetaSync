"""Regression tests for the November 14, 2026 review fixes (node side)."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

import run as node_run  # noqa: E402
from metasync_node.config import Config  # noqa: E402
from metasync_node.identity import load_or_create_identity  # noqa: E402
from metasync_node.meta import builder as B  # noqa: E402
from metasync_node.meta import claims as C  # noqa: E402
from metasync_node.meta import soulid as S  # noqa: E402
from metasync_node.meta import verify as V  # noqa: E402
from metasync_node.meta.store import MetaStore  # noqa: E402
from metasync_node.relay import WIRE_CHUNK_BYTES  # noqa: E402
from metasync_node.service import NodeService  # noqa: E402
from metasync_node.store import Store  # noqa: E402
from tests.mocks import MockPool, MockSoulSync, browser_client  # noqa: E402


def make_config(state_dir: str) -> Config:
    config = Config(state_dir=state_dir, musicbrainz_min_interval_s=0.0, http_timeout_s=5)
    config.musicbrainz_base_url = "http://127.0.0.1:1/ws/2"
    return config


class TestEnvSwitchesApplyOnce(unittest.TestCase):
    """An env switch is a starting value, not a lock on the dashboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def boot(self, env):
        return NodeService(make_config(self.tmp.name), store=Store(self.tmp.name), env=env)

    def test_start_sending_survives_a_restart_with_dry_run_in_env(self):
        env = {"METASYNC_DRY_RUN": "true"}
        service = self.boot(env)
        self.assertTrue(service.dry_run)
        service.store.settings["dry_run"] = False  # the dashboard's Start sending
        service.store.save_settings()
        self.assertFalse(self.boot(env).dry_run)

    def test_listening_opt_out_survives_a_restart(self):
        env = {"METASYNC_SHARE_LISTENING": "true", "METASYNC_SHARE_CHURN": "true"}
        service = self.boot(env)
        self.assertTrue(service.store.settings["share_listening"])
        self.assertTrue(service.store.settings["share_churn"])
        service.store.settings["share_listening"] = False
        service.store.settings["share_churn"] = False
        service.store.save_settings()
        again = self.boot(env)
        self.assertFalse(again.store.settings["share_listening"])
        self.assertFalse(again.store.settings["share_churn"])

    def test_a_changed_env_value_is_applied_again(self):
        self.boot({"METASYNC_DRY_RUN": "true"})
        self.assertFalse(self.boot({"METASYNC_DRY_RUN": "false"}).dry_run)
        # ... and going back to the first value counts as a change too.
        self.assertTrue(self.boot({"METASYNC_DRY_RUN": "true"}).dry_run)

    def test_removing_and_restoring_an_env_value_applies_it_again(self):
        env = {"METASYNC_DRY_RUN": "true"}
        service = self.boot(env)
        service.store.settings["dry_run"] = False
        service.store.save_settings()
        # A real environment is never empty; the switch is simply absent.
        self.assertFalse(self.boot({"PATH": "/usr/bin"}).dry_run)
        self.assertTrue(self.boot(env).dry_run)

    def test_legacy_dry_run_name_still_works(self):
        self.assertFalse(self.boot({"DRY_RUN": "false"}).dry_run)


class TestSchedulerSurvivesSaveFailure(unittest.TestCase):
    def test_a_failing_save_in_the_error_path_does_not_end_the_loop(self):
        ticks = []
        stop = threading.Event()

        class BrokenStore:
            state = {}

            def record_error(self, leg, err):
                pass

            def save_state(self):
                raise OSError(28, "No space left on device")

        class Service:
            store = BrokenStore()

            class config:  # noqa: N801 - mirrors service.config
                scheduler_tick_s = 0
                refresh_interval_h = 24

            def sync_relay_client(self):
                raise RuntimeError("relay leg failed")

            def sync_meta_store(self):
                raise RuntimeError("meta leg failed")

            def report_job(self):
                ticks.append(1)
                if len(ticks) >= 3:
                    stop.set()
                raise RuntimeError("report leg failed")

        thread = threading.Thread(target=node_run.scheduler_loop, args=(Service(), stop), daemon=True)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        # Three full ticks ran: the loop outlived every failing save.
        self.assertEqual(len(ticks), 3)


class TestStoredKeysDoNotFollowAUrlChange(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.soulsync = MockSoulSync()
        self.pool = MockPool()
        self.addCleanup(self.soulsync.close)
        self.addCleanup(self.pool.close)
        self.store = Store(self.tmp.name)
        self.service = NodeService(make_config(self.tmp.name), store=self.store)
        from metasync_node.app import create_app

        self.client = browser_client(create_app(self.service))
        self.client.post("/setup", data={"password": "test-password", "confirm": "test-password"})

    def test_soulsync_key_is_not_sent_to_a_new_address(self):
        self.client.post("/settings/soulsync", data={"url": self.soulsync.url, "api_key": "sk_secret", "action": "save"})
        self.assertEqual(self.store.settings["soulsync"]["api_key"], "sk_secret")
        # New address, blank key field: refused, nothing changes.
        self.client.post("/settings/soulsync", data={"url": "http://127.0.0.1:9/elsewhere", "api_key": "", "action": "test"})
        self.assertEqual(self.store.settings["soulsync"], {"url": self.soulsync.url, "api_key": "sk_secret"})
        # Same address, blank key field: the stored key is kept, as before.
        self.client.post("/settings/soulsync", data={"url": self.soulsync.url + "/", "api_key": "", "action": "save"})
        self.assertEqual(self.store.settings["soulsync"], {"url": self.soulsync.url, "api_key": "sk_secret"})
        # New address with its key: saved.
        self.client.post("/settings/soulsync", data={"url": "http://127.0.0.1:9/elsewhere", "api_key": "sk_other", "action": "save"})
        self.assertEqual(self.store.settings["soulsync"], {"url": "http://127.0.0.1:9/elsewhere", "api_key": "sk_other"})

    def test_pool_key_is_not_sent_to_a_new_pool_url(self):
        self.store.settings["pools"] = [{
            "id": "sin", "name": "SIN", "url": self.pool.url,
            "api_key": "pool-key", "role": "node", "enabled": True,
        }]
        self.store.save_settings()
        self.client.post("/pools", data={"pool_id": "sin", "name": "SIN", "url": "https://elsewhere.example", "api_key": "", "role": "node"})
        pool = self.store.settings["pools"][0]
        self.assertEqual(pool["url"], self.pool.url)
        self.assertEqual(pool["api_key"], "pool-key")


class TestBatchNodeKeySpelling(unittest.TestCase):
    """One key, one accepted spelling: origins are counted by this string."""

    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        self.pub = C.b64url_encode(self.priv.public_key().public_bytes_raw())
        subject = S.generate_soul_id("track", "Radiohead", "Karma Police")
        self.claim = C.validate_claim({
            "subject": subject, "kind": "track", "field": "name",
            "value": ["Radiohead", "Karma Police"], "source": "musicbrainz",
            "soulid_version": C.SOULID_VERSION,
        })

    def envelope(self, spelling):
        return C.sign_batch(self.priv, C.make_batch(spelling, [self.claim]))

    def test_canonical_spelling_verifies(self):
        C.verify_batch(self.pub, self.envelope(self.pub))

    def test_other_spellings_of_the_same_key_are_refused(self):
        for spelling in (self.pub + "=", "!!!!" + self.pub, "    " + self.pub, "...." + self.pub):
            with self.subTest(spelling=spelling):
                # Each of these decodes to the very same key bytes...
                self.assertEqual(C.b64url_decode(spelling), C.b64url_decode(self.pub))
                # ...and must not be accepted as a different origin.
                with self.assertRaises(C.ClaimError):
                    C.verify_batch(spelling, self.envelope(spelling))


class TestAlbumSubjectForTracklists(unittest.TestCase):
    """SoulSync exports a per-track "album_track" id as album_soul_id."""

    def test_real_export_shape_builds_the_tracklist_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        album_id = S.generate_soul_id("album", "Radiohead", "OK Computer")
        track_id = S.generate_soul_id("track", "Radiohead", "Airbag")
        album = {
            "soul_id": album_id, "title": "OK Computer", "artist_name": "Radiohead",
            "year": 1997, "canonical_source": "spotify", "canonical_album_id": "alb-1",
            "musicbrainz_release_id": "mb-rel-1", "musicbrainz_match_status": "matched",
            "genres": [],
        }
        track = {
            "soul_id": track_id, "title": "Airbag", "artist_name": "Radiohead",
            "album_title": "OK Computer",
            # What SoulSync really sends here (core/soulid_worker.py):
            "album_soul_id": S.generate_soul_id("album_track", "Radiohead", "OK Computer", "Airbag"),
            "track_number": 1, "disc_number": 1, "duration": 284460,
        }
        self.assertNotEqual(track["album_soul_id"], album_id)

        class Fake:
            def export_page(self, entity, cursor="", since="", limit=1000):
                items = {"artist": [], "album": [album], "track": [track]}[entity]
                return {"soulid_version": "kinded_v2", "items": items, "has_more": False, "next_cursor": ""}

        store = MetaStore(tmp.name)
        B.LibraryBuilder(Fake(), load_or_create_identity(tmp.name), store).run("2026-10-26T00:00:00+00:00")
        slots = [c for c in store.own_claims(album_id) if c["field"] == "album_track"]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["value"]["track"], track_id)
        self.assertEqual(slots[0]["value"]["variant"], "mb:mb-rel-1")


class TestVerifierNonJsonAnswer(unittest.TestCase):
    def test_a_non_json_200_is_a_failed_lookup(self):
        class Response:
            status_code = 200
            ok = True

            def json(self):
                raise ValueError("Expecting value")

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        no_sleep = lambda seconds: None  # noqa: E731
        fetchers = (
            V.MusicBrainzFetcher("MetaSync-Test/0", session=Session(), min_interval_s=0.0, sleep=no_sleep),
            V.DeezerFetcher(session=Session(), min_interval_s=0.0, sleep=no_sleep),
        )
        for fetcher in fetchers:
            with self.subTest(fetcher=type(fetcher).__name__):
                with self.assertRaises(V.FetcherError):
                    fetcher._get("/x")


class TestAnswerFrameFitsTheRelayShare(unittest.TestCase):
    def test_a_sealed_frame_fits_the_relays_per_node_share(self):
        from metasync_relay.config import RelayConfig

        cap = RelayConfig().answer_node_query_cap_bytes
        # Sealing adds 48 bytes, base64 4/3, and a small JSON wrapper.
        worst_frame = (WIRE_CHUNK_BYTES + 48) * 4 // 3 + 1024
        self.assertLessEqual(worst_frame, cap)


if __name__ == "__main__":
    unittest.main()

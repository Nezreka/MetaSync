"""Service wiring for metadata contribution: settings gate, run
outcome recording, scheduler due rule. Clients are the same fakes
as test_meta_contribute's, injected through the service's override
hooks."""
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.config import Config
from metasync_node.service import NodeService
from metasync_node.store import Store
from tests.test_meta_contribute import (
    ALBUM,
    ARTIST,
    TRACK,
    FakePool,
    FakeSoulSync,
    page,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class MetadataServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.settings["soulsync"] = {"url": "http://ss.local",
                                            "api_key": "sk_test"}
        self.store.settings["pools"] = [{
            "id": "pool-1", "name": "SIN", "url": "https://pool.example",
            "api_key": "sin_live_x", "role": "node", "enabled": True,
        }]
        self.store.save_settings()
        pages = {
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        }
        self.soulsync = FakeSoulSync(pages)
        self.service = NodeService(
            Config(state_dir=self.tmp.name), store=self.store,
            soulsync=self.soulsync)
        self.pool = FakePool(self.service.identity)
        self.service._pool_overrides["pool-1"] = self.pool

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_records_completion(self):
        self.store.settings["share_metadata"] = True
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "completed")
        self.assertGreater(out["accepted"], 0)
        status = self.service.metadata_status()
        self.assertEqual(status["last_status"], "completed")
        self.assertEqual(status["last_totals"]["accepted"],
                         out["accepted"])

    def test_due_rule(self):
        # Off by default: never due.
        self.assertFalse(self.service.metadata_due(NOW))
        self.store.settings["share_metadata"] = True
        self.assertTrue(self.service.metadata_due(NOW))
        self.service.run_metadata_contribution(NOW)
        # Just ran: not due again for ~a day.
        self.assertFalse(self.service.metadata_due(NOW + timedelta(hours=2)))
        self.assertTrue(self.service.metadata_due(NOW + timedelta(hours=21)))

    def test_preview_stores_sample_without_sending(self):
        out = self.service.preview_metadata()
        self.assertGreater(sum(out["counts"].values()), 0)
        self.assertEqual(self.pool.sent, [])
        self.assertIsNotNone(
            self.service.metadata_status()["preview"])

    def test_no_pool_is_a_status_not_a_crash(self):
        self.store.settings["share_metadata"] = True
        self.store.settings["pools"] = []
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "no_pool")

    def test_switch_off_sends_nothing(self):
        # share_metadata defaults off: even an explicit run refuses.
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "sharing_off")
        self.assertEqual(self.pool.sent, [])
        self.assertEqual(self.soulsync.calls, [])

    def test_switch_off_does_not_stamp_last_run_at(self):
        # A refused run is not an attempt: stamping it would push
        # the first real scheduled run out by a day once the user
        # turns sharing on.
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "sharing_off")
        self.assertIsNone(self.service.metadata_status()["last_run_at"])

    def test_pool_error_records_partial_totals(self):
        from metasync_node.pool import PoolError

        class BoomPool(FakePool):
            def post_meta_claims(self, envelope, meta_base):
                raise PoolError("meta service down")

        self.service._pool_overrides["pool-1"] = BoomPool(
            self.service.identity)
        self.store.settings["share_metadata"] = True
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "pool_error")
        totals = self.service.metadata_status()["last_totals"]
        self.assertGreater(totals["claims_built"], 0)

    def test_bad_meta_url_sends_nothing(self):
        # The pool token is presented to meta_url, so a plain-http
        # non-loopback URL is refused before anything is read or sent.
        self.store.settings["share_metadata"] = True
        self.store.settings["meta_url"] = "http://meta.example"
        out = self.service.run_metadata_contribution(NOW)
        self.assertEqual(out["status"], "bad_meta_url")
        self.assertEqual(self.pool.sent, [])
        self.assertEqual(self.soulsync.calls, [])


if __name__ == "__main__":
    unittest.main()

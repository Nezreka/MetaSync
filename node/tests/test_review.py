"""Behaviour added by the Oct 2026 review — closed-week reporting, the
scheduler's once-a-week rule, bulk suppression, listening signals, and the
pool-key transport guards. Same localhost mocks as test_node.py."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.config import Config  # noqa: E402
from metasync_node.pool import PoolClient, PoolError  # noqa: E402
from metasync_node.service import NodeService, check_pool_url  # noqa: E402
from metasync_node.signals import (  # noqa: E402
    BULK_EVENT_LIMIT, HUNT_DEMAND_LIMIT, LISTEN_LIMIT, compute_signals,
)
from metasync_node.store import Store  # noqa: E402
from tests.mocks import browser_client, ALBUMS, RG_A, RG_B, REL_A, REL_B, MockMusicBrainz, MockPool, MockSoulSync  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)  # Monday of 2026-W44
W43_START = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _mbid(prefix: str, n: int) -> str:
    return f"{prefix * 8}-{prefix * 4}-4{prefix * 3}-8{prefix * 3}-{n:012d}"


def _types(result) -> list[str]:
    return [f"{s['type']}:{s['mbid']}" for s in result["signals"]]


class TestClosedWeekRules(unittest.TestCase):
    def _signals(self, albums, **kwargs):
        return compute_signals(
            watchlist=[], wishlist=[], albums=albums,
            release_to_group={REL_A: RG_A, REL_B: RG_B}, week_start=W43_START, **kwargs,
        )

    def test_import_keep_fires_once_in_the_week_an_album_crosses_14_days(self):
        crossing = {"id": 1, "created_at": "2026-10-06T08:00:00", "musicbrainz_release_id": REL_A}
        long_kept = {"id": 2, "created_at": "2026-01-10T08:00:00", "musicbrainz_release_id": REL_B}
        keys = _types(self._signals([crossing, long_kept]))
        self.assertIn(f"import_keep:{RG_A}", keys)
        # An album kept for months is not re-reported every week: the
        # report describes last week, not the library.
        self.assertNotIn(f"import_keep:{RG_B}", keys)
        # The week after, the crossing album is quiet too.
        next_week = compute_signals(
            watchlist=[], wishlist=[], albums=[crossing], release_to_group={REL_A: RG_A},
            week_start=W43_START + timedelta(days=7),
        )
        self.assertEqual(next_week["signals"], [])

    def test_events_are_bounded_by_the_week_not_by_the_run_time(self):
        # Landed on Monday of W44 — after the reported week closed.
        late = {"id": 1, "created_at": "2026-10-26T01:00:00", "musicbrainz_release_id": REL_A}
        self.assertEqual(self._signals([late])["signals"], [])

    def test_a_malformed_date_skips_its_item_not_the_report(self):
        broken = {"id": 1, "created_at": "not-a-date", "musicbrainz_release_id": REL_A}
        fresh = {"id": 2, "created_at": "2026-10-21T09:00:00", "musicbrainz_release_id": REL_B}
        result = compute_signals(
            watchlist=[{"musicbrainz_artist_id": RG_A, "date_added": "garbage"}],
            wishlist=[{"track_data": {"musicbrainz_release_group_id": RG_B}, "date_added": "??"}],
            albums=[broken, fresh], release_to_group={REL_A: RG_A, REL_B: RG_B},
            week_start=W43_START,
        )
        self.assertEqual(_types(result), [f"recent_add:{RG_B}"])

    def test_a_bulk_import_is_held_back_not_reported(self):
        bulk = [
            {"id": i, "created_at": "2026-10-20T08:00:00", "musicbrainz_release_id": _mbid("1", i)}
            for i in range(BULK_EVENT_LIMIT + 1)
        ]
        r2g = {a["musicbrainz_release_id"]: _mbid("2", a["id"]) for a in bulk}
        result = compute_signals(
            watchlist=[], wishlist=[], albums=bulk, release_to_group=r2g, week_start=W43_START,
        )
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["diagnostics"]["bulk_suppressed"], {"recent_add": BULK_EVENT_LIMIT + 1})

    def test_standing_hunt_demand_is_sampled_not_dropped(self):
        wishlist = [
            {"track_data": {"musicbrainz_release_group_id": _mbid("3", i)}, "date_added": "2026-09-01T00:00:00", "retry_count": 1}
            for i in range(HUNT_DEMAND_LIMIT + 40)
        ]
        result = compute_signals(
            watchlist=[], wishlist=wishlist, albums=[], release_to_group={}, week_start=W43_START,
        )
        self.assertEqual(len(result["signals"]), HUNT_DEMAND_LIMIT)
        again = compute_signals(
            watchlist=[], wishlist=wishlist, albums=[], release_to_group={}, week_start=W43_START,
        )
        self.assertEqual(result["signals"], again["signals"])  # deterministic within a week


class TestListenSignals(unittest.TestCase):
    def _signals(self, listening, r2g=None):
        return compute_signals(
            watchlist=[], wishlist=[], albums=[], week_start=W43_START,
            release_to_group=r2g or {REL_A: RG_A, REL_B: RG_B}, listening=listening,
        )

    def test_off_means_none_and_a_threshold_of_three_distinct_tracks(self):
        rows = [
            {"musicbrainz_release_id": REL_A, "plays": 9, "distinct_tracks": 3, "distinct_days": 1},
            {"musicbrainz_release_id": REL_B, "plays": 30, "distinct_tracks": 2, "distinct_days": 5},
        ]
        keys = _types(self._signals(rows))
        self.assertEqual(keys, [f"listen_album:{RG_A}"])  # one hit on repeat is not an album
        off = compute_signals(
            watchlist=[], wishlist=[], albums=[], release_to_group={REL_A: RG_A},
            week_start=W43_START,
        )
        self.assertFalse(off["diagnostics"]["listening_shared"])
        self.assertEqual(off["signals"], [])

    def test_capped_to_the_most_played_albums(self):
        rows, r2g = [], {}
        for i in range(LISTEN_LIMIT + 10):
            rel = _mbid("4", i)
            r2g[rel] = _mbid("5", i)
            rows.append({"musicbrainz_release_id": rel, "plays": 100 + i, "distinct_tracks": 4, "distinct_days": 1})
        result = self._signals(rows, r2g)
        self.assertEqual(len(result["signals"]), LISTEN_LIMIT)
        self.assertEqual(result["diagnostics"]["listen_candidates"], LISTEN_LIMIT + 10)
        sent = {s["mbid"] for s in result["signals"]}
        self.assertNotIn(_mbid("5", 0), sent)  # the least played fall off
        self.assertIn(_mbid("5", LISTEN_LIMIT + 9), sent)


class ReviewServiceCase(unittest.TestCase):
    listening = None
    dry_run = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.soulsync = MockSoulSync(listening=self.listening)
        self.pool = MockPool()
        self.mb = MockMusicBrainz()
        for mock in (self.soulsync, self.pool, self.mb):
            self.addCleanup(mock.close)
        config = Config(state_dir=self.tmp.name, musicbrainz_min_interval_s=0.0, http_timeout_s=5,
                        musicbrainz_base_url=self.mb.url)
        self.store = Store(self.tmp.name)
        self.store.settings["soulsync"] = {"url": self.soulsync.url, "api_key": "sk_test"}
        self.store.settings["pools"] = [{
            "id": "sin", "name": "SIN", "url": self.pool.url,
            "api_key": "pool-key", "role": "node", "enabled": True,
        }]
        self.store.settings["dry_run"] = self.dry_run
        self.store.save_settings()
        self.service = NodeService(config, store=self.store)
        self.service.clock = lambda: NOW


class TestReportWindow(ReviewServiceCase):
    def test_reads_are_bounded_to_the_closed_week(self):
        outcome = self.service.run_report(NOW)
        self.assertEqual(outcome["week"], "2026-W43")
        downloads = [params for path, params in self.soulsync.gets if path.endswith("/download-activity")]
        self.assertEqual(downloads[0]["since"], "2026-10-19T00:00:00+00:00")
        self.assertEqual(downloads[0]["until"], "2026-10-26T00:00:00+00:00")

    def test_listening_is_never_read_while_the_switch_is_off(self):
        self.service.run_report(NOW)
        self.assertFalse(any(path.endswith("/listening-activity") for path, _ in self.soulsync.gets))

    def test_a_rerun_in_the_same_week_never_counts_as_a_second_absence(self):
        self.service.compute_report(NOW)
        self.service.compute_report(NOW + timedelta(minutes=5))
        snapshots = self.store.state["library_group_snapshots"]
        self.assertEqual([s["week"] for s in snapshots], ["2026-W43"])

    def test_a_quiet_week_sends_nothing_and_still_counts_as_done(self):
        self.soulsync.close()
        self.soulsync = MockSoulSync(watchlist=[], wishlist=[], albums=[])
        self.addCleanup(self.soulsync.close)
        self.store.settings["soulsync"]["url"] = self.soulsync.url
        outcome = self.service.run_report(NOW)
        self.assertEqual(outcome["pools"]["sin"]["status"], "empty")
        self.assertEqual(self.pool.reports, [])
        self.assertFalse(self.service.report_due(NOW + timedelta(hours=8)))

    def test_a_second_send_for_the_week_is_acknowledged_not_counted(self):
        first = self.service.run_report(NOW)
        second = self.service.run_report(NOW + timedelta(hours=1))
        self.assertEqual(first["pools"]["sin"]["status"], "sent")
        self.assertEqual(second["pools"]["sin"]["status"], "already_sent")
        self.assertEqual(len(self.pool.reports), 1)


class TestListeningFlow(ReviewServiceCase):
    listening = [{"musicbrainz_release_id": REL_A, "plays": 12, "distinct_tracks": 6, "distinct_days": 3}]

    def test_listening_is_read_for_the_week_and_reported_when_on(self):
        self.store.settings["share_listening"] = True
        outcome = self.service.run_report(NOW)
        reads = [params for path, params in self.soulsync.gets if path.endswith("/listening-activity")]
        self.assertEqual(reads, [{"since": "2026-10-19T00:00:00+00:00", "until": "2026-10-26T00:00:00+00:00"}])
        sent = self.pool.reports[0]["signals"]
        self.assertIn({"type": "listen_album", "mbid": RG_A}, sent)
        self.assertEqual(outcome["diagnostics"]["listen_sent"], 1)


class TestScheduler(ReviewServiceCase):
    def test_not_before_this_nodes_slot_then_once_per_week(self):
        monday = datetime(2026, 10, 26, tzinfo=timezone.utc)
        slot = self.service.report_slot()
        self.assertLess(slot, timedelta(hours=6))
        if slot > timedelta(0):
            self.assertFalse(self.service.report_due(monday))
        self.assertTrue(self.service.report_due(monday + slot))
        self.service.run_report(monday + slot)
        self.assertFalse(self.service.report_due(monday + slot + timedelta(minutes=1)))
        self.assertFalse(self.service.report_due(monday + timedelta(days=6)))
        self.assertTrue(self.service.report_due(monday + timedelta(days=7) + slot))

    def test_a_failed_send_backs_off_instead_of_retrying_every_tick(self):
        self.store.settings["pools"][0]["url"] = "http://127.0.0.1:1"
        self.service.run_report(NOW)
        self.assertFalse(self.service.report_due(NOW + timedelta(minutes=1)))
        self.assertTrue(self.service.report_due(NOW + timedelta(hours=7)))

    def test_upgrade_from_start_of_week_reporting_never_resends_a_week(self):
        # The old node reported "the current week" on Monday: on W44 its
        # last_report already names W44, which is LATER than W43.
        self.store.state["last_report"] = {"sin": {"week": "2026-W44", "at": "2026-10-26T00:01:00+00:00"}}
        self.assertFalse(self.service.report_due(NOW))


class TestDryRunScheduler(ReviewServiceCase):
    dry_run = True

    def test_dry_run_previews_each_week_once(self):
        self.assertTrue(self.service.report_due(NOW))
        self.service.run_report(NOW)
        self.assertFalse(self.service.report_due(NOW + timedelta(hours=7)))


class TestPoolKeyTransport(unittest.TestCase):
    def test_pool_urls_must_be_https_off_loopback(self):
        check_pool_url("https://sin.example.net")
        check_pool_url("http://127.0.0.1:8080")
        check_pool_url("http://localhost:8080")
        with self.assertRaises(ValueError):
            check_pool_url("http://sin.example.net")
        with self.assertRaises(ValueError):
            check_pool_url("ftp://sin.example.net")
        check_pool_url("http://sin.lan", allow_insecure=True)

    def test_direct_key_fallback_only_for_loopback_pools(self):
        self.assertTrue(PoolClient({"url": "http://127.0.0.1:9"}).legacy_allowed)
        self.assertFalse(PoolClient({"url": "https://sin.example.net"}).legacy_allowed)
        self.assertTrue(PoolClient({"url": "https://sin.example.net"}, allow_legacy=True).legacy_allowed)


class TestHousekeeping(ReviewServiceCase):
    def test_raw_release_lookups_are_pruned_and_no_longer_cached(self):
        self.store.state["mb_cache"] = {
            f"/release/{REL_A}?inc=release-groups&fmt=json": {"big": "blob"},
            f"/release-group/{RG_A}?inc=releases&fmt=json": {"kept": True},
        }
        self.store.save_state()
        service = NodeService(self.service.config, store=Store(self.tmp.name))
        self.assertEqual(list(service.store.state["mb_cache"]), [f"/release-group/{RG_A}?inc=releases&fmt=json"])
        service.compute_report(NOW)
        self.assertFalse(any(k.startswith("/release/") for k in service.store.state["mb_cache"]))
        self.assertEqual(service.store.state["release_to_group"][REL_A], RG_A)


class TestLoginThrottle(unittest.TestCase):
    def test_five_wrong_passwords_then_a_cool_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = NodeService(Config(state_dir=tmp.name), store=Store(tmp.name))
        from metasync_node.app import create_app
        client = browser_client(create_app(service))
        client.post("/setup", data={"password": "right-password", "confirm": "right-password"})
        client.get("/logout")
        for _ in range(5):
            self.assertEqual(client.post("/login", data={"password": "wrong"}).status_code, 200)
        self.assertEqual(client.post("/login", data={"password": "right-password"}).status_code, 429)


class TestCsrf(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = NodeService(Config(state_dir=tmp.name), store=Store(tmp.name))
        from metasync_node.app import create_app
        self.app = create_app(self.service)
        self.browser = browser_client(self.app)
        self.browser.post("/setup", data={"password": "right-password", "confirm": "right-password"})

    def _plain_client(self):
        from flask.testing import FlaskClient

        return FlaskClient(self.app, self.app.response_class, use_cookies=True)

    def test_a_form_post_without_the_pages_token_is_refused(self):
        # Same browser, logged in, but the POST did not come from a page
        # this UI rendered (no token, or someone else's token).
        with self.browser.session_transaction() as sess:
            token = sess["csrf_token"]
        raw = self._plain_client()
        with raw.session_transaction() as sess:
            sess["ui_admin"] = True
            sess["csrf_token"] = token
        self.assertEqual(raw.post("/actions/dry-run", data={"dry_run": ""}).status_code, 400)
        self.assertEqual(raw.post("/actions/dry-run", data={"csrf_token": "forged"}).status_code, 400)
        self.assertTrue(self.service.dry_run)  # unchanged
        self.assertEqual(raw.post("/actions/dry-run", data={"csrf_token": token}).status_code, 302)
        self.assertFalse(self.service.dry_run)

    def test_every_rendered_form_carries_the_token(self):
        for page in ("/", "/pools", "/settings/soulsync"):
            html = self.browser.get(page).data.decode()
            self.assertEqual(html.count("<form"), html.count('name="csrf_token"'), page)

    def test_machine_routes_need_no_token(self):
        res = self._plain_client().post("/local/pair/request")
        self.assertEqual(res.status_code, 200)
        # ...but the dashboard's approve button does.
        res = self._plain_client().post("/local/pair/confirm", data={"session_id": "x"})
        self.assertEqual(res.status_code, 400)


class TestServedReleaseGroups(unittest.TestCase):
    def test_soulsync_served_groups_skip_musicbrainz(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        albums = [{"id": 1, "created_at": "2026-10-21T09:00:00",
                   "musicbrainz_release_id": REL_B, "musicbrainz_release_group_id": RG_B}]
        wishlist = [{"id": 1, "track_data": {"name": "x"}, "date_added": "2026-10-20T09:00:00",
                     "musicbrainz_release_group_id": RG_A}]
        soulsync = MockSoulSync(albums=albums, wishlist=wishlist, watchlist=[])
        mb = MockMusicBrainz()
        self.addCleanup(soulsync.close)
        self.addCleanup(mb.close)
        store = Store(tmp.name)
        store.settings["soulsync"] = {"url": soulsync.url, "api_key": "sk_test"}
        service = NodeService(Config(state_dir=tmp.name, musicbrainz_min_interval_s=0.0,
                                     musicbrainz_base_url=mb.url), store=store)
        report = service.compute_report(NOW)
        keys = _types(report)
        self.assertIn(f"recent_add:{RG_B}", keys)
        self.assertIn(f"wishlist_add:{RG_A}", keys)
        self.assertEqual(mb.hits, [])  # nothing asked of MusicBrainz


if __name__ == "__main__":
    unittest.main()

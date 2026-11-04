"""Getting a node out of dry run, and showing how big the network is.

A node starts in dry run: it builds the exact weekly report and sends
nothing, so the network doesn't count it. Nothing told the user when a
report was ready and unsent, and nothing said how far the network was
from charting on its own (15 installs per album per week)."""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mocks import browser_client  # noqa: E402
from tests.test_review import NOW, ReviewServiceCase  # noqa: E402


class _Who:
    def __init__(self, answer=None, fail=False):
        self.answer = answer
        self.fail = fail

    def get_whoami(self):
        if self.fail:
            raise RuntimeError("pool down")
        return self.answer


class TestUnsentReport(ReviewServiceCase):
    dry_run = True

    def test_a_dry_run_report_is_ready_and_unsent(self):
        self.assertIsNone(self.service.unsent_report())  # nothing computed yet
        self.service.run_report(NOW)

        unsent = self.service.unsent_report()
        self.assertEqual(unsent["week"], "2026-W43")
        self.assertEqual(unsent["pools"], 1)
        self.assertGreater(unsent["signal_count"], 0)
        self.assertEqual(self.service.status()["unsent_report"], unsent)
        local = self.service.local_status()
        self.assertTrue(local["dry_run"])
        self.assertEqual(local["unsent_report"], unsent)

    def test_nothing_is_unsent_once_sending_is_on(self):
        self.service.run_report(NOW)
        self.store.settings["dry_run"] = False
        self.assertIsNone(self.service.unsent_report())


class TestStartSending(ReviewServiceCase):
    dry_run = True

    def _app(self):
        from metasync_node.app import create_app
        app = create_app(self.service)
        browser = browser_client(app)
        browser.post("/setup", data={"password": "right-password", "confirm": "right-password"})
        return browser

    def test_start_sending_turns_dry_run_off_and_sends_this_weeks_report(self):
        self.service.run_report(NOW)
        self.assertEqual(self.pool.reports, [])
        browser = self._app()

        res = browser.post("/actions/start-sending")

        self.assertEqual(res.status_code, 302)
        self.assertFalse(self.service.dry_run)
        for _ in range(100):  # the report runs on the job thread
            job = self.service.report_job() or {}
            if job.get("status") != "running":
                break
            time.sleep(0.1)
        self.assertEqual(job.get("status"), "done", job)
        self.assertEqual(len(self.pool.reports), 1)
        self.assertIsNone(self.service.unsent_report())

    def test_the_dashboard_and_preview_offer_it_while_a_report_waits(self):
        self.service.run_report(NOW)
        browser = self._app()
        dashboard = browser.get("/").data.decode()
        self.assertIn("Your report is ready, and nothing was sent", dashboard)
        self.assertIn("/actions/start-sending", dashboard)
        self.assertEqual(dashboard.count("<form"), dashboard.count('name="csrf_token"'))
        preview = browser.get("/preview").data.decode()
        self.assertIn("Looks right?", preview)
        self.assertIn("/actions/start-sending", preview)

    def test_start_sending_with_no_pool_only_turns_sending_on(self):
        self.store.settings["pools"] = []
        browser = self._app()
        browser.post("/actions/start-sending")
        self.assertFalse(self.service.dry_run)
        self.assertIsNone(self.service.report_job())


class TestNetworkSize(ReviewServiceCase):
    def _remember(self, answer=None, fail=False):
        pool = self.service.pools()[0]
        self.service._remember_network(pool, _Who(answer, fail), datetime(2026, 10, 27, tzinfo=timezone.utc))

    def test_the_network_size_the_pool_reports_is_kept_and_shown(self):
        self._remember({"ok": True, "network": {
            "week": "2026-W43", "installs": 12, "previousWeek": "2026-W42",
            "previousInstalls": 9, "publishThreshold": 15}})
        progress = self.service.network_progress()
        self.assertEqual((progress["installs"], progress["publish_threshold"], progress["pool"]),
                         (12, 15, "SIN"))
        self.assertEqual(self.service.local_status()["network"], progress)

        from metasync_node.app import create_app
        browser = browser_client(create_app(self.service))
        browser.post("/setup", data={"password": "right-password", "confirm": "right-password"})
        html = browser.get("/").data.decode()
        self.assertIn("12 installs reported 2026-W43", html)
        self.assertIn("start once 15 installs", html)

    def test_an_older_pool_or_a_bad_answer_shows_nothing(self):
        self._remember({"ok": True})  # a pool without the field
        self._remember({"ok": True, "network": {"installs": "lots", "publishThreshold": 15}})
        self._remember({"ok": True, "network": {"installs": -3, "publishThreshold": 15}})
        self._remember({"ok": True, "network": {"installs": 4, "publishThreshold": 0}})
        self._remember(fail=True)
        self.assertIsNone(self.service.network_progress())

    def test_a_failed_check_keeps_the_last_size(self):
        self._remember({"ok": True, "network": {"week": "2026-W43", "installs": 12, "publishThreshold": 15}})
        self._remember(fail=True)
        self.assertEqual(self.service.network_progress()["installs"], 12)


if __name__ == "__main__":
    unittest.main()

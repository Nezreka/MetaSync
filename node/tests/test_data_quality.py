"""Data quality (signals.py GAP-1, GAP-3, GAP-4) and the lookup order.

- hunt_demand: only rows SoulSync has tried, and only albums not owned;
- first_seen: a re-scan that resets created_at re-sends neither a
  recent_add nor an import_keep;
- late matches: a wishlist row or library album matched after its week
  still counts, once, in the week it is matched, and a re-run of that week
  gives the same report;
- churn_optin rides the payload only when drop signals are shared;
- this week's albums are looked up on MusicBrainz before the rest.
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.signals import compute_signals  # noqa: E402
from tests.mocks import RG_A, RG_B, REL_A, REL_B  # noqa: E402
from tests.test_node import W43_START, ServiceCase  # noqa: E402

W44_START = W43_START + timedelta(days=7)
W45_START = W43_START + timedelta(days=14)


def keys(result):
    return sorted(f"{s['type']}:{s['mbid']}" for s in result["signals"])


def run(**kwargs):
    base = {"watchlist": [], "wishlist": [], "albums": [], "release_to_group": {}, "week_start": W43_START}
    base.update(kwargs)
    return compute_signals(**base)


class TestHuntDemand(unittest.TestCase):
    def row(self, **extra):
        return {"id": 1, "track_data": {"musicbrainz_release_group_id": RG_B},
                "date_added": "2026-10-01T00:00:00", **extra}

    def test_untried_rows_are_not_hunts(self):
        self.assertEqual(keys(run(wishlist=[self.row()])), [])
        self.assertEqual(keys(run(wishlist=[self.row(retry_count=0)])), [])

    def test_a_tried_row_is_a_hunt(self):
        self.assertEqual(keys(run(wishlist=[self.row(retry_count=3)])), [f"hunt_demand:{RG_B}"])

    def test_a_missing_track_of_an_owned_album_is_not_a_hunt(self):
        owned = [{"id": 7, "created_at": "2026-08-01T00:00:00", "musicbrainz_release_id": REL_B}]
        result = run(wishlist=[self.row(retry_count=3)], albums=owned, release_to_group={REL_B: RG_B})
        self.assertNotIn(f"hunt_demand:{RG_B}", keys(result))


class TestFirstSeen(unittest.TestCase):
    album = {"id": 5, "created_at": "2026-10-20T08:00:00", "musicbrainz_release_id": REL_A}

    def test_a_rescan_does_not_make_an_old_album_new(self):
        # the node saw this group in August; a re-scan reset created_at
        result = run(albums=[self.album], release_to_group={REL_A: RG_A},
                     first_seen={RG_A: "2026-08-01T00:00:00+00:00"})
        self.assertEqual(keys(result), [])
        self.assertEqual(result["first_seen"][RG_A], "2026-08-01T00:00:00+00:00")

    def test_a_reset_date_does_not_resend_a_keeper(self):
        # created_at reset to 2026-10-06 would cross 14 days in W43
        reset = {**self.album, "created_at": "2026-10-06T08:00:00"}
        result = run(albums=[reset], release_to_group={REL_A: RG_A},
                     first_seen={RG_A: "2026-08-01T00:00:00+00:00"})
        self.assertNotIn(f"import_keep:{RG_A}", keys(result))

    def test_a_genuinely_new_album_is_new_and_remembered(self):
        result = run(albums=[self.album], release_to_group={REL_A: RG_A})
        self.assertEqual(keys(result), [f"recent_add:{RG_A}"])
        self.assertTrue(result["first_seen"][RG_A].startswith("2026-10-20"))
        # re-running the same week (preview, then send) gives the same report
        again = run(albums=[self.album], release_to_group={REL_A: RG_A}, first_seen=result["first_seen"])
        self.assertEqual(keys(again), [f"recent_add:{RG_A}"])


class TestLateMatches(unittest.TestCase):
    unmatched = {"id": 11, "track_data": {}, "date_added": "2026-10-20T09:00:00"}
    matched = {"id": 11, "musicbrainz_release_group_id": RG_B, "track_data": {}, "date_added": "2026-10-20T09:00:00"}

    def test_a_wishlist_row_matched_a_week_late_counts_once(self):
        w43 = run(wishlist=[self.unmatched])
        self.assertEqual(keys(w43), [])
        self.assertIn("11", w43["late_pending"]["wishlist"])
        w44 = run(wishlist=[self.matched], week_start=W44_START, late_pending=w43["late_pending"])
        self.assertEqual(keys(w44), [f"wishlist_add:{RG_B}"])
        # the same week again: same report
        again = run(wishlist=[self.matched], week_start=W44_START, late_pending=w43["late_pending"])
        self.assertEqual(keys(again), [f"wishlist_add:{RG_B}"])
        # the next week: never again
        w45 = run(wishlist=[self.matched], week_start=W45_START, late_pending=w44["late_pending"])
        self.assertEqual(keys(w45), [])

    def test_a_late_album_is_a_recent_add_in_the_week_it_resolves(self):
        album = {"id": 21, "created_at": "2026-10-21T08:00:00", "musicbrainz_release_id": REL_A}
        w43 = run(albums=[album])  # no group yet
        self.assertEqual(keys(w43), [])
        w44 = run(albums=[album], release_to_group={REL_A: RG_A}, week_start=W44_START,
                  late_pending=w43["late_pending"])
        self.assertEqual(keys(w44), [f"recent_add:{RG_A}"])

    def test_a_row_never_matched_is_forgotten_after_the_window(self):
        pending = run(wishlist=[self.unmatched])["late_pending"]
        for week in (W44_START, W45_START):
            pending = run(wishlist=[self.unmatched], week_start=week, late_pending=pending)["late_pending"]
        later = run(wishlist=[self.matched], week_start=W45_START + timedelta(days=7), late_pending=pending)
        self.assertEqual(keys(later), [])


class TestPayloadAndLookupOrder(ServiceCase):
    def test_churn_optin_only_when_drop_signals_are_shared(self):
        report = {"week": "2026-W43", "signals": []}
        self.assertNotIn("churn_optin", self.service.payload_for_pool(self.pool_dict(), report))
        self.store.settings["share_churn"] = True
        self.assertIs(self.service.payload_for_pool(self.pool_dict(), report)["churn_optin"], True)

    def test_this_weeks_albums_are_looked_up_first(self):
        looked_up = []

        class Recorder:
            def release_to_group(self, release_id):
                looked_up.append(release_id)
                return None

        old = {"id": 1, "created_at": "2025-01-01T00:00:00", "musicbrainz_release_id": REL_B}
        new = {"id": 2, "created_at": "2026-10-20T00:00:00", "musicbrainz_release_id": REL_A}
        self.service.resolver = Recorder()
        self.config.release_resolution_cap = 1
        self.service._resolve_album_groups([old, new], priority_release_ids=[REL_A])
        self.assertEqual(looked_up, [REL_A])


if __name__ == "__main__":
    unittest.main()

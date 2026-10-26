"""Merge library rules (DESIGN-METADATA §8/§9)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.meta import merge as M

SUBJ = "soul_0123456789abcdef"


def agg(field, value, accounts=1, tier="unverified", source="deezer",
        kind="track"):
    return {"subject": SUBJ, "kind": kind, "field": field, "value": value,
            "accounts": accounts, "tier": tier, "source": source}


def fields(claims, kind="track"):
    return M.merge_subject(kind, SUBJ, claims)["fields"]


class TestSetFieldsAndFloor(unittest.TestCase):
    def test_nirvana_both_ids_publish_as_set(self):
        # two bands, one artist SoulID, two true Spotify IDs (§8)
        out = fields([
            agg("spotify_id", "AAA", tier="strong", kind="artist"),
            agg("spotify_id", "BBB", tier="strong", kind="artist"),
        ], kind="artist")
        pub = out["spotify_id"]["published"]
        self.assertEqual(pub["values"], ["AAA", "BBB"])
        self.assertEqual(pub["support"], "strong-verified")

    def test_strong_publishes_at_n1(self):
        out = fields([agg("deezer_id", "123", tier="strong")])
        self.assertIn("published", out["deezer_id"])

    def test_unverified_below_floor_is_held(self):
        out = fields([agg("deezer_id", "123", accounts=2)])
        self.assertNotIn("published", out["deezer_id"])
        self.assertEqual(out["deezer_id"]["held"], ["123"])

    def test_unverified_at_floor_publishes(self):
        out = fields([agg("deezer_id", "123", accounts=3)])
        pub = out["deezer_id"]["published"]
        self.assertEqual(pub["support"], "3+ accounts")

    def test_plausible_is_not_strong(self):
        out = fields([agg("isrc", "X", tier="plausible")])
        self.assertNotIn("published", out["isrc"])


class TestYear(unittest.TestCase):
    def test_provider_year_never_publishes(self):
        out = fields([agg("year", 1997, tier="strong", source="spotify")])
        self.assertNotIn("published", out["year"])

    def test_mb_year_publishes(self):
        out = fields([agg("year", 1997, tier="strong",
                          source="musicbrainz")])
        self.assertEqual(out["year"]["published"]["values"], [1997])

    def test_disputed_mb_years_never_auto_fill(self):
        out = fields([
            agg("year", 1997, tier="strong", source="musicbrainz"),
            agg("year", 1998, tier="strong", source="musicbrainz"),
        ])
        self.assertTrue(out["year"]["published"]["never_auto_fill"])


class TestGenres(unittest.TestCase):
    def test_ranked_by_support_floor_per_genre(self):
        out = fields([
            agg("genres", "rock", accounts=5),
            agg("genres", "alternative", accounts=3),
            agg("genres", "obscure-tag", accounts=1),
        ])
        pub = out["genres"]["published"]
        self.assertEqual(pub["values"], ["rock", "alternative"])
        self.assertTrue(pub["never_auto_fill"])
        self.assertEqual(out["genres"]["held"], ["obscure-tag"])


class TestSupportLabels(unittest.TestCase):
    def test_mixed_tier_set_is_not_labelled_strong(self):
        # one strong value + one floor-only value: the entry must not
        # wear "strong-verified" for both (§8 enrich auto-settles on
        # a strong-verified MBID — the label must not overclaim)
        out = fields([
            agg("spotify_id", "AAA", tier="strong"),
            agg("spotify_id", "BBB", accounts=3),
        ])
        pub = out["spotify_id"]["published"]
        self.assertEqual(pub["values"], ["AAA", "BBB"])
        self.assertEqual(pub["support"], "3+ accounts")

    def test_multiple_mbids_kept_and_flagged(self):
        out = fields([
            agg("recording_mbid", "mb-1", tier="strong"),
            agg("recording_mbid", "mb-2", tier="strong"),
        ])
        pub = out["recording_mbid"]["published"]
        self.assertEqual(pub["values"], ["mb-1", "mb-2"])
        self.assertTrue(pub["never_auto_fill"])
        out = fields([agg("recording_mbid", "mb-1", tier="strong")])
        self.assertFalse(
            out["recording_mbid"]["published"]["never_auto_fill"])


class TestDuration(unittest.TestCase):
    def test_median_and_outlier_drop(self):
        claims = [agg("duration_ms", {"ms": ms, "variant": "v1"},
                      tier="strong")
                  for ms in (213000, 214000, 215000, 240000)]
        out = fields(claims)
        self.assertEqual(out["duration_ms"]["variants"]["v1"]["ms"], 214000)
        self.assertEqual(out["duration_ms"]["published"]["values"], [214000])
        # §9: support is coarse — no exact report counts in the output
        self.assertNotIn("reports", out["duration_ms"]["variants"]["v1"])

    def test_two_disagreeing_reports_do_not_crash(self):
        # both reports are >2s from their own median, so the outlier
        # filter empties the kept set — regression: this raised
        # StatisticsError and killed the whole merge (gate, Oct 26)
        claims = [agg("duration_ms", {"ms": ms, "variant": "v1"},
                      tier="strong")
                  for ms in (200000, 210000)]
        out = fields(claims)
        self.assertEqual(out["duration_ms"]["variants"], {})
        self.assertNotIn("published", out["duration_ms"])

    def test_median_is_integer_ms(self):
        claims = [agg("duration_ms", {"ms": ms, "variant": "v1"},
                      tier="strong")
                  for ms in (200000, 203000)]
        out = fields(claims)
        ms = out["duration_ms"]["variants"]["v1"]["ms"]
        self.assertEqual(ms, 201500)
        self.assertIsInstance(ms, int)

    def test_variants_that_disagree_publish_no_bare_duration(self):
        claims = [
            agg("duration_ms", {"ms": 200000, "variant": "std"},
                tier="strong"),
            agg("duration_ms", {"ms": 260000, "variant": "live"},
                tier="strong"),
        ]
        out = fields(claims)
        self.assertNotIn("published", out["duration_ms"])
        self.assertEqual(set(out["duration_ms"]["variants"]),
                         {"std", "live"})


class TestAlbumTrack(unittest.TestCase):
    def _claims(self, variant, tier="strong", accounts=1):
        return [
            agg("album_track",
                {"track": f"soul_t{i}", "disc": 1, "position": i,
                 "variant": variant},
                tier=tier, accounts=accounts, kind="album")
            for i in (1, 2, 3)
        ]

    def test_variants_never_collapse(self):
        out = fields(self._claims("std") + self._claims("dlx"),
                     kind="album")
        self.assertEqual(set(out["album_track"]["variants"]),
                         {"std", "dlx"})

    def test_same_sequence_same_fingerprint_across_providers(self):
        a = fields(self._claims("spotify:1"), kind="album")
        b = fields(self._claims("deezer:9"), kind="album")
        fa = a["album_track"]["variants"]["spotify:1"]["fingerprint"]
        fb = b["album_track"]["variants"]["deezer:9"]["fingerprint"]
        self.assertEqual(fa, fb)

    def test_unverified_tracklist_held(self):
        out = fields(self._claims("std", tier="unverified"),
                     kind="album")
        self.assertEqual(out["album_track"]["variants"], {})
        self.assertEqual(out["album_track"]["held_variants"], ["std"])

    def test_one_verified_slot_does_not_publish_the_tracklist(self):
        # §9 gates every claim, §7 strong-verifies a variant only when
        # its FULL tracklist matches — one strong slot must not carry
        # the below-floor slots into the corpus (gate, Oct 26)
        claims = self._claims("std", tier="unverified")
        claims[0] = agg("album_track",
                        {"track": "soul_t1", "disc": 1, "position": 1,
                         "variant": "std"},
                        tier="strong", kind="album")
        out = fields(claims, kind="album")
        self.assertEqual(out["album_track"]["variants"], {})
        self.assertEqual(out["album_track"]["held_variants"], ["std"])


class TestDisplayName(unittest.TestCase):
    def test_tie_break_is_input_order_independent(self):
        # same claims, reversed order, same winner — the worker and
        # the node must merge identically (§8, gate, Oct 26)
        claims = [
            agg("display_name", ["beta name"], accounts=5),
            agg("display_name", ["alpha name"], accounts=5),
        ]
        a = fields(claims, kind="album")
        b = fields(list(reversed(claims)), kind="album")
        self.assertEqual(a["display_name"]["published"]["values"],
                         b["display_name"]["published"]["values"])

    def test_mb_name_wins_over_crowd(self):
        out = fields([
            agg("display_name", ["ok computer"], accounts=9),
            agg("display_name", ["OK Computer"], tier="strong",
                source="musicbrainz"),
        ], kind="album")
        self.assertEqual(out["display_name"]["published"]["values"],
                         [["OK Computer"]])


if __name__ == "__main__":
    unittest.main()

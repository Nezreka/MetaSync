"""Seed claims from a public corpus — and why they are not observations."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync import soulid  # noqa: E402
from metasync.claim import SRC_DATASET, SRC_OBSERVED, Claim  # noqa: E402
from metasync.identity import NodeIdentity  # noqa: E402
from metasync.seed import build, wanted_isrcs  # noqa: E402
from metasync.store import ORIGIN_LOCAL, ClaimStore  # noqa: E402

NORM = {"artist": "pinkfloyd", "album": "animals", "title": "dogs"}
MBID = "af507c30-4993-41b8-ad02-95eb77be7ba4"


def library_claim(ids, title="dogs"):
    return Claim(entity="track", norm={**NORM, "title": title}, ids=ids,
                 observed_on="2026-08-19")


class SeedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = ClaimStore(self.dir / "s.db")
        self.me = NodeIdentity.generate()
        self.seed_node = NodeIdentity.generate()

        # mbdump/isrc:      id, recording, isrc, edits_pending, created
        (self.dir / "isrc").write_text(
            "1\t500\tGBN9Y1100088\t0\t2011-05-16\n"
            "2\t501\tUSUM71301829\t0\t2011-05-16\n"
            "3\t999\tZZOTHER000001\t0\t2011-05-16\n")
        # mbdump/recording: id, gid, name, artist_credit, ...
        (self.dir / "recording").write_text(
            f"500\t{MBID}\tDogs\t42\t1023000\n"
            "501\tbbbbbbbb-0000-0000-0000-000000000001\tTeenage\t43\t273720\n"
            "999\tcccccccc-0000-0000-0000-000000000002\tOther\t44\t1000\n")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _build(self, **kwargs):
        return build(self.store, self.dir / "isrc", self.dir / "recording",
                     self.seed_node, **kwargs)

    def test_isrc_join_fills_a_missing_mbid(self):
        self.store.put(library_claim({"isrc": ("GBN9Y1100088",)}).sign(self.me), ORIGIN_LOCAL)
        stats = self._build()
        self.assertEqual(stats.claims, 1)
        resolved = self.store.resolve("track", library_claim({}).key)
        self.assertEqual(resolved["musicbrainz"][0]["value"], MBID)

    def test_tracks_that_already_have_an_mbid_are_skipped(self):
        """Re-asserting what the library resolved teaches nothing and only
        inflates the store."""
        self.store.put(
            library_claim({"isrc": ("GBN9Y1100088",), "musicbrainz": ("existing-mbid",)})
            .sign(self.me), ORIGIN_LOCAL)
        stats = self._build()
        self.assertEqual(stats.wanted_isrcs, 0)
        self.assertEqual(stats.claims, 0)

    def test_all_flag_seeds_even_known_tracks(self):
        self.store.put(
            library_claim({"isrc": ("GBN9Y1100088",), "musicbrainz": ("existing-mbid",)})
            .sign(self.me), ORIGIN_LOCAL)
        stats = self._build(only_missing=False)
        self.assertEqual(stats.claims, 1)
        values = [e["value"] for e in
                  self.store.resolve("track", library_claim({}).key)["musicbrainz"]]
        self.assertIn(MBID, values)
        self.assertIn("existing-mbid", values)

    def test_seed_claims_are_marked_as_dataset_not_observation(self):
        self.store.put(library_claim({"isrc": ("GBN9Y1100088",)}).sign(self.me), ORIGIN_LOCAL)
        self._build()
        entry = self.store.resolve("track", library_claim({}).key)["musicbrainz"][0]
        self.assertEqual(entry["observers"], 0)
        self.assertEqual(entry["datasets"], 1)

    def test_a_seed_never_corroborates_a_library(self):
        """Both assert the same id; support must stay one observer, one dataset
        — not two of anything."""
        self.store.put(
            library_claim({"isrc": ("GBN9Y1100088",), "musicbrainz": (MBID,)}).sign(self.me),
            ORIGIN_LOCAL)
        self._build(only_missing=False)
        entry = self.store.resolve("track", library_claim({}).key)["musicbrainz"][0]
        self.assertEqual(entry["observers"], 1)
        self.assertEqual(entry["datasets"], 1)

    def test_unmatched_isrcs_are_ignored(self):
        self.store.put(library_claim({"isrc": ("NOTINMB00001",)}).sign(self.me), ORIGIN_LOCAL)
        stats = self._build()
        self.assertEqual(stats.claims, 0)

    def test_seed_claims_verify_like_any_other(self):
        self.store.put(library_claim({"isrc": ("GBN9Y1100088",)}).sign(self.me), ORIGIN_LOCAL)
        self._build()
        checked, failures = self.store.verify_all()
        self.assertEqual(failures, [])
        self.assertEqual(checked, 2)

    def test_one_isrc_shared_by_several_recordings_keeps_both(self):
        """MusicBrainz permits it, and both are legitimately claimable."""
        (self.dir / "isrc").write_text(
            "1\t500\tGBN9Y1100088\t0\t2011-05-16\n"
            "2\t501\tGBN9Y1100088\t0\t2011-05-16\n")
        self.store.put(library_claim({"isrc": ("GBN9Y1100088",)}).sign(self.me), ORIGIN_LOCAL)
        self._build()
        values = [e["value"] for e in
                  self.store.resolve("track", library_claim({}).key)["musicbrainz"]]
        self.assertEqual(len(values), 2)

    def test_wanted_isrcs_is_case_insensitive(self):
        self.store.put(library_claim({"isrc": ("gbn9y1100088",)}).sign(self.me), ORIGIN_LOCAL)
        by_isrc, _ = wanted_isrcs(self.store)
        self.assertIn("GBN9Y1100088", by_isrc)


if __name__ == "__main__":
    unittest.main()

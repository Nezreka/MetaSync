"""SoulID port tests — the golden vectors are the contract.

Vectors were generated from SoulSync's own implementation (PR #1883)
by tests/gen_soulid_golden_vectors.py. If this port and SoulSync ever
disagree, one of them changed a frozen network contract — that is a
bug by definition, not a test to update casually.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.meta import soulid

FIXTURE = (Path(__file__).resolve().parent / "fixtures"
           / "soulid_golden_vectors.json")


class TestGoldenVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_version_matches(self):
        self.assertEqual(self.data["soulid_version"], soulid.SOULID_VERSION)

    def test_all_vectors(self):
        for v in self.data["vectors"]:
            with self.subTest(kind=v["kind"], parts=v["parts"]):
                self.assertEqual(
                    [soulid.normalize_for_soul_id(p) for p in v["parts"]],
                    v["normalized"])
                self.assertEqual(
                    soulid.generate_soul_id(v["kind"], *v["parts"]),
                    v["soul_id"])

    def test_same_artist_accent_variants_one_id(self):
        self.assertEqual(soulid.generate_soul_id("artist", "Beyoncé"),
                         soulid.generate_soul_id("artist", "Beyonce"))


class TestArityAndEmpties(unittest.TestCase):
    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            soulid.generate_soul_id("song", "x")

    def test_wrong_arity(self):
        with self.assertRaises(ValueError):
            soulid.generate_soul_id("album", "only one part")

    def test_empty_part_mints_nothing(self):
        self.assertEqual(soulid.generate_soul_id("album", "Radiohead", ""), "")

    def test_album_and_title_track_do_not_collide(self):
        # the v1 bug kinded_v2 fixed: kind is in the hash input
        album = soulid.generate_soul_id("album", "Nirvana", "Nevermind")
        track = soulid.generate_soul_id("track", "Nirvana", "Nevermind")
        self.assertNotEqual(album, track)


if __name__ == "__main__":
    unittest.main()

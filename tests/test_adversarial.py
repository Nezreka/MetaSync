"""What happens when nodes lie — the question real users cannot be asked."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync.claim import Claim  # noqa: E402
from metasync.identity import NodeIdentity  # noqa: E402
from metasync.simulate import Population, run, sybil_threshold  # noqa: E402
from metasync.store import ORIGIN_LOCAL, ORIGIN_PEER, ClaimStore  # noqa: E402

NORM = {"artist": "pinkfloyd", "album": "animals", "title": "dogs"}


def claim(spotify_id):
    return Claim(entity="track", norm=NORM, ids={"spotify": (spotify_id,)},
                 observed_on="2026-08-19")


class LocalFirstTests(unittest.TestCase):
    """Node identities are free, so peer counts can be manufactured. What this
    machine saw in its own library must not be overridable by any number of
    strangers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ClaimStore(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_local_observation_outranks_any_number_of_peers(self):
        self.store.put(claim("mine").sign(NodeIdentity.generate()), ORIGIN_LOCAL)
        for _ in range(50):
            self.store.put(claim("theirs").sign(NodeIdentity.generate()), ORIGIN_PEER)
        top = self.store.resolve("track", claim("x").key)["spotify"][0]
        self.assertEqual(top["value"], "mine")
        self.assertTrue(top["mine"])

    def test_peers_still_rank_among_themselves(self):
        """Local-first is a floor, not a blindfold: without a local claim the
        best-supported peer value still wins."""
        for _ in range(3):
            self.store.put(claim("popular").sign(NodeIdentity.generate()), ORIGIN_PEER)
        self.store.put(claim("rare").sign(NodeIdentity.generate()), ORIGIN_PEER)
        values = [e["value"] for e in self.store.resolve("track", claim("x").key)["spotify"]]
        self.assertEqual(values[0], "popular")

    def test_local_claims_are_marked(self):
        self.store.put(claim("mine").sign(NodeIdentity.generate()), ORIGIN_LOCAL)
        self.assertTrue(self.store.resolve("track", claim("x").key)["spotify"][0]["mine"])


class SybilTests(unittest.TestCase):
    """These assertions record a known weakness rather than a defence. If the
    numbers move, the protocol's security changed and someone should know."""

    def test_honest_population_converges_on_the_truth(self):
        result = run(Population(honest=10, keys=120, seed=5))
        self.assertEqual(result.truth_rate, 1.0)

    def test_independent_liars_barely_matter(self):
        """Each liar invents its own falsehood, so they never corroborate one
        another and never out-count an honest majority."""
        result = run(Population(honest=10, liars=8, keys=120, seed=5))
        self.assertEqual(result.lie_ranked_first, 0)

    def test_a_small_sybil_group_defeats_a_sparse_majority(self):
        """The documented weakness: real libraries are sparse, an attacker is
        exhaustive, so a fake identity is worth several honest nodes."""
        threshold = sybil_threshold(honest=10, keys=60, seed=5)
        self.assertIsNotNone(threshold["sybils_needed"])
        self.assertLessEqual(threshold["sybils_needed"], 5)

    def test_local_observations_are_immune_to_any_sybil_count(self):
        small = run(Population(honest=10, sybils=10, keys=120, seed=5, local=True))
        huge = run(Population(honest=10, sybils=200, keys=120, seed=5, local=True))
        self.assertEqual(small.truth_rate, huge.truth_rate)
        self.assertGreater(huge.truth_rate, 0.25)


if __name__ == "__main__":
    unittest.main()

"""The claim store, and the corroboration model it implements."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync.claim import Claim  # noqa: E402
from metasync.identity import NodeIdentity  # noqa: E402
from metasync.store import ORIGIN_LOCAL, ORIGIN_PEER, ClaimStore  # noqa: E402


def claim_for(spotify_id="6VbjrBn8FgFHaFI6NxCSK5", observed_on="2026-08-19", title="dogs"):
    return Claim(
        entity="track",
        norm={"artist": "pinkfloyd", "album": "animals", "title": title},
        ids={"spotify": (spotify_id,)},
        facts={"duration": 1023},
        observed_on=observed_on,
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ClaimStore(Path(self._tmp.name) / "metasync.db")
        self.alice = NodeIdentity.generate()
        self.bob = NodeIdentity.generate()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_round_trip(self):
        claim = claim_for()
        self.store.put(claim.sign(self.alice), ORIGIN_LOCAL)
        held = self.store.get("track", claim.key)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].verify().ids["spotify"], ("6VbjrBn8FgFHaFI6NxCSK5",))

    def test_relaying_the_same_claim_does_not_inflate_the_count(self):
        """The anti-echo property: a claim copied a thousand times is still one
        observation, because it keeps its original signer."""
        signed = claim_for().sign(self.alice)
        for _ in range(50):
            self.store.put(signed, ORIGIN_PEER)
        resolved = self.store.resolve("track", claim_for().key)
        self.assertEqual(resolved["spotify"][0]["observers"], 1)

    def test_independent_observers_do_accumulate(self):
        claim = claim_for()
        self.store.put(claim.sign(self.alice), ORIGIN_LOCAL)
        self.store.put(claim.sign(self.bob), ORIGIN_PEER)
        resolved = self.store.resolve("track", claim.key)
        self.assertEqual(resolved["spotify"][0]["observers"], 2)

    def test_disagreement_is_kept_as_alternatives_ranked_by_support(self):
        """Editions collapse into one key, so several ids can be correct at
        once. The store ranks rather than picks a winner."""
        key = claim_for().key
        self.store.put(claim_for("aaa").sign(self.alice))
        self.store.put(claim_for("bbb").sign(self.bob))
        self.store.put(claim_for("bbb").sign(NodeIdentity.generate()))
        values = [entry["value"] for entry in self.store.resolve("track", key)["spotify"]]
        self.assertEqual(values, ["bbb", "aaa"])

    def test_min_observers_filters(self):
        key = claim_for().key
        self.store.put(claim_for("aaa").sign(self.alice))
        self.store.put(claim_for("bbb").sign(self.bob))
        self.store.put(claim_for("bbb").sign(NodeIdentity.generate()))
        resolved = self.store.resolve("track", key, min_observers=2)
        self.assertEqual([e["value"] for e in resolved["spotify"]], ["bbb"])

    def test_a_node_revising_its_claim_retracts_the_old_id(self):
        key = claim_for().key
        self.store.put(claim_for("aaa", observed_on="2026-08-01").sign(self.alice))
        self.store.put(claim_for("bbb", observed_on="2026-08-19").sign(self.alice))
        resolved = self.store.resolve("track", key)
        self.assertEqual([e["value"] for e in resolved["spotify"]], ["bbb"])

    def test_older_claim_does_not_overwrite_newer(self):
        key = claim_for().key
        self.store.put(claim_for("new", observed_on="2026-08-19").sign(self.alice))
        self.assertFalse(
            self.store.put(claim_for("old", observed_on="2026-01-01").sign(self.alice)))
        self.assertEqual(
            [e["value"] for e in self.store.resolve("track", key)["spotify"]], ["new"])

    def test_reverse_lookup(self):
        claim = claim_for()
        self.store.put(claim.sign(self.alice))
        self.assertEqual(self.store.find_by_id("spotify", "6VbjrBn8FgFHaFI6NxCSK5"),
                         [("track", claim.key)])

    def test_verify_all_passes_on_a_clean_store(self):
        self.store.put(claim_for().sign(self.alice))
        self.store.put(claim_for(title="pigs").sign(self.bob))
        checked, failures = self.store.verify_all()
        self.assertEqual(checked, 2)
        self.assertEqual(failures, [])

    def test_verify_all_catches_a_tampered_row(self):
        claim = claim_for()
        self.store.put(claim.sign(self.alice))
        self.store._conn.execute(
            "UPDATE claims SET payload = replace(payload, '6VbjrBn8FgFHaFI6NxCSK5', 'tampered0000000000000')")
        self.store._conn.commit()
        _, failures = self.store.verify_all()
        self.assertEqual(len(failures), 1)

    def test_stats(self):
        self.store.put(claim_for().sign(self.alice))
        self.store.put(claim_for(title="pigs").sign(self.bob))
        stats = self.store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["observers"], 2)
        self.assertEqual(stats["distinct_keys"], 2)

    def test_cursor_state(self):
        self.assertEqual(self.store.get_cursor("track"), "")
        self.store.set_cursor("track", "abc")
        self.assertEqual(self.store.get_cursor("track"), "abc")
        self.assertTrue(self.store.get_last_synced("track"))
        self.store.reset_cursor("track")
        self.assertEqual(self.store.get_cursor("track"), "")


if __name__ == "__main__":
    unittest.main()


class StatsDistinctionTests(unittest.TestCase):
    """`ids` is knowledge; `id_observations` is support for it. Conflating them
    makes a peer that merely agrees with you look like a peer that taught you
    something."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ClaimStore(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_corroboration_adds_support_not_knowledge(self):
        claim = claim_for()
        self.store.put(claim.sign(NodeIdentity.generate()))
        first = self.store.stats()
        self.store.put(claim.sign(NodeIdentity.generate()))
        second = self.store.stats()
        self.assertEqual(second["ids"], first["ids"])                       # no new knowledge
        self.assertEqual(second["id_observations"], first["id_observations"] + 1)

    def test_a_genuinely_new_id_adds_both(self):
        self.store.put(claim_for("aaa").sign(NodeIdentity.generate()))
        first = self.store.stats()
        self.store.put(claim_for("bbb").sign(NodeIdentity.generate()))
        second = self.store.stats()
        self.assertEqual(second["ids"], first["ids"] + 1)


class ProvenanceTests(unittest.TestCase):
    """A public dataset is one source however many nodes redistribute it.
    Counting it as independent observation would let one dump outvote every
    real person on the network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ClaimStore(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _dataset_claim(self, spotify_id="aaa"):
        from metasync.claim import SRC_DATASET
        return Claim(
            entity="track",
            norm={"artist": "pinkfloyd", "album": "animals", "title": "dogs"},
            ids={"spotify": (spotify_id,)},
            observed_on="2026-08-19",
            src=SRC_DATASET,
        )

    def test_dataset_and_observed_support_are_reported_separately(self):
        self.store.put(claim_for("aaa").sign(NodeIdentity.generate()))
        self.store.put(self._dataset_claim("aaa").sign(NodeIdentity.generate()))
        entry = self.store.resolve("track", claim_for().key)["spotify"][0]
        self.assertEqual(entry["observers"], 1)
        self.assertEqual(entry["datasets"], 1)

    def test_many_dataset_copies_do_not_look_like_many_observers(self):
        for _ in range(5):
            self.store.put(self._dataset_claim().sign(NodeIdentity.generate()))
        entry = self.store.resolve("track", claim_for().key)["spotify"][0]
        self.assertEqual(entry["observers"], 0)
        self.assertEqual(entry["datasets"], 5)

    def test_observed_entries_outrank_dataset_ones(self):
        self.store.put(claim_for("observed-id").sign(NodeIdentity.generate()))
        for _ in range(3):
            self.store.put(self._dataset_claim("dataset-id").sign(NodeIdentity.generate()))
        values = [e["value"] for e in self.store.resolve("track", claim_for().key)["spotify"]]
        self.assertEqual(values[0], "observed-id")

    def test_stats_separate_the_two_kinds_of_source(self):
        self.store.put(claim_for().sign(NodeIdentity.generate()))
        self.store.put(self._dataset_claim("zzz").sign(NodeIdentity.generate()))
        stats = self.store.stats()
        self.assertEqual(stats["observers"], 1)
        self.assertEqual(stats["dataset_sources"], 1)

    def test_an_old_store_gains_the_column_without_losing_claims(self):
        """Real stores predate this field; the migration must not drop them."""
        path = Path(self._tmp.name) / "old.db"
        legacy = ClaimStore(path)
        legacy.put(claim_for().sign(NodeIdentity.generate()))
        legacy._conn.execute("ALTER TABLE claims RENAME TO claims_tmp")
        legacy._conn.execute(
            "CREATE TABLE claims (node TEXT NOT NULL, entity TEXT NOT NULL, key TEXT NOT NULL,"
            " song_key TEXT, payload TEXT NOT NULL, sig TEXT NOT NULL,"
            " observed_on TEXT NOT NULL, origin TEXT NOT NULL, stored_at TEXT NOT NULL,"
            " PRIMARY KEY (node, entity, key))")
        legacy._conn.execute(
            "INSERT INTO claims SELECT node, entity, key, song_key, payload, sig,"
            " observed_on, origin, stored_at FROM claims_tmp")
        legacy._conn.execute("DROP TABLE claims_tmp")
        legacy._conn.commit()
        legacy.close()

        reopened = ClaimStore(path)
        self.assertEqual(reopened.stats()["total"], 1)
        self.assertEqual(reopened.stats()["observers"], 1)
        reopened.close()

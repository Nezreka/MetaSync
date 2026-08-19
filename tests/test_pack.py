"""Claim packs: the manual, transport-free form of the network."""

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync.claim import Claim  # noqa: E402
from metasync.identity import NodeIdentity  # noqa: E402
from metasync.pack import BadPack, ImportStats, import_pack, read_pack, write_pack  # noqa: E402
from metasync.store import ORIGIN_LOCAL, ORIGIN_PEER, ClaimStore  # noqa: E402


def claim(title="dogs", ids=None, observed_on="2026-08-19"):
    return Claim(
        entity="track",
        norm={"artist": "pinkfloyd", "album": "animals", "title": title},
        ids=ids or {"spotify": ("spotify-dogs",)},
        facts={"duration": 1023},
        observed_on=observed_on,
    )


class PackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.alice = NodeIdentity.generate()
        self.bob = NodeIdentity.generate()
        self.store = ClaimStore(self.dir / "alice.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_round_trip(self):
        self.store.put(claim().sign(self.alice), ORIGIN_LOCAL)
        path = self.dir / "a.pack"
        stats = write_pack(self.store, path)
        self.assertEqual(stats.claims, 1)
        self.assertTrue(path.exists())
        self.assertEqual(len(list(read_pack(path))), 1)

    def test_pack_defaults_to_own_observations_only(self):
        """Relaying is opt-in: by default a node shares what it saw itself."""
        self.store.put(claim("dogs").sign(self.alice), ORIGIN_LOCAL)
        self.store.put(claim("pigs").sign(self.bob), ORIGIN_PEER)
        stats = write_pack(self.store, self.dir / "own.pack")
        self.assertEqual(stats.claims, 1)

    def test_relay_includes_peer_claims_unchanged(self):
        """A relayed claim keeps its original signer — that is what stops a
        pack passing through ten hands from counting as ten observations."""
        self.store.put(claim("pigs").sign(self.bob), ORIGIN_PEER)
        path = self.dir / "relay.pack"
        write_pack(self.store, path, origin=None)
        relayed = list(read_pack(path))
        self.assertEqual(relayed[0].node, self.bob.node_id)
        relayed[0].verify()

    def test_no_partial_file_is_left_behind(self):
        self.store.put(claim().sign(self.alice), ORIGIN_LOCAL)
        path = self.dir / "a.pack"
        write_pack(self.store, path)
        self.assertEqual(list(self.dir.glob("*.partial")), [])


class ImportTests(unittest.TestCase):
    """Importing is where the network's value shows up or does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.alice = NodeIdentity.generate()
        self.bob = NodeIdentity.generate()
        self.mine = ClaimStore(self.dir / "mine.db")
        self.theirs = ClaimStore(self.dir / "theirs.db")

    def tearDown(self):
        self.mine.close()
        self.theirs.close()
        self._tmp.cleanup()

    def _pack_from_theirs(self, name="theirs.pack") -> Path:
        path = self.dir / name
        write_pack(self.theirs, path)
        return path

    def test_new_entity_is_learned(self):
        self.theirs.put(claim("sheep").sign(self.bob), ORIGIN_LOCAL)
        stats = import_pack(self.mine, self._pack_from_theirs())
        self.assertEqual(stats.accepted, 1)
        self.assertEqual(stats.new_keys, 1)
        self.assertEqual(stats.new_ids, 1)
        self.assertEqual(stats.corroborated_ids, 0)

    def test_known_entity_with_a_new_id_is_counted_as_learned(self):
        """The interesting case: we both hold the track, they resolved a
        provider we never did."""
        self.mine.put(claim(ids={"spotify": ("s1",)}).sign(self.alice), ORIGIN_LOCAL)
        self.theirs.put(claim(ids={"spotify": ("s1",), "deezer": ("d1",)}).sign(self.bob),
                        ORIGIN_LOCAL)
        stats = import_pack(self.mine, self._pack_from_theirs())
        self.assertEqual(stats.new_keys, 0)
        self.assertEqual(stats.new_ids, 1)          # deezer
        self.assertEqual(stats.corroborated_ids, 1)  # spotify

    def test_corroboration_raises_the_observer_count(self):
        self.mine.put(claim().sign(self.alice), ORIGIN_LOCAL)
        self.theirs.put(claim().sign(self.bob), ORIGIN_LOCAL)
        import_pack(self.mine, self._pack_from_theirs())
        resolved = self.mine.resolve("track", claim().key)
        self.assertEqual(resolved["spotify"][0]["observers"], 2)

    def test_importing_the_same_pack_twice_teaches_nothing_new(self):
        self.theirs.put(claim("sheep").sign(self.bob), ORIGIN_LOCAL)
        path = self._pack_from_theirs()
        import_pack(self.mine, path)
        second = import_pack(self.mine, path)
        self.assertEqual(second.new_ids, 0)
        self.assertEqual(second.new_keys, 0)
        resolved = self.mine.resolve("track", claim("sheep").key)
        self.assertEqual(resolved["spotify"][0]["observers"], 1)

    def test_tampered_claim_is_rejected(self):
        self.theirs.put(claim().sign(self.bob), ORIGIN_LOCAL)
        path = self._pack_from_theirs()
        lines = gzip.open(path, "rt", encoding="utf-8").read().splitlines()
        entry = json.loads(lines[1])
        entry["claim"]["ids"]["spotify"] = ["attacker-id"]
        lines[1] = json.dumps(entry)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

        stats = import_pack(self.mine, path)
        self.assertEqual(stats.accepted, 0)
        self.assertEqual(stats.rejected_signature, 1)
        self.assertEqual(self.mine.stats()["total"], 0)

    def test_forged_key_is_rejected_even_with_a_valid_signature(self):
        """A peer may sign whatever it likes; it still cannot bind a payload to
        a key that its own normalized parts do not hash to."""
        payload = claim().to_payload()
        payload["key"] = "soul_0000000000000000"
        forged = json.dumps({"claim": payload, "node": self.bob.node_id,
                             "sig": claim().sign(self.bob).sig})
        path = self.dir / "forged.pack"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps({"format": "metasync-pack/1", "created_on": "2026-08-19"}) + "\n")
            handle.write(forged + "\n")
        stats = import_pack(self.mine, path)
        self.assertEqual(stats.accepted, 0)
        self.assertEqual(self.mine.stats()["total"], 0)

    def test_own_claims_handed_back_are_flagged(self):
        self.theirs.put(claim().sign(self.alice), ORIGIN_LOCAL)
        stats = import_pack(self.mine, self._pack_from_theirs(), own_node=self.alice.node_id)
        self.assertEqual(stats.own_claims, 1)

    def test_older_claim_from_a_known_node_is_superseded(self):
        self.mine.put(claim(observed_on="2026-08-19").sign(self.bob), ORIGIN_PEER)
        self.theirs.put(claim(observed_on="2026-01-01").sign(self.bob), ORIGIN_LOCAL)
        stats = import_pack(self.mine, self._pack_from_theirs())
        self.assertEqual(stats.superseded, 1)
        self.assertEqual(stats.accepted, 0)

    def test_garbage_file_is_refused(self):
        path = self.dir / "junk.pack"
        path.write_bytes(b"not a gzip file at all")
        with self.assertRaises(BadPack):
            import_pack(self.mine, path)

    def test_wrong_format_version_is_refused(self):
        path = self.dir / "future.pack"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps({"format": "metasync-pack/99"}) + "\n")
        with self.assertRaises(BadPack):
            import_pack(self.mine, path)


if __name__ == "__main__":
    unittest.main()

"""Claim construction, signing, verification, and the allowlist boundary."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync import canonical, soulid  # noqa: E402
from metasync.claim import Claim, InvalidClaim, SignedClaim, validate  # noqa: E402
from metasync.identity import NodeIdentity  # noqa: E402


def track_claim(**overrides):
    base = dict(
        entity="track",
        norm={"artist": "pinkfloyd", "album": "animals", "title": "dogs"},
        ids={"spotify": ("6VbjrBn8FgFHaFI6NxCSK5",), "isrc": ("GBN9Y1100088",)},
        facts={"duration": 1023, "track_number": 2, "explicit": False},
        observed_on="2026-08-19",
    )
    base.update(overrides)
    return Claim(**base)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.identity = NodeIdentity.generate()

    def test_round_trip(self):
        signed = track_claim().sign(self.identity)
        restored = SignedClaim.from_wire(signed.to_wire()).verify()
        self.assertEqual(restored.norm["title"], "dogs")
        self.assertEqual(restored.ids["spotify"], ("6VbjrBn8FgFHaFI6NxCSK5",))

    def test_tampered_payload_fails(self):
        signed = track_claim().sign(self.identity)
        wire = signed.to_wire()
        wire["claim"]["ids"]["spotify"] = ["attackerControlledId"]
        with self.assertRaises(InvalidClaim):
            SignedClaim.from_wire(wire).verify()

    def test_signature_cannot_be_lifted_onto_another_node(self):
        """The node id is inside the signed bytes, so a relay cannot re-attribute
        an observation to itself and have it still verify."""
        signed = track_claim().sign(self.identity)
        wire = signed.to_wire()
        wire["node"] = NodeIdentity.generate().node_id
        with self.assertRaises(InvalidClaim):
            SignedClaim.from_wire(wire).verify()

    def test_node_id_is_stable_and_reloadable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.key"
            first = NodeIdentity.load_or_create(path)
            second = NodeIdentity.load_or_create(path)
            self.assertEqual(first.node_id, second.node_id)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class KeyIntegrityTests(unittest.TestCase):
    def test_key_is_derived_not_asserted(self):
        claim = track_claim()
        self.assertEqual(
            claim.key,
            soulid.release_track_key("pinkfloyd", "animals", "dogs"),
        )

    def test_forged_key_is_rejected(self):
        """A peer cannot bind a payload to a key it did not derive."""
        payload = track_claim().to_payload()
        payload["key"] = soulid.album_key("someone", "else")
        with self.assertRaises(InvalidClaim) as ctx:
            validate(payload)
        self.assertIn("does not match its normalized parts", str(ctx.exception))

    def test_forged_song_key_is_rejected(self):
        payload = track_claim().to_payload()
        payload["song_key"] = soulid.song_key("someone", "else")
        with self.assertRaises(InvalidClaim):
            validate(payload)

    def test_unnormalized_parts_are_rejected(self):
        """Parts must arrive already normalized, or two nodes holding the same
        entity would derive different keys from the same claim."""
        with self.assertRaises(InvalidClaim) as ctx:
            validate(track_claim(norm={"artist": "Pink Floyd", "album": "animals", "title": "dogs"}).to_payload())
        self.assertIn("must already be normalized", str(ctx.exception))


class ArtistKeyPathTests(unittest.TestCase):
    def test_canonical_path_is_cross_node(self):
        claim = Claim(
            entity="artist",
            norm={"artist": "pinkfloyd", "qualifier": "860"},
            key_path="canonical",
            observed_on="2026-08-19",
        )
        validate(claim.to_payload())
        self.assertTrue(claim.is_cross_node_key)

    def test_album_fallback_path_is_not_cross_node(self):
        """soulid_worker falls back to the alphabetically-first album in the
        local library — an install-dependent value. Such keys are structurally
        valid but must never be used as a rendezvous point."""
        claim = Claim(
            entity="artist",
            norm={"artist": "pinkfloyd", "qualifier": "animals"},
            key_path="album",
            observed_on="2026-08-19",
        )
        validate(claim.to_payload())
        self.assertFalse(claim.is_cross_node_key)

    def test_artist_claim_must_declare_its_path(self):
        payload = Claim(
            entity="artist",
            norm={"artist": "pinkfloyd"},
            observed_on="2026-08-19",
        ).to_payload()
        with self.assertRaises(InvalidClaim):
            validate(payload)

    def test_key_path_rejected_on_non_artist(self):
        with self.assertRaises(InvalidClaim):
            validate(track_claim(key_path="canonical").to_payload())


class AllowlistTests(unittest.TestCase):
    """The export boundary. These are the fields that must never travel."""

    def test_rejects_lyrics_and_prose(self):
        for name in ("genius_lyrics", "summary", "discogs_bio", "lastfm_wiki"):
            with self.assertRaises(InvalidClaim, msg=name):
                validate(track_claim(facts={"duration": 100, name: "..."}).to_payload())

    def test_rejects_local_and_privacy_fields(self):
        for name in ("file_path", "file_size", "bitrate", "play_count", "thumb_url"):
            with self.assertRaises(InvalidClaim, msg=name):
                validate(track_claim(facts={name: "x"}).to_payload())

    def test_rejects_unknown_top_level_keys(self):
        payload = track_claim().to_payload()
        payload["library_track_id"] = "12345"
        with self.assertRaises(InvalidClaim):
            validate(payload)

    def test_rejects_unclaimable_provider(self):
        with self.assertRaises(InvalidClaim):
            validate(track_claim(ids={"soulseek": ("someuser",)}).to_payload())

    def test_rejects_hydrabase_as_a_payload_id(self):
        """soul_id is the key, never a claimed id — otherwise a node could
        launder network-sourced identity back in as its own observation."""
        with self.assertRaises(InvalidClaim):
            validate(track_claim(ids={"hydrabase": ("soul_0000000000000000",)}).to_payload())

    def test_rejects_out_of_range_facts(self):
        with self.assertRaises(InvalidClaim):
            validate(track_claim(facts={"year": 99}).to_payload())
        with self.assertRaises(InvalidClaim):
            validate(track_claim(facts={"bpm": 100000}).to_payload())

    def test_rejects_unsorted_or_duplicate_ids(self):
        payload = track_claim().to_payload()
        payload["ids"]["spotify"] = ["b", "a"]
        with self.assertRaises(InvalidClaim):
            validate(payload)


class CanonicalEncodingTests(unittest.TestCase):
    def test_key_order_does_not_change_bytes(self):
        self.assertEqual(
            canonical.encode({"b": 1, "a": 2}),
            canonical.encode({"a": 2, "b": 1}),
        )

    def test_floats_are_rejected(self):
        """1.0 vs 1 vs 1.0000000000000002 is the classic cross-language
        canonicalization break; MetaSync carries no fractional values."""
        with self.assertRaises(canonical.NotCanonical):
            canonical.encode({"duration": 1023.0})

    def test_null_is_rejected(self):
        with self.assertRaises(canonical.NotCanonical):
            canonical.encode({"label": None})

    def test_output_is_pure_ascii(self):
        encoded = canonical.encode({"artist": "Beyoncé", "kanji": "東京"})
        encoded.decode("ascii")
        self.assertIn(b"\\u00e9", encoded)


if __name__ == "__main__":
    unittest.main()


class UnnamedKeyTests(unittest.TestCase):
    """SoulSync's `soul_unnamed_<library_pk>` fallback embeds the user's media
    server primary key. It is install-local and must never be relayed."""

    def test_unnamed_key_is_rejected(self):
        payload = track_claim().to_payload()
        payload["key"] = "soul_unnamed_84213"
        with self.assertRaises(InvalidClaim) as ctx:
            validate(payload)
        self.assertIn("not a derived soul id", str(ctx.exception))

    def test_unnamed_song_key_is_rejected(self):
        payload = track_claim().to_payload()
        payload["song_key"] = "soul_unnamed_84213"
        with self.assertRaises(InvalidClaim):
            validate(payload)

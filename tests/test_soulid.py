"""SoulID port tests, including conformance against the real SoulSync worker."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync import soulid  # noqa: E402

SOULSYNC_ROOT = Path(__file__).resolve().parents[3] / "SoulSync"


def _load_soulsync_soulid():
    """Import SoulSync's real soulid_worker, or return None if unavailable."""
    if not (SOULSYNC_ROOT / "core" / "soulid_worker.py").exists():
        return None
    sys.path.insert(0, str(SOULSYNC_ROOT))
    try:
        from core import soulid_worker
        return soulid_worker
    except Exception:
        return None
    finally:
        sys.path.remove(str(SOULSYNC_ROOT))


class NormalizationTests(unittest.TestCase):
    def test_strips_accents_case_and_punctuation(self):
        self.assertEqual(soulid.normalize_for_soul_id("Beyoncé"), "beyonce")
        self.assertEqual(soulid.normalize_for_soul_id("AC/DC"), "acdc")
        self.assertEqual(soulid.normalize_for_soul_id("Sigur Rós"), "sigurros")

    def test_drops_parentheticals(self):
        self.assertEqual(soulid.normalize_for_soul_id("Time (Remastered 2011)"), "time")
        self.assertEqual(soulid.normalize_for_soul_id("Halo [Live]"), "halo")

    def test_is_idempotent(self):
        """Claims carry pre-normalized parts and re-derive their key from them,
        so normalizing twice must equal normalizing once."""
        for raw in ["Beyoncé", "Time (Remastered 2011)", "AC/DC", "  spaced  out  "]:
            once = soulid.normalize_for_soul_id(raw)
            self.assertEqual(soulid.normalize_for_soul_id(once), once, raw)

    def test_empty_input_yields_empty_id(self):
        self.assertEqual(soulid.generate_soul_id(""), "")
        self.assertEqual(soulid.generate_soul_id("", None), "")


class KeyDerivationTests(unittest.TestCase):
    def test_song_key_collapses_versions(self):
        """The song-level key deliberately links versions of the same song."""
        self.assertEqual(
            soulid.song_key("Pink Floyd", "Time"),
            soulid.song_key("pink floyd", "Time (Remastered 2011)"),
        )

    def test_release_key_distinguishes_releases(self):
        """The release-level key does not — which is why it is the primary key
        for track claims: provider track ids are release-specific."""
        self.assertNotEqual(
            soulid.release_track_key("Pink Floyd", "The Dark Side of the Moon", "Time"),
            soulid.release_track_key("Pink Floyd", "Pulse", "Time"),
        )

    def test_album_key_collapses_editions(self):
        """Parentheticals are stripped, so the deluxe and standard editions share
        a key. Both editions' provider ids are legitimately claimable under it."""
        self.assertEqual(
            soulid.album_key("Pink Floyd", "The Dark Side of the Moon"),
            soulid.album_key("Pink Floyd", "The Dark Side of the Moon (Deluxe Edition)"),
        )

    def test_key_shape(self):
        key = soulid.album_key("Pink Floyd", "Animals")
        self.assertTrue(key.startswith("soul_"))
        self.assertEqual(len(key), 21)


class ConformanceTests(unittest.TestCase):
    """The port must agree with SoulSync byte for byte, or every key MetaSync
    computes is a key no SoulSync install would recognize."""

    @classmethod
    def setUpClass(cls):
        cls.worker = _load_soulsync_soulid()

    def setUp(self):
        if self.worker is None:
            self.skipTest(f"SoulSync checkout not importable at {SOULSYNC_ROOT}")

    def test_normalization_matches(self):
        samples = [
            "Beyoncé", "AC/DC", "Sigur Rós", "Time (Remastered 2011)",
            "Motörhead", "10,000 Maniacs", "!!!", "Björk", "P!nk",
            "Guns N' Roses", "Halo [Live]", "", "   ", "東京事変",
        ]
        for raw in samples:
            self.assertEqual(
                soulid.normalize_for_soul_id(raw),
                self.worker.normalize_for_soul_id(raw),
                f"normalization diverged on {raw!r}",
            )

    def test_generated_ids_match(self):
        cases = [
            ("Pink Floyd", "The Dark Side of the Moon", "Time"),
            ("Beyoncé", "I Am... Sasha Fierce", "Halo"),
            ("AC/DC", "Back in Black", "Hells Bells"),
        ]
        for parts in cases:
            self.assertEqual(
                soulid.generate_soul_id(*parts),
                self.worker.generate_soul_id(*parts),
                f"soul id diverged on {parts!r}",
            )

    def test_release_track_key_matches_worker_ordering(self):
        """soulid_worker.py:579 — generate_soul_id(artist, album, title)."""
        artist, album, title = "Pink Floyd", "Animals", "Dogs"
        self.assertEqual(
            soulid.release_track_key(artist, album, title),
            self.worker.generate_soul_id(artist, album, title),
        )

    def test_song_key_matches_worker_ordering(self):
        """soulid_worker.py:574 — generate_soul_id(artist, title)."""
        self.assertEqual(
            soulid.song_key("Pink Floyd", "Dogs"),
            self.worker.generate_soul_id("Pink Floyd", "Dogs"),
        )


if __name__ == "__main__":
    unittest.main()

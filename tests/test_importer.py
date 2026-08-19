"""Row → claim conversion: the provenance boundary."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync import soulid  # noqa: E402
from metasync.importer import (  # noqa: E402
    ImportStats, album_claim, artist_claim, claims_from_rows, collect_ids,
    recover_artist_qualifier, track_claim,
)


def track_row(**overrides):
    row = {
        "soul_id": soulid.song_key("Pink Floyd", "Dogs"),
        "album_soul_id": soulid.release_track_key("Pink Floyd", "Animals", "Dogs"),
        "title": "Dogs",
        "artist_name": "Pink Floyd",
        "album_title": "Animals",
        "track_number": 2, "disc_number": 1,
        "duration": 1023000,            # milliseconds, as SoulSync stores it
        "bpm": None, "explicit": False, "year": 1977,
        "isrc": "GBN9Y1100088",
        "spotify_track_id": "6VbjrBn8FgFHaFI6NxCSK5",
        "spotify_match_status": "matched",
        "deezer_id": "3135553",
        "deezer_match_status": "matched",
        "musicbrainz_recording_id": "0f4e4a1c-dead-beef-9b3c-abcdef012345",
        "musicbrainz_match_status": "not_found",
        "updated_at": "2026-08-19T00:00:00",
    }
    row.update(overrides)
    return row


class MatchStatusGateTests(unittest.TestCase):
    """Only ids this install resolved itself may be published."""

    def test_matched_ids_are_kept(self):
        ids = collect_ids(track_row(), "track")
        self.assertEqual(ids["spotify"], ("6VbjrBn8FgFHaFI6NxCSK5",))
        self.assertEqual(ids["deezer"], ("3135553",))

    def test_unmatched_ids_are_dropped(self):
        """The recording MBID is present but its status is 'not_found'."""
        ids = collect_ids(track_row(), "track")
        self.assertNotIn("musicbrainz", ids)

    def test_null_status_is_dropped(self):
        """NULL means the worker never confirmed it — that is a guess, not an
        observation, and publishing guesses is what the network exists to avoid."""
        stats = ImportStats()
        ids = collect_ids(track_row(spotify_match_status=None), "track", stats)
        self.assertNotIn("spotify", ids)
        self.assertEqual(stats.ids_dropped_unmatched, 2)  # spotify + musicbrainz

    def test_intrinsic_ids_need_no_status(self):
        """ISRC and UPC are issued by a standards body, not resolved by a match."""
        ids = collect_ids(track_row(), "track")
        self.assertEqual(ids["isrc"], ("GBN9Y1100088",))

    def test_soul_id_is_never_a_payload_id(self):
        ids = collect_ids(track_row(), "track")
        self.assertNotIn("hydrabase", ids)
        self.assertNotIn("soul_id", ids)


class TrackClaimTests(unittest.TestCase):
    def test_key_is_release_level(self):
        claim = track_claim(track_row(), ImportStats())
        self.assertEqual(claim.key, soulid.release_track_key("pinkfloyd", "animals", "dogs"))
        self.assertEqual(claim.song_key, soulid.song_key("pinkfloyd", "dogs"))

    def test_duration_converts_milliseconds_to_seconds(self):
        claim = track_claim(track_row(), ImportStats())
        self.assertEqual(claim.facts["duration"], 1023)

    def test_absurd_duration_is_dropped_not_clamped(self):
        claim = track_claim(track_row(duration=999_999_999_999), ImportStats())
        self.assertNotIn("duration", claim.facts)

    def test_track_without_album_is_skipped(self):
        """Provider track ids are release-specific, so a track with no release
        has nothing claimable about it."""
        stats = ImportStats()
        self.assertIsNone(track_claim(track_row(album_title=""), stats))
        self.assertEqual(stats.skipped_no_key, 1)

    def test_track_with_no_matched_ids_is_skipped(self):
        stats = ImportStats()
        row = track_row(spotify_match_status="error", deezer_match_status="error", isrc=None)
        self.assertIsNone(track_claim(row, stats))
        self.assertEqual(stats.skipped_no_ids, 1)

    def test_stale_stored_key_is_counted_but_claim_still_made(self):
        """SoulSync writes soul_id once and never recomputes it, so a retagged
        album leaves a stale id. The derived key is what other nodes compute."""
        stats = ImportStats()
        claim = track_claim(track_row(album_soul_id="soul_0000000000000000"), stats)
        self.assertIsNotNone(claim)
        self.assertEqual(stats.stale_stored_key, 1)
        self.assertEqual(claim.key, soulid.release_track_key("pinkfloyd", "animals", "dogs"))

    def test_explicit_false_survives(self):
        claim = track_claim(track_row(), ImportStats())
        self.assertIs(claim.facts["explicit"], False)


class ExcludedFieldTests(unittest.TestCase):
    """Even if a future SoulSync serializer regression leaks these, nothing in
    the claim path reads them."""

    def test_local_fields_never_reach_the_payload(self):
        leaked = {
            "id": "84213", "artist_id": "12", "album_id": "34",
            "file_path": "/music/Pink Floyd/Animals/02 Dogs.flac",
            "file_size": 88_200_000, "bitrate": 1411,
            "thumb_url": "http://192.168.1.10:32400/library/metadata/84213/thumb",
            "genius_lyrics": "You gotta be crazy...",
            "genius_description": "A song about...",
            "play_count": 42, "last_played": "2026-08-18",
            "server_source": "plex", "verification_status": "verified",
        }
        claim = track_claim(track_row(**leaked), ImportStats())
        blob = repr(claim.to_payload())
        for value in ("84213", "file_path", "/music/", "192.168", "crazy", "plex", "1411"):
            self.assertNotIn(value, blob, f"{value!r} leaked into the claim")


class ArtistClaimTests(unittest.TestCase):
    """The artist key is only reproducible on the canonical path, and even then
    only if the qualifier can be recovered from the row's own ids."""

    def artist_row(self, **overrides):
        name = "Pink Floyd"
        deezer, itunes = 860, 487143
        row = {
            "name": name,
            "soul_id": soulid.artist_key(name, str(max(deezer, itunes))),
            "soul_id_path": "canonical",
            "deezer_id": str(deezer), "deezer_match_status": "matched",
            "itunes_artist_id": str(itunes), "itunes_match_status": "matched",
            "genres": ["Progressive Rock", "Rock"],
        }
        row.update(overrides)
        return row

    def test_qualifier_recovered_from_max_of_provider_ids(self):
        row = self.artist_row()
        self.assertEqual(recover_artist_qualifier(row, "pinkfloyd"), "487143")

    def test_canonical_claim_is_built_and_verifiable(self):
        claim = artist_claim(self.artist_row(), ImportStats())
        self.assertIsNotNone(claim)
        self.assertTrue(claim.is_cross_node_key)
        self.assertEqual(claim.key, self.artist_row()["soul_id"])

    def test_album_fallback_path_is_refused(self):
        """That key hashes the alphabetically-first album in THIS library."""
        stats = ImportStats()
        self.assertIsNone(artist_claim(self.artist_row(soul_id_path="album"), stats))
        self.assertEqual(stats.skipped_artist_unverifiable, 1)

    def test_null_path_still_works_when_the_key_reproduces(self):
        """Most of a real library predates the soul_id_path column, so NULL is
        the common case. Reconstruction is the proof, not the label."""
        claim = artist_claim(self.artist_row(soul_id_path=None), ImportStats())
        self.assertIsNotNone(claim)
        self.assertTrue(claim.is_cross_node_key)

    def test_declared_name_path_is_refused(self):
        stats = ImportStats()
        self.assertIsNone(artist_claim(self.artist_row(soul_id_path="name"), stats))
        self.assertEqual(stats.skipped_artist_unverifiable, 1)

    def test_unrecoverable_qualifier_is_refused(self):
        """The lookup that produced the stored id is no longer visible in the
        row, so the key cannot be re-derived and the claim is dropped."""
        stats = ImportStats()
        row = self.artist_row(soul_id=soulid.artist_key("pinkfloyd", "999999999"))
        self.assertIsNone(artist_claim(row, stats))
        self.assertEqual(stats.skipped_artist_unverifiable, 1)


class AlbumClaimTests(unittest.TestCase):
    def album_row(self, **overrides):
        row = {
            "soul_id": soulid.album_key("Pink Floyd", "Animals"),
            "title": "Animals", "artist_name": "Pink Floyd",
            "year": 1977, "release_date": "1977-01-23T00:00:00",
            "track_count": 5, "record_type": "album", "label": "Harvest",
            "genres": ["Rock", "Progressive Rock", "Rock"],
            "upc": "5099902988726",
            "spotify_album_id": "34tz4eDHlKlAgFtC24oNzT",
            "spotify_match_status": "matched",
        }
        row.update(overrides)
        return row

    def test_release_date_is_trimmed_to_a_date(self):
        claim = album_claim(self.album_row(), ImportStats())
        self.assertEqual(claim.facts["release_date"], "1977-01-23")

    def test_bad_release_date_is_dropped(self):
        claim = album_claim(self.album_row(release_date="1977"), ImportStats())
        self.assertNotIn("release_date", claim.facts)

    def test_genres_are_deduped_and_sorted(self):
        claim = album_claim(self.album_row(), ImportStats())
        self.assertEqual(claim.facts["genres"], ["Progressive Rock", "Rock"])

    def test_upc_needs_no_match_status(self):
        claim = album_claim(self.album_row(), ImportStats())
        self.assertEqual(claim.ids["upc"], ("5099902988726",))


class BatchTests(unittest.TestCase):
    def test_stats_account_for_every_row(self):
        rows = [track_row(), track_row(album_title=""), track_row(spotify_match_status="error",
                                                                  deezer_match_status="error",
                                                                  isrc=None)]
        claims, stats = claims_from_rows(rows, "track")
        self.assertEqual(len(claims), 1)
        self.assertEqual(stats.rows, 3)
        self.assertEqual(stats.claims, 1)
        self.assertEqual(stats.skipped_no_key + stats.skipped_no_ids, 2)

    def test_every_produced_claim_validates(self):
        claims, _ = claims_from_rows([track_row()], "track")
        from metasync.claim import validate
        for claim in claims:
            validate(claim.to_payload())


if __name__ == "__main__":
    unittest.main()


class RealWorldTypeTests(unittest.TestCase):
    """Shapes observed in a live SoulSync database."""

    def test_float_bpm_is_rounded_not_dropped(self):
        """`bpm` comes back as 87.2 — a strict int() would silently lose it,
        and a float can never reach the wire (MCJ/1 rejects floats)."""
        claim = track_claim(track_row(bpm=87.2), ImportStats())
        self.assertEqual(claim.facts["bpm"], 87)

    def test_float_duration_is_handled(self):
        claim = track_claim(track_row(duration=273720.0), ImportStats())
        self.assertEqual(claim.facts["duration"], 274)

    def test_null_year_on_track_is_omitted(self):
        """Tracks carry year only sometimes; albums are where it lives."""
        claim = track_claim(track_row(year=None), ImportStats())
        self.assertNotIn("year", claim.facts)

    def test_string_numerics_are_accepted(self):
        claim = track_claim(track_row(track_number="4", duration="273720"), ImportStats())
        self.assertEqual(claim.facts["track_number"], 4)
        self.assertEqual(claim.facts["duration"], 274)

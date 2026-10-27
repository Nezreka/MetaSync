"""Verification tests (§7/§8): fake fetchers, recorded shapes, no
live network.

Strong = a cross-match where both sides come from provider
responses; plausible never marks strong; the sanity gate is
bounded per origin per field per rolling 24 h and closed to
origins with no verified history.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.identity import NodeIdentity
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta import verify as V
from metasync_node.meta.store import MetaStore, value_key

NOW = 1_800_000_000.0
DAY = 24 * 3600
ARTIST = S.generate_soul_id("artist", "Radiohead")
ALBUM = S.generate_soul_id("album", "Radiohead", "OK Computer")
TRACK = S.generate_soul_id("track", "Radiohead", "Airbag")
TRACK2 = S.generate_soul_id("track", "Radiohead", "Paranoid Android")
ISRC = "GBAYE9700182"


class FakeFetcher:
    """Answers from a {(method, arg): response} table; records
    every call so cache behavior is assertable."""

    def __init__(self, name, table=None):
        self.name = name
        self.table = table or {}
        self.calls = []

    def _do(self, method, arg):
        self.calls.append((method, arg))
        result = self.table.get((method, arg))
        if isinstance(result, Exception):
            raise result
        return result

    def track_by_id(self, arg):
        return self._do("track_by_id", arg)

    def track_by_isrc(self, arg):
        return self._do("track_by_isrc", arg)

    def album_by_id(self, arg):
        return self._do("album_by_id", arg)

    def album_tracks(self, arg):
        return self._do("album_tracks", arg)

    def artist_by_id(self, arg):
        return self._do("artist_by_id", arg)


def mb_track(isrc=ISRC, links=None, duration_ms=284000):
    return {"provider": "musicbrainz", "id": "mb-rec-1",
            "title": "Airbag", "artist": "Radiohead", "isrc": isrc,
            "duration_ms": duration_ms, "links": links or {},
            "album_id": None}


def dz_track(isrc=ISRC, duration_ms=284000):
    return {"provider": "deezer", "id": "12345", "title": "Airbag",
            "artist": "Radiohead", "isrc": isrc,
            "duration_ms": duration_ms, "links": {},
            "album_id": "3135556"}


def claim(subject, kind, field, value, source):
    return {"subject": subject, "kind": kind, "field": field,
            "value": value, "source": source,
            "soulid_version": C.SOULID_VERSION}


def track_claims(isrc=ISRC):
    return [
        claim(TRACK, "track", "name", ["Radiohead", "Airbag"],
              "file_tags"),
        claim(TRACK, "track", "deezer_id", "12345", "deezer"),
        claim(TRACK, "track", "recording_mbid", "mb-rec-1",
              "musicbrainz"),
        claim(TRACK, "track", "isrc", isrc, "file_tags"),
        claim(TRACK, "track", "duration_ms", 284460, "file_tags"),
    ]


class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MetaStore(self.tmp.name)
        self.origin = NodeIdentity(key="aa" * 32)
        self.clock = [NOW]

    def seed(self, claims):
        batch = C.make_batch(self.origin.public_key_b64url, claims,
                             asserted_at=int(NOW) - 60)
        env = C.sign_batch(self.origin.signing_private_key, batch)
        self.store.put_cached_batch(env, received_at=NOW)

    def verifier(self, mb_table=None, dz_table=None, **fetchers):
        fetchers.setdefault(
            "musicbrainz", FakeFetcher("musicbrainz", mb_table))
        fetchers.setdefault("deezer", FakeFetcher("deezer", dz_table))
        return V.Verifier(self.store, fetchers,
                          clock=lambda: self.clock[0])

    def tier(self, subject, field, value):
        verdicts = self.store.verdicts_for_subjects([subject])
        got = verdicts.get((self.origin.public_key_b64url, subject,
                            field, value_key(value)))
        return got["tier"] if got else None


class TrackStrongTest(VerifyCase):
    def test_isrc_cross_match_marks_strong(self):
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        out = verifier.verify_subject(TRACK)
        self.assertGreater(out["strong"], 0)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "recording_mbid", "mb-rec-1"),
                         "strong-verified")
        # duration rode the cross-match: within 2s of both reports.
        self.assertEqual(self.tier(TRACK, "duration_ms", 284460),
                         "strong-verified")
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertGreaterEqual(rep["checks_passed"], 4)
        self.assertEqual(rep["checks_failed"], 0)

    def test_link_only_cross_match_is_strong(self):
        # ISRCs differ between the responses, but the MusicBrainz
        # response links to the very Deezer id in hand: §7's link
        # form of a cross-match.
        self.seed(track_claims(isrc="FRXXX0000001"))
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(isrc="FRXXX0000001",
                               links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"):
                      dz_track(isrc="FRXXX0000001")})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")

    def test_single_response_is_plausible_never_strong(self):
        self.seed(track_claims())
        verifier = self.verifier(
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC), "plausible")
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "plausible")
        self.assertEqual(self.tier(TRACK, "duration_ms", 284460),
                         "plausible")
        self.assertIsNone(self.tier(TRACK, "recording_mbid", "mb-rec-1"))

    def test_unasserted_isrc_is_not_a_contradiction(self):
        # SF-7: track ids and ISRCs are SETS (§8). A claimed ISRC
        # the cross-matched pair does not carry may be a true
        # live/remaster/edit twin — it earns NO verdict, but it is
        # NOT failed either: no verdict clears, no failed check is
        # charged to its origin. (The recording MBID keeps the
        # contradiction path — see the absence/recheck tests.)
        self.seed(track_claims(isrc="WRONG0000000"))
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        out = verifier.verify_subject(TRACK)
        self.assertEqual(out["failed"], 0)
        self.assertIsNone(self.tier(TRACK, "isrc", "WRONG0000000"))
        # The cross-match itself is unaffected: the claimed ids
        # the pair DOES carry still grade strong.
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "recording_mbid", "mb-rec-1"),
                         "strong-verified")
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertEqual(rep["checks_failed"], 0)

    def test_title_mismatch_establishes_nothing(self):
        self.seed(track_claims())
        wrong = mb_track()
        wrong["title"] = "Creep"
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"): wrong},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        # The MB response is not this track; only Deezer's single
        # response remains, and it can only make things plausible.
        self.assertEqual(self.tier(TRACK, "isrc", ISRC), "plausible")
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertEqual(rep["checks_failed"], 0)


class FetchEtiquetteTest(VerifyCase):
    def test_responses_cached_90_days(self):
        self.seed(track_claims())
        mb = FakeFetcher("musicbrainz",
                         {("track_by_id", "mb-rec-1"):
                          mb_track(links={"deezer": "12345"})})
        dz = FakeFetcher("deezer",
                         {("track_by_id", "12345"): dz_track()})
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb, "deezer": dz},
                              clock=lambda: self.clock[0])
        verifier.verify_subject(TRACK)
        first_calls = len(mb.calls) + len(dz.calls)
        self.assertGreater(first_calls, 0)
        verifier.verify_subject(TRACK)
        self.assertEqual(len(mb.calls) + len(dz.calls), first_calls)
        # Past 90 days the same lookups refetch (re-verification).
        # The unverdicted claims have expired by then (§6) — the
        # design's answer is a re-ask, so the origin's answer is
        # re-minted fresh at the advanced time (MF-1: the verifier
        # never grades the expired rows themselves).
        self.clock[0] += 100 * DAY
        batch = C.make_batch(self.origin.public_key_b64url,
                             track_claims(),
                             asserted_at=int(self.clock[0]) - 60)
        self.store.put_cached_batch(
            C.sign_batch(self.origin.signing_private_key, batch),
            received_at=self.clock[0])
        verifier.verify_subject(TRACK)
        self.assertGreater(len(mb.calls) + len(dz.calls), first_calls)

    def test_spotify_stub_never_configured_never_evidence(self):
        stub = V.SpotifyFetcher()
        with self.assertRaises(V.FetcherNotConfigured):
            stub.track_by_id("anything")
        self.seed(track_claims())
        verifier = self.verifier(spotify=stub)
        out = verifier.verify_subject(TRACK)
        self.assertEqual(out["failed"], 0)


class ArtistLinkTest(VerifyCase):
    def test_fetched_link_grades_strong(self):
        self.seed([
            claim(ARTIST, "artist", "name", ["Radiohead"], "file_tags"),
            claim(ARTIST, "artist", "artist_mbid", "mb-art-1",
                  "musicbrainz"),
            claim(ARTIST, "artist", "deezer_id", "399", "deezer"),
        ])
        verifier = self.verifier(mb_table={
            ("artist_by_id", "mb-art-1"): {
                "provider": "musicbrainz", "id": "mb-art-1",
                "name": "Radiohead", "links": {"deezer": "399"}},
        })
        verifier.verify_subject(ARTIST)
        self.assertEqual(self.tier(ARTIST, "deezer_id", "399"),
                         "strong-verified")
        self.assertEqual(self.tier(ARTIST, "artist_mbid", "mb-art-1"),
                         "strong-verified")


class AlbumTracklistTest(VerifyCase):
    def test_tracklist_fingerprint_grades_slots_strong(self):
        slots = [
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 1,
                   "variant": "mb:rel-1"}, "musicbrainz"),
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK2, "disc": 1, "position": 2,
                   "variant": "mb:rel-1"}, "musicbrainz"),
        ]
        self.seed([
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
            claim(TRACK, "track", "name", ["Radiohead", "Airbag"],
                  "file_tags"),
            claim(TRACK2, "track", "name",
                  ["Radiohead", "Paranoid Android"], "file_tags"),
            *slots,
        ])
        tracks = [{"disc": 1, "position": 1, "title": "Airbag",
                   "duration_ms": 284000, "isrc": None, "id": "r1"},
                  {"disc": 1, "position": 2,
                   "title": "Paranoid Android", "duration_ms": 383000,
                   "isrc": None, "id": "r2"}]
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"}, "release_id": "rel-1",
                "tracks": tracks},
                # MF-5: an mb: variant grades against the NAMED
                # release's tracklist, fetched by name.
                ("album_tracks", "rel-1"): tracks},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": tracks}})
        verifier.verify_subject(ALBUM)
        self.assertEqual(
            self.tier(ALBUM, "release_group_mbid", "mb-rg-1"),
            "strong-verified")
        self.assertEqual(self.tier(ALBUM, "deezer_id", "3135556"),
                         "strong-verified")
        for slot in slots:
            self.assertEqual(
                self.tier(ALBUM, "album_track", slot["value"]),
                "strong-verified")


class ForceRecheckTest(VerifyCase):
    """MF-2: a re-check must be able to re-fetch past the 90-day
    cache, and fresh evidence must be able to overturn a verdict
    the cache would otherwise have replayed forever."""

    def _strong_verifier(self):
        self.seed(track_claims())
        return self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})

    def test_unforced_recheck_replays_the_cache(self):
        verifier = self._strong_verifier()
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        mb = verifier.fetchers["musicbrainz"]
        calls = len(mb.calls)
        # The providers now contradict the claim — an unforced
        # run never hears it: the fresh cache answers instead.
        mb.table[("track_by_id", "mb-rec-1")] = mb_track(
            isrc="GBAYE9700999", links={"deezer": "12345"})
        verifier.verify_subject(TRACK)
        self.assertEqual(len(mb.calls), calls)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")

    def test_forced_recheck_refetches_and_overturns(self):
        verifier = self._strong_verifier()
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        mb = verifier.fetchers["musicbrainz"]
        dz = verifier.fetchers["deezer"]
        # Both providers now agree the recording carries a
        # DIFFERENT isrc: the claimed one stands contradicted.
        mb.table[("track_by_id", "mb-rec-1")] = mb_track(
            isrc="GBAYE9700999", links={"deezer": "12345"})
        dz.table[("track_by_id", "12345")] = dz_track(
            isrc="GBAYE9700999")
        calls = len(mb.calls)
        verifier.verify_subject(TRACK, force=True)
        self.assertGreater(len(mb.calls), calls)  # really re-fetched
        self.assertIsNone(self.tier(TRACK, "isrc", ISRC))  # cleared
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertGreaterEqual(rep["checks_failed"], 1)


class EvidenceTimeTest(VerifyCase):
    """MF-2: verified_at is when the EVIDENCE was fetched, not
    when the grading happened."""

    def test_verdict_stamped_with_evidence_time(self):
        self.seed(track_claims())
        evidence_at = NOW - 80 * DAY
        # The provider responses sit in the fetch cache as they
        # were fetched 80 days ago; grading happens "today" (the
        # fetchers' tables are empty — any real fetch would 404).
        self.store.fetch_store(
            "musicbrainz", "track_by_id:mb-rec-1", evidence_at,
            False, mb_track(links={"deezer": "12345"}))
        self.store.fetch_store(
            "deezer", "track_by_id:12345", evidence_at, False,
            dz_track())
        verifier = self.verifier()
        verifier.verify_subject(TRACK)
        verdicts = self.store.verdicts_for_subjects([TRACK])
        got = verdicts[(self.origin.public_key_b64url, TRACK,
                        "isrc", value_key(ISRC))]
        self.assertEqual(got["tier"], "strong-verified")
        self.assertEqual(got["verified_at"], evidence_at)


class MonotonicVerdictTest(VerifyCase):
    """R2-SF-1: a lesser grade never overwrites a stronger
    verdict; only a contradiction (clear_verdict) removes one."""

    def test_store_verdicts_only_move_up(self):
        key = self.origin.public_key_b64url
        self.store.set_verdict(key, TRACK, "isrc", ISRC,
                               "plausible", NOW, "one")
        self.assertEqual(self.tier(TRACK, "isrc", ISRC), "plausible")
        self.store.set_verdict(key, TRACK, "isrc", ISRC,
                               "strong-verified", NOW + 10, "two")
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        self.store.set_verdict(key, TRACK, "isrc", ISRC,
                               "plausible", NOW + 20, "three")
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        self.store.clear_verdict(key, TRACK, "isrc", ISRC)
        self.assertIsNone(self.tier(TRACK, "isrc", ISRC))
        self.store.set_verdict(key, TRACK, "isrc", ISRC,
                               "plausible", NOW + 30, "four")
        self.assertEqual(self.tier(TRACK, "isrc", ISRC), "plausible")

    def test_weaker_recheck_does_not_downgrade(self):
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        # MusicBrainz goes dark; the forced re-check sees only
        # the Deezer single response — plausible at best. The
        # strong verdicts survive untouched.
        verifier.fetchers["musicbrainz"].table.clear()
        verifier.verify_subject(TRACK, force=True)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")

    def test_unreachable_provider_changes_nothing(self):
        class Down:
            name = "musicbrainz"

            def track_by_id(self, arg):
                raise V.FetcherError("down")

            def track_by_isrc(self, arg):
                raise V.FetcherError("down")

        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        down = self.verifier(dz_table={("track_by_id", "12345"):
                                       dz_track()},
                             musicbrainz=Down())
        down.verify_subject(TRACK, force=True)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")


class AllClaimedIdsTest(VerifyCase):
    """R2-SF-2: album/artist verification tries EVERY claimed id
    until one identity-matches — the first id that merely
    resolves used to poison the whole check."""

    def test_album_second_group_id_matches(self):
        self.seed([
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid",
                  "mb-aaa-wrong", "musicbrainz"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
        ])
        wrong = {"provider": "musicbrainz", "id": "mb-aaa-wrong",
                 "title": "Kid A", "artist": "Radiohead",
                 "upc": None, "year": 2000, "links": {},
                 "release_id": "rel-9", "tracks": []}
        right = {"provider": "musicbrainz", "id": "mb-rg-1",
                 "title": "OK Computer", "artist": "Radiohead",
                 "upc": None, "year": 1997,
                 "links": {"deezer": "3135556"},
                 "release_id": "rel-1", "tracks": []}
        dz = {"provider": "deezer", "id": "3135556",
              "title": "OK Computer", "artist": "Radiohead",
              "upc": "123", "year": 1997, "links": {},
              "release_id": None, "tracks": []}
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-aaa-wrong"): wrong,
                      ("album_by_id", "mb-rg-1"): right},
            dz_table={("album_by_id", "3135556"): dz})
        verifier.verify_subject(ALBUM)
        self.assertEqual(
            self.tier(ALBUM, "release_group_mbid", "mb-rg-1"),
            "strong-verified")
        self.assertEqual(self.tier(ALBUM, "deezer_id", "3135556"),
                         "strong-verified")

    def test_artist_second_mbid_matches(self):
        self.seed([
            claim(ARTIST, "artist", "name", ["Radiohead"],
                  "file_tags"),
            claim(ARTIST, "artist", "artist_mbid", "mb-aaa-wrong",
                  "musicbrainz"),
            claim(ARTIST, "artist", "artist_mbid", "mb-art-1",
                  "musicbrainz"),
            claim(ARTIST, "artist", "deezer_id", "399", "deezer"),
        ])
        verifier = self.verifier(mb_table={
            ("artist_by_id", "mb-aaa-wrong"): {
                "provider": "musicbrainz", "id": "mb-aaa-wrong",
                "name": "Radiohed", "links": {}},
            ("artist_by_id", "mb-art-1"): {
                "provider": "musicbrainz", "id": "mb-art-1",
                "name": "Radiohead", "links": {"deezer": "399"}},
        })
        verifier.verify_subject(ARTIST)
        self.assertEqual(self.tier(ARTIST, "deezer_id", "399"),
                         "strong-verified")
        self.assertEqual(self.tier(ARTIST, "artist_mbid", "mb-art-1"),
                         "strong-verified")


class IsrcSetTest(VerifyCase):
    """R2-SF-3: MusicBrainz recordings carry the FULL ISRC set;
    cross-matches and contradiction checks run against the set."""

    def test_cross_match_uses_any_isrc_in_the_set(self):
        self.seed(track_claims())
        mb_resp = mb_track(isrc="GBAYE9700999")  # first-listed differs
        mb_resp["isrcs"] = ["GBAYE9700999", ISRC]
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"): mb_resp},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")


class StubResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class StubSession:
    """Routes by URL substring (first match wins); records the
    URLs asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        for needle, resp in self.routes:
            if needle in url:
                return resp
        return StubResponse(404, {})


class FetcherHonestyTest(unittest.TestCase):
    """R2-SF-5 + R2-SF-3 at the fetcher seam: composite fetches
    fail rather than caching partial successes, Deezer paginates,
    truncation is labeled, recordings carry every ISRC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_recording_carries_the_full_isrc_set(self):
        session = StubSession([("/recording/", StubResponse(200, {
            "id": "mb-rec-1", "title": "Airbag",
            "artist-credit": [{"name": "Radiohead"}],
            "isrcs": ["GBAYE9700999", ISRC], "length": 284000,
            "relations": []}))])
        fetcher = V.MusicBrainzFetcher(
            "test-agent", session=session, min_interval_s=0,
            sleep=lambda s: None)
        got = fetcher.track_by_id("mb-rec-1")
        self.assertEqual(got["isrcs"], ["GBAYE9700999", ISRC])
        self.assertEqual(got["isrc"], "GBAYE9700999")

    def test_musicbrainz_partial_album_is_refused(self):
        session = StubSession([
            ("/release-group/", StubResponse(200, {
                "id": "mb-rg-1", "title": "OK Computer",
                "artist-credit": [{"name": "Radiohead"}],
                "first-release-date": "1997-05-21",
                "releases": [{"id": "rel-1", "status": "Official",
                              "date": "1997-05-21"}],
                "relations": []})),
            ("/release/rel-1", StubResponse(404, {})),
        ])
        fetcher = V.MusicBrainzFetcher(
            "test-agent", session=session, min_interval_s=0,
            sleep=lambda s: None)
        with self.assertRaises(V.FetcherError):
            fetcher.album_by_id("mb-rg-1")

    def test_partial_album_caches_nothing(self):
        store = MetaStore(self.tmp.name)
        session = StubSession([
            ("/release-group/", StubResponse(200, {
                "id": "mb-rg-1", "title": "OK Computer",
                "artist-credit": [{"name": "Radiohead"}],
                "first-release-date": "1997-05-21",
                "releases": [{"id": "rel-1", "status": "Official",
                              "date": "1997-05-21"}],
                "relations": []})),
            ("/release/rel-1", StubResponse(404, {})),
        ])
        fetcher = V.MusicBrainzFetcher(
            "test-agent", session=session, min_interval_s=0,
            sleep=lambda s: None)
        verifier = V.Verifier(store, {"musicbrainz": fetcher},
                              clock=lambda: NOW)
        origin = NodeIdentity(key="aa" * 32)
        batch = C.make_batch(origin.public_key_b64url, [
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
        ], asserted_at=int(NOW) - 60)
        store.put_cached_batch(
            C.sign_batch(origin.signing_private_key, batch),
            received_at=NOW)
        verifier.verify_subject(ALBUM)
        self.assertIsNone(store.fetch_cached(
            "musicbrainz", "album_by_id:mb-rg-1"))

    def test_deezer_album_tracks_paginates(self):
        def track_json(i):
            return {"id": 1000 + i, "title": f"T{i}",
                    "duration": 200, "disk_number": 1,
                    "track_position": i + 1, "isrc": None}

        session = StubSession([
            ("index=100", StubResponse(200, {
                "data": [track_json(i) for i in range(100, 150)],
                "total": 150})),
            ("index=0", StubResponse(200, {
                "data": [track_json(i) for i in range(100)],
                "total": 150})),
        ])
        fetcher = V.DeezerFetcher(session=session,
                                  min_interval_s=0,
                                  sleep=lambda s: None)
        got = fetcher.album_tracks("3135556")
        self.assertEqual(len(got), 150)

    def test_deezer_truncation_is_labeled(self):
        def track_json(i):
            return {"id": 1000 + i, "title": f"T{i}",
                    "duration": 200, "disk_number": 1,
                    "track_position": i + 1, "isrc": None}

        session = StubSession([
            ("/tracks?", StubResponse(200, {
                "data": [track_json(i) for i in range(100)],
                "total": 100})),
            ("/album/3135556", StubResponse(200, {
                "id": 3135556, "title": "OK Computer",
                "artist": {"name": "Radiohead"}, "upc": "123",
                "nb_tracks": 200, "release_date": "1997-05-21"})),
        ])
        fetcher = V.DeezerFetcher(session=session,
                                  min_interval_s=0,
                                  sleep=lambda s: None)
        got = fetcher.album_by_id("3135556")
        self.assertEqual(len(got["tracks"]), 100)
        self.assertTrue(got["tracks_truncated"])

    def test_deezer_partial_album_is_refused(self):
        session = StubSession([
            ("/tracks?", StubResponse(404, {})),
            ("/album/3135556", StubResponse(200, {
                "id": 3135556, "title": "OK Computer",
                "artist": {"name": "Radiohead"}, "upc": "123",
                "nb_tracks": 12, "release_date": "1997-05-21"})),
        ])
        fetcher = V.DeezerFetcher(session=session,
                                  min_interval_s=0,
                                  sleep=lambda s: None)
        with self.assertRaises(V.FetcherError):
            fetcher.album_by_id("3135556")


class NamedReleaseTracklistTest(VerifyCase):
    """R2-SF-10: an mb:<release_id> variant is checked against
    the tracklist of the release it NAMES — not only the one
    release the release-group path happened to pick."""

    def test_named_release_tracklist_grades_the_variant(self):
        slots = [
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 1,
                   "variant": "mb:rel-9"}, "musicbrainz"),
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK2, "disc": 1, "position": 2,
                   "variant": "mb:rel-9"}, "musicbrainz"),
        ]
        self.seed([
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
            claim(TRACK, "track", "name", ["Radiohead", "Airbag"],
                  "file_tags"),
            claim(TRACK2, "track", "name",
                  ["Radiohead", "Paranoid Android"], "file_tags"),
            *slots,
        ])
        picked = [{"disc": 1, "position": 1,
                   "title": "Airbag (2017 Remaster)",
                   "duration_ms": 284000, "isrc": None, "id": "x1"},
                  {"disc": 1, "position": 2,
                   "title": "Paranoid Android (2017 Remaster)",
                   "duration_ms": 383000, "isrc": None, "id": "x2"}]
        named = [{"disc": 1, "position": 1, "title": "Airbag",
                  "duration_ms": 284000, "isrc": None, "id": "r1"},
                 {"disc": 1, "position": 2,
                  "title": "Paranoid Android",
                  "duration_ms": 383000, "isrc": None, "id": "r2"}]
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"},
                "release_id": "rel-1", "tracks": picked},
                ("album_tracks", "rel-9"): named},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": picked}})
        verifier.verify_subject(ALBUM)
        for slot in slots:
            self.assertEqual(
                self.tier(ALBUM, "album_track", slot["value"]),
                "strong-verified")


class VariantlessTracklistTest(VerifyCase):
    """R2-SF-11 on the verify side: variant-less claims group per
    asserting origin — one origin's matching tracklist grades
    ITS slots, another origin's different tracklist is untouched
    by the match."""

    def test_variantless_groups_are_per_origin(self):
        def seed_from(identity, slots):
            claims = [
                claim(ALBUM, "album", "name",
                      ["Radiohead", "OK Computer"], "file_tags"),
                claim(ALBUM, "album", "release_group_mbid",
                      "mb-rg-1", "musicbrainz"),
                claim(ALBUM, "album", "deezer_id", "3135556",
                      "deezer"),
                claim(TRACK, "track", "name",
                      ["Radiohead", "Airbag"], "file_tags"),
                claim(TRACK2, "track", "name",
                      ["Radiohead", "Paranoid Android"], "file_tags"),
                *slots,
            ]
            batch = C.make_batch(identity.public_key_b64url, claims,
                                 asserted_at=int(NOW) - 60)
            env = C.sign_batch(identity.signing_private_key, batch)
            self.store.put_cached_batch(env, received_at=NOW)

        good = [
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 1,
                   "variant": None}, "file_tags"),
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK2, "disc": 1, "position": 2,
                   "variant": None}, "file_tags"),
        ]
        other = NodeIdentity(key="bb" * 32)
        bad = [
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 1,
                   "variant": None}, "file_tags"),
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 2,
                   "variant": None}, "file_tags"),
        ]
        seed_from(self.origin, good)
        seed_from(other, bad)
        tracks = [{"disc": 1, "position": 1, "title": "Airbag",
                   "duration_ms": 284000, "isrc": None, "id": "r1"},
                  {"disc": 1, "position": 2,
                   "title": "Paranoid Android",
                   "duration_ms": 383000, "isrc": None, "id": "r2"}]
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"},
                "release_id": "rel-1", "tracks": tracks}},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": tracks}})
        verifier.verify_subject(ALBUM)
        for slot in good:
            self.assertEqual(
                self.tier(ALBUM, "album_track", slot["value"]),
                "strong-verified")
        verdicts = self.store.verdicts_for_subjects([ALBUM])
        bad_key = (other.public_key_b64url, ALBUM, "album_track",
                   value_key(bad[1]["value"]))
        self.assertNotIn(bad_key, verdicts)


class SharedPacerTest(unittest.TestCase):
    """R2-SF-6: one pacer per node — a call through the verifier's
    fetcher spaces the resolver's next call, and vice versa."""

    def test_pacer_is_shared_across_both_clients(self):
        from metasync_node import musicbrainz as MB

        sleeps = []
        pacer = MB.Pacer(10.0, sleep=sleeps.append)
        fetcher = V.MusicBrainzFetcher(
            "test-agent",
            session=StubSession([("/recording/", StubResponse(200, {
                "id": "mb-rec-1", "title": "Airbag",
                "artist-credit": [{"name": "Radiohead"}],
                "isrcs": [ISRC], "length": 284000,
                "relations": []}))]),
            pacer=pacer, sleep=lambda s: None)
        resolver = MB.MusicBrainzResolver(
            "https://musicbrainz.org/ws/2", "test-agent",
            session=StubSession([("/release/", StubResponse(
                200, {"release-group": {"id": "g1"}}))]),
            pacer=pacer, sleep=lambda s: None)
        self.assertIs(fetcher._pacer, pacer)
        self.assertIs(resolver._pacer, pacer)
        fetcher.track_by_id("mb-rec-1")  # first call: no wait
        self.assertEqual(sleeps, [])
        # The OTHER client's next call is the one that waits —
        # proof the interval is enforced across clients, not per
        # client.
        out = resolver.release_to_group("rel-1")
        self.assertEqual(out, "g1")
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 9.0)


class SanityGateTest(VerifyCase):
    def test_origin_without_history_cannot_trigger(self):
        verifier = self.verifier()
        self.assertFalse(verifier.note_conflict(
            self.origin.public_key_b64url, TRACK, "isrc"))

    def test_trigger_bounded_once_per_rolling_24h(self):
        # R2-SF-8: the window rolls — 23 h later is still inside
        # it (a calendar day would have reset at midnight), and
        # 25 h later it has lapsed.
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)  # earns a passed check
        key = self.origin.public_key_b64url
        self.assertTrue(verifier.note_conflict(key, TRACK, "isrc"))
        self.assertFalse(verifier.note_conflict(key, TRACK, "isrc"))
        self.assertTrue(verifier.note_conflict(key, TRACK, "deezer_id"))
        self.clock[0] += 23 * 3600
        self.assertFalse(verifier.note_conflict(key, TRACK, "isrc"))
        self.clock[0] += 2 * 3600
        self.assertTrue(verifier.note_conflict(key, TRACK, "isrc"))


class ExpiredClaimsVerifyTest(VerifyCase):
    """Gate MF-1 (verifier side): expired claims stay dead. The
    verifier reads claims through §6's expiry predicate — an
    expired, unverdicted row is never graded, so no verdict can
    resurrect it."""

    def test_expired_unverdicted_claims_are_never_verified(self):
        batch = C.make_batch(self.origin.public_key_b64url,
                             track_claims(),
                             asserted_at=int(NOW) - 100 * DAY)
        env = C.sign_batch(self.origin.signing_private_key, batch)
        self.store.put_cached_batch(env, received_at=NOW - 100 * DAY)
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        out = verifier.verify_subject(TRACK)
        self.assertEqual(out["strong"], 0)
        self.assertEqual(out["plausible"], 0)
        # Nothing was even fetched: the subject has no live claims.
        self.assertEqual(
            verifier.fetchers["musicbrainz"].calls, [])
        self.assertIsNone(self.tier(TRACK, "isrc", ISRC))
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertEqual(rep["checks_passed"], 0)


class VerificationBudgetTest(VerifyCase):
    """Gate SF-6: verification is bounded work — a per-run fetch
    cap, and failed lookups negative-cached behind a backoff."""

    def test_run_budget_caps_network_fetches(self):
        subjects = []
        for i in range(6):
            subj = S.generate_soul_id("track", "Radiohead", f"T{i}")
            subjects.append(subj)
            batch = C.make_batch(
                self.origin.public_key_b64url, [
                    claim(subj, "track", "name",
                          ["Radiohead", f"T{i}"], "file_tags"),
                    claim(subj, "track", "recording_mbid", f"mb-{i}",
                          "musicbrainz"),
                    claim(subj, "track", "isrc", f"ISRC{i}",
                          "file_tags"),
                ], asserted_at=int(NOW) - 60)
            self.store.put_cached_batch(
                C.sign_batch(self.origin.signing_private_key, batch),
                received_at=NOW)
        mb = FakeFetcher("musicbrainz", {})
        dz = FakeFetcher("deezer", {})
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb, "deezer": dz},
                              clock=lambda: self.clock[0])
        saved = V.MAX_FETCHES_PER_RUN
        V.MAX_FETCHES_PER_RUN = 5
        try:
            verifier.verify_subjects(subjects)
        finally:
            V.MAX_FETCHES_PER_RUN = saved
        total = len(mb.calls) + len(dz.calls)
        self.assertGreater(total, 0)
        self.assertLessEqual(total, 5)

    def test_failed_lookup_is_negative_cached_with_backoff(self):
        self.seed(track_claims())
        mb = FakeFetcher("musicbrainz", {
            ("track_by_id", "mb-rec-1"): V.FetcherError("down"),
            ("track_by_isrc", ISRC): V.FetcherError("down")})
        dz = FakeFetcher("deezer", {
            ("track_by_id", "12345"): V.FetcherError("down"),
            ("track_by_isrc", ISRC): V.FetcherError("down")})
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb, "deezer": dz},
                              clock=lambda: self.clock[0])
        verifier.verify_subject(TRACK)
        first = len(mb.calls) + len(dz.calls)
        self.assertGreater(first, 0)
        # Inside the backoff the dark providers are not re-asked.
        verifier.verify_subject(TRACK)
        self.assertEqual(len(mb.calls) + len(dz.calls), first)
        # Past it, the lookups are retried.
        self.clock[0] += 2 * 3600
        verifier.verify_subject(TRACK)
        self.assertGreater(len(mb.calls) + len(dz.calls), first)

    def test_subjects_ranked_by_origin_reputation(self):
        # The queue is spent best-evidence-first: the subject
        # whose asserting origin holds the better reputation is
        # verified first.
        def seed_from(identity, subj, title, mbid):
            batch = C.make_batch(identity.public_key_b64url, [
                claim(subj, "track", "name",
                      ["Radiohead", title], "file_tags"),
                claim(subj, "track", "recording_mbid", mbid,
                      "musicbrainz"),
                claim(subj, "track", "isrc", ISRC, "file_tags"),
            ], asserted_at=int(NOW) - 60)
            self.store.put_cached_batch(
                C.sign_batch(identity.signing_private_key, batch),
                received_at=NOW)

        good_subj = S.generate_soul_id("track", "Radiohead", "Good")
        bad_subj = S.generate_soul_id("track", "Radiohead", "Bad")
        other = NodeIdentity(key="bb" * 32)
        seed_from(self.origin, good_subj, "Good", "mb-good")
        seed_from(other, bad_subj, "Bad", "mb-bad")
        for _ in range(3):
            self.store.record_check(
                self.origin.public_key_b64url, True, NOW)
        verifier = self.verifier()
        out = verifier.verify_subjects([bad_subj, good_subj])
        self.assertEqual(
            [s["subject"] for s in out], [good_subj, bad_subj])


class LookupOrderTest(VerifyCase):
    """Gate r2 SF-4: within a subject, lookups are ordered by
    the asserting origin's standing, then local reputation, and
    capped per origin — forty bogus MBIDs from a no-standing,
    no-reputation origin can neither sort ahead of the honest
    id (they do, as strings) nor spend the subject budget."""

    def test_bogus_ids_sort_last_and_hit_the_per_origin_cap(self):
        bogus = NodeIdentity(key="bb" * 32)
        honest_claims = [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-honest",
                  "musicbrainz"),
            claim(TRACK, "track", "isrc", ISRC, "file_tags"),
        ]
        bogus_claims = [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
        ] + [
            claim(TRACK, "track", "recording_mbid", f"mb-{i:03d}",
                  "musicbrainz")
            for i in range(40)
        ]
        for identity, claims in ((self.origin, honest_claims),
                                 (bogus, bogus_claims)):
            batch = C.make_batch(identity.public_key_b64url, claims,
                                 asserted_at=int(NOW) - 60)
            self.store.put_cached_batch(
                C.sign_batch(identity.signing_private_key, batch),
                received_at=NOW)
        for _ in range(3):
            self.store.record_check(
                self.origin.public_key_b64url, True, NOW)
        mb = FakeFetcher("musicbrainz", {})
        dz = FakeFetcher("deezer", {})
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb, "deezer": dz},
                              clock=lambda: self.clock[0])
        verifier.verify_subject(TRACK)
        by_id = [arg for method, arg in mb.calls
                 if method == "track_by_id"]
        # The honest id is fetched FIRST despite sorting last as
        # a string, and the bogus origin's spend stops at its
        # per-subject allowance instead of eating all 40 slots.
        self.assertEqual(by_id[0], "mb-honest")
        self.assertEqual(len(by_id) - 1, V.MAX_FETCHES_PER_ORIGIN)
        self.assertLessEqual(len(mb.calls) + len(dz.calls),
                             V.MAX_FETCHES_PER_SUBJECT)


class SubjectPriorityExpiryTest(VerifyCase):
    """Gate r2 leftover: _subject_priority ranks on LIVE rows
    only — an expired row lends its origin's standing and
    reputation to nothing (MF-1's filter, applied to the queue
    ranking too)."""

    def _seed_at(self, identity, subj, title, mbid, age_s):
        batch = C.make_batch(identity.public_key_b64url, [
            claim(subj, "track", "name", ["Radiohead", title],
                  "file_tags"),
            claim(subj, "track", "recording_mbid", mbid,
                  "musicbrainz"),
        ], asserted_at=int(NOW) - age_s)
        self.store.put_cached_batch(
            C.sign_batch(identity.signing_private_key, batch),
            received_at=NOW - age_s + 60)

    def test_expired_rows_contribute_nothing(self):
        stale = S.generate_soul_id("track", "Radiohead", "Stale")
        fresh = S.generate_soul_id("track", "Radiohead", "Fresh")
        other = NodeIdentity(key="bb" * 32)
        self._seed_at(self.origin, stale, "Stale", "mb-stale",
                      100 * DAY)
        self._seed_at(other, fresh, "Fresh", "mb-fresh", 60)
        for _ in range(3):
            self.store.record_check(
                self.origin.public_key_b64url, True, NOW)
        self.store.record_check(other.public_key_b64url, True, NOW)
        verifier = self.verifier()
        # The stale subject's origin holds the better reputation,
        # but its only rows are 100 days old and unverdicted —
        # dead. It ranks as if it had no evidence at all, behind
        # the fresh subject whose origin has a single pass.
        self.assertEqual(verifier._subject_priority(stale),
                         (1, 0, stale))
        out = verifier.verify_subjects([stale, fresh])
        self.assertEqual([s["subject"] for s in out],
                         [fresh, stale])


class OwnAssertedLookupSkipsOriginCapTest(VerifyCase):
    """Gate closing note: the per-origin cap must not return
    _UNREACHABLE for a lookup of an id the user's OWN claims
    assert. The attacker co-asserts the user's ids and burns
    its whole per-subject allowance on bogus isrc lookups; the
    deezer lookup for the user's own id runs AFTER the flood in
    _verify_track's order, so pre-fix it arrived already capped
    and the user's own id was never graded. A purely
    network-asserted id from the same capped origin stays
    capped, exactly as before."""

    def test_own_id_co_asserted_by_capped_attacker_still_grades(
            self):
        me = NodeIdentity(key="cc" * 32)
        attacker = NodeIdentity(key="bb" * 32)
        self.store.replace_owner_claims(
            "seed:own", track_claims(), me.public_key_b64url,
            me.signing_private_key)
        attacker_claims = [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-rec-1",
                  "musicbrainz"),
            claim(TRACK, "track", "deezer_id", "12345", "deezer"),
            claim(TRACK, "track", "deezer_id", "99999", "deezer"),
        ] + [
            claim(TRACK, "track", "isrc", f"BOGUS{i:07d}",
                  "file_tags")
            for i in range(12)
        ]
        batch = C.make_batch(attacker.public_key_b64url,
                             attacker_claims,
                             asserted_at=int(NOW) - 60)
        self.store.put_cached_batch(
            C.sign_batch(attacker.signing_private_key, batch),
            received_at=NOW)
        mb = FakeFetcher("musicbrainz", {
            ("track_by_id", "mb-rec-1"):
                mb_track(links={"deezer": "12345"}),
            ("track_by_isrc", ISRC):
                mb_track(links={"deezer": "12345"}),
        })
        dz = FakeFetcher("deezer", {
            ("track_by_id", "12345"): dz_track(),
            ("track_by_isrc", ISRC): dz_track(),
        })
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb, "deezer": dz},
                              clock=lambda: self.clock[0])
        out = verifier.verify_subject(TRACK)
        # The flood really did cap the attacker: its purely
        # network-asserted deezer id was never fetched.
        self.assertGreaterEqual(
            verifier._origin_fetches[attacker.public_key_b64url],
            V.MAX_FETCHES_PER_ORIGIN)
        self.assertNotIn(("track_by_id", "99999"), dz.calls)
        # But the user's own id — co-asserted by the capped
        # attacker — was fetched anyway, and the cross-match
        # graded the co-asserted ids strong.
        self.assertIn(("track_by_id", "12345"), dz.calls)
        self.assertGreater(out["strong"], 0)
        verdicts = self.store.verdicts_for_subjects([TRACK])
        key = attacker.public_key_b64url
        self.assertEqual(
            verdicts[(key, TRACK, "deezer_id",
                      value_key("12345"))]["tier"],
            "strong-verified")
        self.assertEqual(
            verdicts[(key, TRACK, "recording_mbid",
                      value_key("mb-rec-1"))]["tier"],
            "strong-verified")


class SubjectStandingExpiryTest(VerifyCase):
    """Gate closing note: verify_subject's standing map applies
    the same _row_expired filter _subject_priority uses — an
    origin whose only standing rows are expired holds NO
    standing for lookup ordering."""

    def _seed_batch(self, identity, claims, asserted_at, standing):
        batch = C.make_batch(identity.public_key_b64url, claims,
                             asserted_at=asserted_at)
        self.store.put_cached_batch(
            C.sign_batch(identity.signing_private_key, batch),
            received_at=NOW, standing=standing)

    def test_expired_rows_contribute_no_standing(self):
        stale = NodeIdentity(key="bb" * 32)
        fresh = NodeIdentity(key="dd" * 32)
        # stale's only rows are 100 days old — dead under §6 —
        # though they were admitted with standing.
        self._seed_batch(stale, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-stale",
                  "musicbrainz"),
        ], int(NOW) - 100 * DAY, True)
        self._seed_batch(fresh, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-fresh",
                  "musicbrainz"),
        ], int(NOW) - 60, True)
        verifier = self.verifier()
        verifier.verify_subject(TRACK)
        self.assertFalse(verifier._subject_standing.get(
            stale.public_key_b64url))
        self.assertTrue(verifier._subject_standing[
            fresh.public_key_b64url])

    def test_expired_standing_does_not_win_lookup_order(self):
        # The mixed case: the origin's standing batch is expired
        # but it also holds a LIVE claim, admitted without
        # standing. Its id sorts first as a string; lookup order
        # follows standing, and its standing died with the
        # expired batch.
        mixed = NodeIdentity(key="bb" * 32)
        fresh = NodeIdentity(key="dd" * 32)
        self._seed_batch(mixed, [
            claim(TRACK, "track", "recording_mbid", "mb-dead",
                  "musicbrainz"),
        ], int(NOW) - 100 * DAY, True)
        self._seed_batch(mixed, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-aaa",
                  "musicbrainz"),
        ], int(NOW) - 60, False)
        self._seed_batch(fresh, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "recording_mbid", "mb-zzz",
                  "musicbrainz"),
        ], int(NOW) - 60, True)
        mb = FakeFetcher("musicbrainz", {})
        verifier = V.Verifier(self.store,
                              {"musicbrainz": mb,
                               "deezer": FakeFetcher("deezer", {})},
                              clock=lambda: self.clock[0])
        verifier.verify_subject(TRACK)
        by_id = [arg for method, arg in mb.calls
                 if method == "track_by_id"]
        self.assertEqual(by_id, ["mb-zzz", "mb-aaa"])


class PlausibleEarnsNoTriggerPowerTest(VerifyCase):
    """Gate SF-8: checks_passed — the §8 sanity gate's currency —
    is credited on STRONG grades only."""

    def test_plausible_grades_record_no_passed_check(self):
        self.seed(track_claims())
        verifier = self.verifier(
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC), "plausible")
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertEqual(rep["checks_passed"], 0)
        self.assertFalse(verifier.note_conflict(
            self.origin.public_key_b64url, TRACK, "isrc"))


class AbsenceTest(VerifyCase):
    """Gate SF-10 / §7.1: a previously verified id that two
    independent lookup paths agree is not-found loses its
    verdict; one path's 404 is not absence."""

    def _strong_verifier(self):
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")
        return verifier

    def test_two_not_found_paths_clear_a_verified_isrc(self):
        verifier = self._strong_verifier()
        mb = verifier.fetchers["musicbrainz"]
        dz = verifier.fetchers["deezer"]
        # Both providers now carry a DIFFERENT isrc for the
        # recording, and the claimed one resolves nowhere — the
        # by-isrc lookups at both providers answer not-found.
        mb.table[("track_by_id", "mb-rec-1")] = mb_track(
            isrc="GBAYE9700999", links={"deezer": "12345"})
        dz.table[("track_by_id", "12345")] = dz_track(
            isrc="GBAYE9700999")
        verifier.verify_subject(TRACK, force=True)
        self.assertIsNone(self.tier(TRACK, "isrc", ISRC))
        rep = self.store.reputation_for(self.origin.public_key_b64url)
        self.assertGreaterEqual(rep["checks_failed"], 1)

    def test_one_not_found_path_clears_nothing(self):
        verifier = self._strong_verifier()
        mb = verifier.fetchers["musicbrainz"]
        dz = verifier.fetchers["deezer"]
        # MusicBrainz loses the isrc; Deezer still resolves it —
        # one path's silence is not absence.
        mb.table[("track_by_id", "mb-rec-1")] = mb_track(
            isrc="GBAYE9700999", links={"deezer": "12345"})
        dz.table[("track_by_isrc", ISRC)] = dz_track()
        verifier.verify_subject(TRACK, force=True)
        self.assertEqual(self.tier(TRACK, "isrc", ISRC),
                         "strong-verified")


class AbsenceVouchingTest(VerifyCase):
    """Gate r2 SF-3: the absence rule's SECOND path must ride on
    a vouched id — asserted by the same origin whose verdict is
    on trial, or carrying its own strong verdict — and the two
    paths must be two different PROVIDERS. A stranger's invented
    id resolving nowhere proves nothing."""

    def _seed_from(self, identity, claims):
        batch = C.make_batch(identity.public_key_b64url, claims,
                             asserted_at=int(NOW) - 60)
        self.store.put_cached_batch(
            C.sign_batch(identity.signing_private_key, batch),
            received_at=NOW)

    def _strong_deezer_track(self):
        """The honest origin's deezer id, strong via the
        MusicBrainz link cross-match; the track carries NO isrc
        claim of the honest origin's."""
        self._seed_from(self.origin, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "deezer_id", "12345", "deezer"),
            claim(TRACK, "track", "recording_mbid", "mb-rec-1",
                  "musicbrainz"),
            claim(TRACK, "track", "duration_ms", 284460,
                  "file_tags"),
        ])
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        return verifier

    def test_strangers_isrc_cannot_be_the_second_path(self):
        verifier = self._strong_deezer_track()
        stranger = NodeIdentity(key="bb" * 32)
        self._seed_from(stranger, [
            claim(TRACK, "track", "name",
                  ["Radiohead", "Airbag"], "file_tags"),
            claim(TRACK, "track", "isrc", "FAKE000000001",
                  "file_tags"),
        ])
        # The honest ids now resolve NOWHERE — the deezer id's
        # own lookup 404s, and the only by-isrc paths run through
        # the stranger's invented ISRC, which also 404s at both
        # providers. One vouched path (the primary) is not two
        # providers: the honest verdict stands, uncharged.
        verifier.fetchers["musicbrainz"].table.clear()
        verifier.fetchers["deezer"].table.clear()
        failed_before = self.store.reputation_for(
            self.origin.public_key_b64url)["checks_failed"]
        verifier.verify_subject(TRACK, force=True)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        self.assertEqual(
            self.store.reputation_for(
                self.origin.public_key_b64url)["checks_failed"],
            failed_before)

    def test_same_origin_isrc_is_a_genuine_second_path(self):
        # The control: the honest origin asserted the ISRC
        # itself, so its by-isrc lookups — at the OTHER provider
        # too — are vouched second paths, and two-provider
        # absence clears as §7.1 intends.
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "deezer_id", "12345"),
                         "strong-verified")
        verifier.fetchers["musicbrainz"].table.clear()
        verifier.fetchers["deezer"].table.clear()
        verifier.verify_subject(TRACK, force=True)
        self.assertIsNone(self.tier(TRACK, "deezer_id", "12345"))
        self.assertIsNone(self.tier(TRACK, "isrc", ISRC))

    def test_two_lookups_at_one_provider_are_one_path(self):
        # MusicBrainz by-id AND by-isrc both 404 for the
        # recording, while Deezer still resolves everything: the
        # MBID's absence rests on ONE provider — its strong
        # verdict survives.
        self.seed(track_claims())
        verifier = self.verifier(
            mb_table={("track_by_id", "mb-rec-1"):
                      mb_track(links={"deezer": "12345"})},
            dz_table={("track_by_id", "12345"): dz_track()})
        verifier.verify_subject(TRACK)
        self.assertEqual(self.tier(TRACK, "recording_mbid",
                                   "mb-rec-1"), "strong-verified")
        mb = verifier.fetchers["musicbrainz"]
        dz = verifier.fetchers["deezer"]
        mb.table.clear()
        dz.table[("track_by_isrc", ISRC)] = dz_track()
        verifier.verify_subject(TRACK, force=True)
        self.assertEqual(self.tier(TRACK, "recording_mbid",
                                   "mb-rec-1"), "strong-verified")


class AlbumOfGradingTest(VerifyCase):
    """Gate MF-2 (verifier side): album_of links grade STRONG
    against the album response's own artist id, so §8's
    fetched-link rung can actually fire."""

    LINK: ClassVar[dict] = {"provider": "deezer",
                            "from_id": "3135556",
                            "to_kind": "artist", "to_id": "399",
                            "relation": "album_of"}

    def test_album_of_grades_strong_from_album_responses(self):
        self.seed([
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
            claim(ALBUM, "album", "provider_link", dict(self.LINK),
                  "deezer"),
        ])
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "artist_id": "mb-art-1",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"},
                "release_id": "rel-1", "tracks": []}},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "artist_id": "399",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": []}})
        verifier.verify_subject(ALBUM)
        self.assertEqual(
            self.tier(ALBUM, "provider_link", self.LINK),
            "strong-verified")


class NamedVariantPerOriginTest(VerifyCase):
    """Gate MF-4: named-variant fingerprints are per asserting
    origin — two agreeing origins both verify, and one origin's
    extra slot poisons only its own fingerprint."""

    def _seed_from(self, identity, slots):
        claims = [
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
            claim(TRACK, "track", "name", ["Radiohead", "Airbag"],
                  "file_tags"),
            claim(TRACK2, "track", "name",
                  ["Radiohead", "Paranoid Android"], "file_tags"),
            *slots,
        ]
        batch = C.make_batch(identity.public_key_b64url, claims,
                             asserted_at=int(NOW) - 60)
        env = C.sign_batch(identity.signing_private_key, batch)
        self.store.put_cached_batch(env, received_at=NOW)

    def _tier_of(self, key, field, value):
        verdicts = self.store.verdicts_for_subjects([ALBUM])
        got = verdicts.get((key, ALBUM, field, value_key(value)))
        return got["tier"] if got else None

    def test_agreeing_origins_verify_extra_slot_poisons_only_its_own(
            self):
        def slot(track, pos):
            return claim(ALBUM, "album", "album_track",
                         {"track": track, "disc": 1, "position": pos,
                          "variant": "mb:rel-1"}, "musicbrainz")

        other = NodeIdentity(key="bb" * 32)
        third = NodeIdentity(key="cc" * 32)
        self._seed_from(self.origin, [slot(TRACK, 1), slot(TRACK2, 2)])
        self._seed_from(other, [slot(TRACK, 1), slot(TRACK2, 2)])
        self._seed_from(third, [slot(TRACK, 1), slot(TRACK2, 2),
                                slot(TRACK, 3)])
        tracks = [{"disc": 1, "position": 1, "title": "Airbag",
                   "duration_ms": 284000, "isrc": None, "id": "r1"},
                  {"disc": 1, "position": 2,
                   "title": "Paranoid Android", "duration_ms": 383000,
                   "isrc": None, "id": "r2"}]
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"},
                "release_id": "rel-1", "tracks": tracks},
                ("album_tracks", "rel-1"): tracks},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": tracks}})
        verifier.verify_subject(ALBUM)
        for key in (self.origin.public_key_b64url,
                    other.public_key_b64url):
            for value in ({"track": TRACK, "disc": 1, "position": 1,
                           "variant": "mb:rel-1"},
                          {"track": TRACK2, "disc": 1, "position": 2,
                           "variant": "mb:rel-1"}):
                self.assertEqual(
                    self._tier_of(key, "album_track", value),
                    "strong-verified")
        # The third origin's fingerprint carries its extra slot:
        # none of its slots grade.
        self.assertIsNone(self._tier_of(
            third.public_key_b64url, "album_track",
            {"track": TRACK, "disc": 1, "position": 1,
             "variant": "mb:rel-1"}))


class NamedVariantOnlyNamedReleaseTest(VerifyCase):
    """Gate MF-5: a named variant grades against its named
    release's tracklist ALONE — matching only the release-group
    pick and the Deezer edition (the union) is not enough."""

    def test_union_match_without_named_match_grades_nothing(self):
        slots = [
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK, "disc": 1, "position": 1,
                   "variant": "mb:rel-9"}, "musicbrainz"),
            claim(ALBUM, "album", "album_track",
                  {"track": TRACK2, "disc": 1, "position": 2,
                   "variant": "mb:rel-9"}, "musicbrainz"),
        ]
        self.seed([
            claim(ALBUM, "album", "name",
                  ["Radiohead", "OK Computer"], "file_tags"),
            claim(ALBUM, "album", "release_group_mbid", "mb-rg-1",
                  "musicbrainz"),
            claim(ALBUM, "album", "deezer_id", "3135556", "deezer"),
            claim(TRACK, "track", "name", ["Radiohead", "Airbag"],
                  "file_tags"),
            claim(TRACK2, "track", "name",
                  ["Radiohead", "Paranoid Android"], "file_tags"),
            *slots,
        ])
        picked = [{"disc": 1, "position": 1, "title": "Airbag",
                   "duration_ms": 284000, "isrc": None, "id": "p1"},
                  {"disc": 1, "position": 2,
                   "title": "Paranoid Android",
                   "duration_ms": 383000, "isrc": None, "id": "p2"}]
        named = [{"disc": 1, "position": 1, "title": "Fitter Happier",
                  "duration_ms": 117000, "isrc": None, "id": "n1"},
                 {"disc": 1, "position": 2, "title": "Electioneering",
                  "duration_ms": 383000, "isrc": None, "id": "n2"}]
        verifier = self.verifier(
            mb_table={("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"},
                "release_id": "rel-1", "tracks": picked},
                ("album_tracks", "rel-9"): named},
            dz_table={("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": "123", "year": 1997, "links": {},
                "release_id": None, "tracks": picked}})
        verifier.verify_subject(ALBUM)
        for slot in slots:
            self.assertIsNone(
                self.tier(ALBUM, "album_track", slot["value"]))

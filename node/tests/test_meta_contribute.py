"""Contributor tests: export rows -> claims -> signed batches.

Fakes stand in for SoulSync and the pool; the identity and the claim
library are the real ones, so every batch a test "sends" is verified
with claims.verify_batch before the fake pool accepts it.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node import contribute as X
from metasync_node.identity import load_or_create_identity
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.soulsync import SoulSyncError

# Real kinded_v2 ids of the tuples below — the claim library
# recomputes the SoulID from every name claim, so fakes must agree.
ARTIST_ID = S.generate_soul_id("artist", "Radiohead")
ALBUM_ID = S.generate_soul_id("album", "Radiohead", "OK Computer")
TRACK_ID = S.generate_soul_id("track", "Radiohead", "Airbag")

ARTIST = {
    "soul_id": ARTIST_ID, "name": "Radiohead",
    "musicbrainz_id": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
    "musicbrainz_match_status": "matched",
    "spotify_artist_id": "4Z8W4fKeB5YxbusRsdQVPb",
    "spotify_match_status": "matched",
    "deezer_id": "399", "deezer_match_status": "pending",
    "genres": ["alternative rock", "art rock"],
}
ALBUM = {
    "soul_id": ALBUM_ID, "title": "OK Computer",
    "artist_name": "Radiohead", "year": 1997, "label": "XL",
    "canonical_source": "spotify", "canonical_album_id": "alb-1",
    "musicbrainz_release_id": "mb-rel-1",
    "musicbrainz_match_status": "matched",
    "spotify_album_id": "19RKaTlM7omb6LsyY9C7KE",
    "spotify_match_status": "identifier",
    "genres": ["alternative rock"],
}
TRACK = {
    "soul_id": TRACK_ID, "title": "Airbag",
    "artist_name": "Radiohead", "album_soul_id": ALBUM["soul_id"],
    "track_number": 1, "disc_number": 1, "duration": 284460,
    "isrc": "GBAYE9700182",
    "musicbrainz_recording_id": "mb-rec-1",
    "musicbrainz_match_status": "matched",
    "spotify_track_id": "2qyS3OaSW2x7yIrfFDv9xz",
    "spotify_match_status": "not_found",
}


class ClaimMappingTest(unittest.TestCase):
    def test_artist_claims(self):
        claims = X.artist_claims(ARTIST)
        by_field = {c["field"]: c for c in claims}
        self.assertEqual(by_field["name"]["value"], ["Radiohead"])
        self.assertEqual(by_field["name"]["source"], "file_tags")
        self.assertEqual(by_field["artist_mbid"]["source"], "musicbrainz")
        self.assertEqual(by_field["spotify_id"]["value"],
                         "4Z8W4fKeB5YxbusRsdQVPb")
        self.assertNotIn("deezer_id", by_field)  # status pending: skipped
        genres = [c for c in claims if c["field"] == "genres"]
        self.assertEqual(len(genres), 2)
        for claim in claims:
            C.validate_claim(claim)

    def test_album_claims_use_canonical_source(self):
        claims = X.album_claims(ALBUM)
        by_field = {c["field"]: c for c in claims}
        self.assertEqual(by_field["name"]["value"],
                         ["Radiohead", "OK Computer"])
        self.assertEqual(by_field["name"]["source"], "spotify")
        self.assertEqual(by_field["year"]["value"], 1997)
        self.assertEqual(by_field["year"]["source"], "spotify")
        # No MBID claim: v1's album MBID field is the release-GROUP id,
        # which the export does not serve (only the release id).
        self.assertNotIn("release_group_mbid", by_field)
        self.assertEqual(by_field["label"]["value"], "XL")
        for claim in claims:
            C.validate_claim(claim)

    def test_track_claims_and_album_track(self):
        variants = {ALBUM["soul_id"]: X.album_variant(ALBUM)}
        sources = {ALBUM["soul_id"]: X.scalar_source(ALBUM)}
        claims = X.track_claims(TRACK, variants, sources)
        by_field = {c["field"]: c for c in claims}
        # The export's duration is already milliseconds (SoulSync's
        # media-server wrappers normalize to ms) — passed through.
        self.assertEqual(by_field["duration_ms"]["value"], 284460)
        self.assertEqual(by_field["duration_ms"]["source"], "file_tags")
        self.assertEqual(by_field["isrc"]["value"], "GBAYE9700182")
        self.assertNotIn("spotify_id", by_field)  # not_found: skipped
        slot = by_field["album_track"]
        self.assertEqual(slot["subject"], ALBUM["soul_id"])
        self.assertEqual(slot["kind"], "album")
        self.assertEqual(slot["value"], {
            "track": TRACK["soul_id"], "disc": 1, "position": 1,
            "variant": "mb:mb-rel-1",
        })
        self.assertEqual(slot["source"], "spotify")
        for claim in claims:
            C.validate_claim(claim)

    def test_variant_fallbacks(self):
        self.assertEqual(X.album_variant(ALBUM), "mb:mb-rel-1")
        no_mb = {**ALBUM, "musicbrainz_release_id": None}
        self.assertEqual(X.album_variant(no_mb), "canonical:spotify:alb-1")
        bare = {**ALBUM, "musicbrainz_release_id": None,
                "canonical_album_id": None}
        self.assertEqual(X.album_variant(bare), "local")
        self.assertEqual(X.album_variant(None), "local")

    def test_album_track_needs_real_numbers_and_variant(self):
        variants = {ALBUM["soul_id"]: X.album_variant(ALBUM)}
        sources = {ALBUM["soul_id"]: X.scalar_source(ALBUM)}
        no_number = {**TRACK, "track_number": None}
        fields = {c["field"] for c in X.track_claims(no_number, variants,
                                                     sources)}
        self.assertNotIn("album_track", fields)
        # No release identity -> variant "local" -> no slot claim.
        bare_album = {**ALBUM, "musicbrainz_release_id": None,
                      "canonical_album_id": None}
        local_variants = {ALBUM["soul_id"]: X.album_variant(bare_album)}
        self.assertEqual(local_variants[ALBUM["soul_id"]], "local")
        fields = {c["field"] for c in X.track_claims(TRACK, local_variants,
                                                     sources)}
        self.assertNotIn("album_track", fields)

    def test_variant_ignores_unresolved_mb_match(self):
        pending = {**ALBUM, "musicbrainz_match_status": "pending"}
        self.assertEqual(X.album_variant(pending), "canonical:spotify:alb-1")

    def test_unnamed_and_incomplete_rows_emit_nothing(self):
        self.assertEqual(X.artist_claims(
            {**ARTIST, "soul_id": "soul_unnamed_42"}), [])
        self.assertEqual(X.album_claims({**ALBUM, "artist_name": ""}), [])
        self.assertEqual(X.track_claims(
            {**TRACK, "title": " "}, {}, {}), [])


class FakeSoulSync:
    def __init__(self, pages):
        self.pages = pages  # {(entity, cursor): page dict}
        self.calls = []

    def export_page(self, entity, cursor="", since="", limit=1000):
        self.calls.append((entity, cursor, since))
        key = (entity, cursor)
        if key not in self.pages:
            raise SoulSyncError(f"no page {key}", code="HTTP_404", status=404)
        return self.pages[key]


class FakePool:
    def __init__(self, identity, responder=None):
        self.identity = identity
        self.responder = responder or (lambda env, n: (201, {
            "accepted": env["batch"]["header"]["claim_count"],
            "duplicates": 0}))
        self.sent = []

    def post_meta_claims(self, envelope, meta_base):
        # The fake verifies like the real ingest does.
        C.verify_batch(self.identity.public_key_b64url, envelope)
        self.sent.append(envelope)
        return self.responder(envelope, len(self.sent))


def page(items, has_more=False, next_cursor="", version="kinded_v2"):
    return {"soulid_version": version, "items": items,
            "has_more": has_more, "next_cursor": next_cursor}


class RunTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.identity = load_or_create_identity(self.tmp.name)
        self.state = {}
        self.saves = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self):
        self.saves += 1

    def _contributor(self, pages, responder=None, sharing_on=None):
        pool = FakePool(self.identity, responder)
        return X.Contributor(FakeSoulSync(pages), pool, self.identity,
                             "https://meta.example", self.state, self._save,
                             sharing_on=sharing_on), pool

    def test_full_walk_sends_batches_and_completes(self):
        pages = {
            ("artist", ""): page([ARTIST], has_more=True, next_cursor="a2"),
            ("artist", "a2"): page([]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        }
        contributor, pool = self._contributor(pages)
        out = contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(out["status"], "completed")
        self.assertGreater(out["accepted"], 0)
        # Next walk's since = this walk's start minus an hour of skew.
        self.assertEqual(self.state["walk"]["since"],
                         "2026-10-25T23:00:00+00:00")
        # The per-album maps persist across walks: an incremental
        # walk re-exports a changed track but not its album, and the
        # slot claim still needs the album's real variant.
        self.assertEqual(self.state["album_variants"],
                         {ALBUM["soul_id"]: "mb:mb-rel-1"})
        # Track batch carried the album_track slot with the mb variant.
        all_claims = [c for env in pool.sent for c in env["batch"]["claims"]]
        self.assertTrue(any(c["field"] == "album_track" for c in all_claims))

    def test_not_approved_stops_the_run(self):
        pages = {("artist", ""): page([ARTIST]),
                 ("album", ""): page([]), ("track", ""): page([])}
        contributor, _ = self._contributor(
            pages, responder=lambda env, n: (403, {
                "error": "contributor_not_approved"}))
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "not_approved")

    def test_allowance_stop_keeps_walk_position(self):
        pages = {
            ("artist", ""): page([ARTIST], has_more=True, next_cursor="a2"),
            ("artist", "a2"): page([]),
            ("album", ""): page([]), ("track", ""): page([]),
        }
        contributor, _ = self._contributor(
            pages, responder=lambda env, n: (429, {}))
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "allowance_spent")
        # The first page was never accepted, so the saved position
        # still points at it: the next run re-walks it instead of
        # losing its claims for good.
        walk = self.state.get("walk") or {}
        self.assertEqual(walk.get("entity", "artist"), "artist")
        self.assertEqual(walk.get("cursor", ""), "")

    def test_unapproved_account_cannot_walk_its_backfill_away(self):
        pages = {
            ("artist", ""): page([ARTIST], has_more=True, next_cursor="a2"),
            ("artist", "a2"): page([ARTIST]),
            ("album", ""): page([]), ("track", ""): page([]),
        }
        contributor, pool = self._contributor(
            pages, responder=lambda env, n: (403, {
                "error": "contributor_not_approved"}))
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "not_approved")
        # Only the first page was attempted; position unmoved.
        self.assertEqual(len(pool.sent), 1)
        self.assertEqual((self.state.get("walk") or {}).get("cursor", ""), "")

    def test_same_track_on_two_albums_dedupes_name_per_batch(self):
        # The same song (same SoulID) on the album and a single:
        # both rows build a name claim for one subject. The batch
        # rule allows one — the flush keeps the first, drops the
        # extra, and make_batch accepts the chunk.
        other_album_track = {**TRACK, "album_soul_id": "soul_" + "d" * 16}
        pages = {
            ("artist", ""): page([]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK, other_album_track]),
        }
        contributor, pool = self._contributor(pages)
        out = contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(out["status"], "completed")
        name_claims = [c for env in pool.sent
                       for c in env["batch"]["claims"]
                       if c["field"] == "name"
                       and c["subject"] == TRACK["soul_id"]]
        self.assertEqual(len(name_claims), 1)

    def test_invalid_row_is_skipped_not_fatal(self):
        bad = {**ARTIST, "name": "x" * 600,
               "soul_id": S.generate_soul_id("artist", "x" * 600)}
        pages = {
            ("artist", ""): page([bad, ARTIST]),
            ("album", ""): page([]), ("track", ""): page([]),
        }
        contributor, _ = self._contributor(pages)
        out = contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["items_skipped"], 1)
        self.assertGreater(out["accepted"], 0)

    def test_stuck_cursor_stops_instead_of_looping(self):
        pages = {("artist", ""): page([ARTIST], has_more=True,
                                      next_cursor="")}
        contributor, _ = self._contributor(pages)
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "export_cursor_stuck")

    def test_version_gate(self):
        pages = {("artist", ""): page([ARTIST], version=None)}
        contributor, _ = self._contributor(pages)
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "soulid_not_v2")

    def test_export_404_is_unavailable_not_empty(self):
        contributor, _ = self._contributor({})
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "export_unavailable")

    def test_sharing_turned_off_mid_run_stops_before_next_send(self):
        # The switch is re-read before every send: flipping it off
        # after page 1 lands stops the run before page 2 goes out,
        # and the walk stays pointed at the unsent page.
        flag = {"on": True}

        def responder(env, n):
            flag["on"] = False
            return (201, {"accepted": env["batch"]["header"]["claim_count"],
                          "duplicates": 0})

        pages = {
            ("artist", ""): page([ARTIST], has_more=True, next_cursor="a2"),
            ("artist", "a2"): page([ARTIST]),
            ("album", ""): page([]), ("track", ""): page([]),
        }
        contributor, pool = self._contributor(
            pages, responder=responder,
            sharing_on=lambda: flag["on"])
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "sharing_off")
        self.assertEqual(len(pool.sent), 1)
        self.assertEqual((self.state.get("walk") or {}).get("cursor"), "a2")

    def test_skipped_album_lends_no_variant_to_its_tracks(self):
        # An album whose name claim does not recompute to its SoulID
        # (a stale ID after a rename) is skipped whole. Its tracks
        # must not then send album_track claims under that rejected
        # album ID, and the variant map must not record it.
        stale_id = S.generate_soul_id("album", "Radiohead", "Pablo Honey")
        stale_album = {**ALBUM, "soul_id": stale_id}
        track = {**TRACK, "album_soul_id": stale_id}
        pages = {
            ("artist", ""): page([]),
            ("album", ""): page([stale_album]),
            ("track", ""): page([track]),
        }
        contributor, pool = self._contributor(pages)
        out = contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(out["status"], "completed")
        self.assertGreaterEqual(out["items_skipped"], 1)
        sent_subjects = [c["subject"] for env in pool.sent
                         for c in env["batch"]["claims"]]
        self.assertNotIn(stale_id, sent_subjects)
        self.assertNotIn(stale_id, self.state.get("album_variants") or {})

    def test_generic_403_is_http_403_not_not_approved(self):
        pages = {("artist", ""): page([ARTIST]),
                 ("album", ""): page([]), ("track", ""): page([])}
        contributor, _ = self._contributor(
            pages, responder=lambda env, n: (403, {"error": "forbidden"}))
        with self.assertRaises(X.ContributionStopped) as ctx:
            contributor.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(ctx.exception.reason, "http_403")

    def test_preview_builds_without_sending(self):
        pages = {
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        }
        contributor, pool = self._contributor(pages)
        out = contributor.preview()
        self.assertEqual(pool.sent, [])
        self.assertGreater(out["counts"]["name"], 0)
        self.assertTrue(out["sample"])


class _StubResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class MetaClaims401RetryTest(unittest.TestCase):
    """pool.post_meta_claims: a 401 gets exactly one fresh exchange
    and one retry with the new token — never a loop, never a
    double-post of an accepted batch (a 401 stored nothing)."""

    def _client(self, statuses):
        from metasync_node.pool import PoolClient

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        identity = load_or_create_identity(tmp.name)
        auth_state = {"access_token": "old-token",
                      "token_expires_epoch": time.time() + 3600}
        calls = []

        class Session:
            def request(self, method, url, data=None, headers=None,
                        timeout=None):
                calls.append((headers or {}).get("Authorization"))
                status = statuses[len(calls) - 1]
                body = {"accepted": 3, "duplicates": 0} \
                    if status < 300 else {"error": "invalid_token"}
                return _StubResponse(status, body)

        client = PoolClient(
            {"id": "p", "name": "P", "url": "https://pool.example",
             "api_key": "k"},
            identity=identity, auth_state=auth_state, session=Session())

        def fake_exchange(force=False):
            auth_state["access_token"] = "new-token"
            auth_state["token_expires_epoch"] = time.time() + 3600
            return {}

        client.exchange = fake_exchange
        return client, calls

    def test_401_reexchanges_and_retries_once(self):
        client, calls = self._client([401, 201])
        status, _ = client.post_meta_claims({"batch": {}},
                                            "https://meta.example")
        self.assertEqual(status, 201)
        self.assertEqual(calls, ["Bearer old-token", "Bearer new-token"])

    def test_second_401_gives_up(self):
        client, calls = self._client([401, 401])
        status, _ = client.post_meta_claims({"batch": {}},
                                            "https://meta.example")
        self.assertEqual(status, 401)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

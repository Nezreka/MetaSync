"""Builder tests: export rows -> claims -> the local claims store.

The builder is PR #12's contributor with the push transport deleted:
same claim construction, same walk discipline, but the store on this
machine is the destination. Fixtures mirror PR #12's (real kinded_v2
ids — the claim library recomputes the SoulID from every name claim,
so fakes must agree).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.identity import load_or_create_identity
from metasync_node.meta import builder as B
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta.store import MetaStore
from metasync_node.soulsync import SoulSyncError

ARTIST_ID = S.generate_soul_id("artist", "Radiohead")
ALBUM_ID = S.generate_soul_id("album", "Radiohead", "OK Computer")
ALBUM2_ID = S.generate_soul_id("album", "Radiohead", "Pablo Honey")
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
    "deezer_id": "3135556", "deezer_match_status": "matched",
    "genres": ["alternative rock"],
}
ALBUM2 = {
    "soul_id": ALBUM2_ID, "title": "Pablo Honey",
    "artist_name": "Radiohead", "year": 1993,
    "canonical_source": "deezer", "canonical_album_id": "alb-2",
    "musicbrainz_release_id": "mb-rel-2",
    "musicbrainz_match_status": "matched",
    "genres": [],
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

T0 = "2026-10-26T00:00:00+00:00"


def page(items, has_more=False, next_cursor="", version="kinded_v2"):
    return {"soulid_version": version, "items": items,
            "has_more": has_more, "next_cursor": next_cursor}


def full_pages(track_items=(TRACK,), album_items=(ALBUM, ALBUM2)):
    return {
        ("artist", ""): page([ARTIST]),
        ("album", ""): page(list(album_items)),
        ("track", ""): page(list(track_items)),
    }


class FakeSoulSync:
    def __init__(self, pages):
        self.pages = pages  # {(entity, cursor): page dict}
        self.calls = []

    def export_page(self, entity, cursor="", since="", limit=1000):
        self.calls.append((entity, cursor, since))
        key = (entity, cursor)
        if key not in self.pages:
            raise SoulSyncError(f"no page {key}", code="HTTP_404",
                                status=404)
        return self.pages[key]


class BuilderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = MetaStore(self.tmp.name)

    def build(self, pages, now=T0, **kwargs):
        builder = B.LibraryBuilder(FakeSoulSync(pages), self.identity,
                                   self.store, **kwargs)
        return builder, builder.run(now)

    def all_fields(self, subject):
        return {c["field"] for c in self.store.own_claims(subject)}


class WalkTest(BuilderCase):
    def test_full_walk_writes_the_store(self):
        _builder, out = self.build(full_pages())
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["rows"], 4)
        counts = self.store.counts()
        self.assertEqual(counts["own_subjects"], 4)
        # The stored per-subject batches verify against the node
        # key and together carry exactly the subject's claims.
        envelopes = self.store.own_batches(ALBUM_ID)
        self.assertTrue(envelopes)
        claims = []
        for envelope in envelopes:
            batch = C.verify_batch(self.identity.public_key_b64url,
                                   envelope)
            self.assertEqual({c["subject"] for c in batch["claims"]},
                             {ALBUM_ID})
            claims += batch["claims"]
        self.assertIn("album_track", {c["field"] for c in claims})

    def test_provider_id_gating(self):
        self.build(full_pages())
        # deezer artist id is "pending": never enters the index —
        # no id claim, and no album_of link either (the link needs
        # BOTH sides resolved).
        self.assertEqual(self.store.resolve_provider_ref("deezer:399"), [])
        # Resolved statuses (matched / identifier) do. The artist
        # resolves by its own id claim (first); the album follows
        # via its spotify album_of link's to-end — the index maps
        # both ends of provider_link claims by design.
        self.assertEqual(
            self.store.resolve_provider_ref(
                "spotify:4Z8W4fKeB5YxbusRsdQVPb"), [ARTIST_ID, ALBUM_ID])
        self.assertEqual(
            self.store.resolve_provider_ref("deezer:3135556"), [ALBUM_ID])
        # The track's spotify status is not_found: skipped.
        self.assertEqual(self.store.resolve_provider_ref(
            "spotify:2qyS3OaSW2x7yIrfFDv9xz"), [])

    def test_no_album_mbid_from_a_release_id(self):
        self.build(full_pages())
        for subject in (ARTIST_ID, ALBUM_ID, ALBUM2_ID, TRACK_ID):
            self.assertNotIn("release_group_mbid",
                             self.all_fields(subject))
        self.assertIn("artist_mbid", self.all_fields(ARTIST_ID))
        self.assertIn("recording_mbid", self.all_fields(TRACK_ID))

    def test_album_track_variant_labels(self):
        self.build(full_pages())
        slots = [c for c in self.store.own_claims(ALBUM_ID)
                 if c["field"] == "album_track"]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["value"], {
            "track": TRACK_ID, "disc": 1, "position": 1,
            "variant": "mb:mb-rel-1"})
        duration = [c for c in self.store.own_claims(TRACK_ID)
                    if c["field"] == "duration_ms"]
        # Passed through as milliseconds, from the files.
        self.assertEqual(duration[0]["value"], 284460)
        self.assertEqual(duration[0]["source"], "file_tags")

    def test_local_variant_gets_no_slot_claim(self):
        bare = {**ALBUM, "musicbrainz_release_id": None,
                "canonical_album_id": None}
        self.build(full_pages(album_items=(bare,)))
        self.assertNotIn("album_track", self.all_fields(ALBUM_ID))

    def test_incremental_walk_uses_since_and_replaces(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        changed = {**ARTIST, "genres": ["art rock"]}
        fake.pages = {
            ("artist", ""): page([changed]),
            ("album", ""): page([]),
            ("track", ""): page([]),
        }
        out = builder.run("2026-10-27T00:00:00+00:00")
        self.assertEqual(out["status"], "completed")
        # since = the first walk's start minus an hour of skew.
        sinces = {call[2] for call in fake.calls[3:]}
        self.assertEqual(sinces, {"2026-10-25T23:00:00+00:00"})
        genres = {c["value"] for c in self.store.own_claims(ARTIST_ID)
                  if c["field"] == "genres"}
        self.assertEqual(genres, {"art rock"})
        # Untouched entities keep their claims (no sweep on
        # incremental walks).
        self.assertIn("album_track", self.all_fields(ALBUM_ID))
        self.assertEqual(self.store.counts()["own_subjects"], 4)

    def test_track_moving_albums_moves_its_slot_claim(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        moved = {**TRACK, "album_soul_id": ALBUM2_ID}
        fake.pages = {
            ("artist", ""): page([]),
            ("album", ""): page([]),
            ("track", ""): page([moved]),
        }
        builder.run("2026-10-27T00:00:00+00:00")
        self.assertNotIn("album_track", self.all_fields(ALBUM_ID))
        slots = [c for c in self.store.own_claims(ALBUM2_ID)
                 if c["field"] == "album_track"]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["value"]["variant"], "mb:mb-rel-2")

    def test_full_rebuild_sweeps_rows_that_left(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        fake.pages = full_pages(track_items=())
        out = builder.run("2026-10-28T00:00:00+00:00", force_full=True)
        self.assertGreater(out["claims_swept"], 0)
        self.assertEqual(self.store.own_claims(TRACK_ID), [])
        # The album's slot claim was the track row's: swept with it.
        self.assertNotIn("album_track", self.all_fields(ALBUM_ID))
        self.assertEqual(self.store.counts()["own_subjects"], 3)

    def test_invalid_row_is_skipped_not_fatal(self):
        bad = {**ARTIST, "name": "x" * 600,
               "soul_id": S.generate_soul_id("artist", "x" * 600)}
        pages = full_pages()
        pages[("artist", "")] = page([bad, ARTIST])
        _builder, out = self.build(pages)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["items_skipped"], 1)
        self.assertIn("name", self.all_fields(ARTIST_ID))

    def test_version_gate(self):
        pages = {("artist", ""): page([ARTIST], version=None)}
        builder = B.LibraryBuilder(FakeSoulSync(pages), self.identity,
                                   self.store)
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run(T0)
        self.assertEqual(ctx.exception.reason, "soulid_not_v2")

    def test_export_404_is_unavailable_not_empty(self):
        builder = B.LibraryBuilder(FakeSoulSync({}), self.identity,
                                   self.store)
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run(T0)
        self.assertEqual(ctx.exception.reason, "export_unavailable")

    def test_stuck_cursor_stops_instead_of_looping(self):
        pages = {("artist", ""): page([ARTIST], has_more=True,
                                      next_cursor="")}
        builder = B.LibraryBuilder(FakeSoulSync(pages), self.identity,
                                   self.store)
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run(T0)
        self.assertEqual(ctx.exception.reason, "export_cursor_stuck")

    def test_answering_turned_off_stops_the_build(self):
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store,
            enabled_on=lambda: False)
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run(T0)
        self.assertEqual(ctx.exception.reason, "answering_off")
        self.assertEqual(self.store.counts()["own_claims"], 0)

    def test_multi_page_walk_and_repeat_row_in_one_page(self):
        changed = {**ARTIST, "genres": ["art rock"]}
        pages = {
            # The artist twice on one page: the later row wins, in
            # the page's single commit.
            ("artist", ""): page([ARTIST, changed], has_more=True,
                                 next_cursor="c2"),
            ("artist", "c2"): page([]),
            ("album", ""): page([ALBUM, ALBUM2]),
            ("track", ""): page([TRACK]),
        }
        _builder, out = self.build(pages)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["rows"], 5)
        genres = {c["value"] for c in self.store.own_claims(ARTIST_ID)
                  if c["field"] == "genres"}
        self.assertEqual(genres, {"art rock"})
        self.assertEqual(self.store.counts()["own_subjects"], 4)


class LinkClaimsTest(BuilderCase):
    """GAP 1: the builder mints provider_link claims (an album's
    own ids naming each other, and album -> artist per shared
    provider) plus the release-group MBID claim."""

    def links(self, subject=ALBUM_ID):
        return [c for c in self.store.own_claims(subject)
                if c["field"] == "provider_link"]

    def test_same_entity_links_each_direction(self):
        self.build(full_pages())
        same = [c["value"] for c in self.links()
                if c["value"]["relation"] == "same_entity"]
        # The album has two resolved provider ids (spotify,
        # deezer): one claim per direction, provider = from side.
        self.assertEqual(same, [
            {"provider": "deezer", "from_id": "3135556",
             "to_kind": "album", "to_id": "19RKaTlM7omb6LsyY9C7KE",
             "relation": "same_entity"},
            {"provider": "spotify",
             "from_id": "19RKaTlM7omb6LsyY9C7KE", "to_kind": "album",
             "to_id": "3135556", "relation": "same_entity"},
        ])
        # The from side of each direction resolves to the album,
        # and so does the to side (both ends are indexed).
        self.assertEqual(
            self.store.resolve_provider_ref("spotify:3135556"),
            [ALBUM_ID])

    def test_album_of_only_for_shared_resolved_providers(self):
        self.build(full_pages())
        album_of = [c["value"] for c in self.links()
                    if c["value"]["relation"] == "album_of"]
        # Album {spotify, deezer} x artist {musicbrainz, spotify}
        # (the artist's deezer is pending): only spotify is shared.
        self.assertEqual(album_of, [
            {"provider": "spotify",
             "from_id": "19RKaTlM7omb6LsyY9C7KE", "to_kind": "artist",
             "to_id": "4Z8W4fKeB5YxbusRsdQVPb", "relation": "album_of"},
        ])

    def test_links_when_the_artist_deezer_resolves(self):
        artist = {**ARTIST, "deezer_match_status": "matched"}
        pages = {
            ("artist", ""): page([artist]),
            ("album", ""): page([ALBUM, ALBUM2]),
            ("track", ""): page([TRACK]),
        }
        self.build(pages)
        album_of = [c["value"] for c in self.links()
                    if c["value"]["relation"] == "album_of"]
        self.assertIn(
            {"provider": "deezer", "from_id": "3135556",
             "to_kind": "artist", "to_id": "399",
             "relation": "album_of"}, album_of)
        # deezer:399 now resolves to the artist (its id claim) and
        # to the album (the link's to-end).
        self.assertEqual(self.store.resolve_provider_ref("deezer:399"),
                         [ARTIST_ID, ALBUM_ID])

    def test_unresolved_album_ids_mint_no_links(self):
        album = {**ALBUM, "spotify_match_status": "not_found"}
        pages = {
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([album]),
            ("track", ""): page([]),
        }
        self.build(pages)
        # Only deezer remains resolved on the album: no same_entity
        # (needs two), and no album_of (the artist's deezer is
        # pending, so deezer is not shared-resolved either).
        self.assertEqual(self.links(), [])

    def test_release_group_claim_from_the_release_group_field(self):
        album = {**ALBUM, "musicbrainz_release_group_id": "mb-rg-1"}
        pages = {
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([album]),
            ("track", ""): page([]),
        }
        self.build(pages)
        rg = [c for c in self.store.own_claims(ALBUM_ID)
              if c["field"] == "release_group_mbid"]
        self.assertEqual(len(rg), 1)
        self.assertEqual(rg[0]["value"], "mb-rg-1")
        self.assertEqual(rg[0]["source"], "musicbrainz")
        self.assertEqual(
            self.store.resolve_provider_ref("musicbrainz:mb-rg-1"),
            [ALBUM_ID])
        # The release-group id joins the same_entity ring like any
        # other resolved provider id.
        same = [c["value"] for c in self.links()
                if c["value"]["relation"] == "same_entity"]
        self.assertIn(
            {"provider": "musicbrainz", "from_id": "mb-rg-1",
             "to_kind": "album", "to_id": "3135556",
             "relation": "same_entity"}, same)

    def test_release_group_gated_by_the_match_status(self):
        album = {**ALBUM, "musicbrainz_release_group_id": "mb-rg-1",
                 "musicbrainz_match_status": "pending"}
        pages = {
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([album]),
            ("track", ""): page([]),
        }
        self.build(pages)
        self.assertNotIn("release_group_mbid", self.all_fields(ALBUM_ID))
        self.assertEqual(
            self.store.resolve_provider_ref("musicbrainz:mb-rg-1"), [])

    def test_artist_ids_survive_into_an_incremental_album_rebuild(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        # The artist does not change; only the album is re-exported.
        # The persisted artist map still backs its album_of link.
        fake.pages = {
            ("artist", ""): page([]),
            ("album", ""): page([{**ALBUM, "year": 2017}]),
            ("track", ""): page([]),
        }
        builder.run("2026-10-27T00:00:00+00:00")
        album_of = [c["value"] for c in self.links()
                    if c["value"]["relation"] == "album_of"]
        self.assertEqual(len(album_of), 1)
        self.assertEqual(album_of[0]["provider"], "spotify")


class SweepGuardTest(BuilderCase):
    def test_empty_full_walk_refuses_to_sweep(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        before = self.store.counts()
        fake.pages = {("artist", ""): page([]),
                      ("album", ""): page([]),
                      ("track", ""): page([])}
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run("2026-10-28T00:00:00+00:00", force_full=True)
        self.assertEqual(ctx.exception.reason, "empty_walk")
        # Nothing was swept, and the walk stays pending (the
        # incremental cursor did not advance past the refusal).
        self.assertEqual(self.store.counts(), before)
        self.assertEqual(builder._state()["walk"], {})

    def test_oversized_sweep_is_refused(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        before = self.store.counts()
        # Only the artist remains in the export: the sweep would
        # drop the albums' and track's claims — well over half.
        fake.pages = {("artist", ""): page([ARTIST]),
                      ("album", ""): page([]),
                      ("track", ""): page([])}
        with self.assertRaises(B.BuildStopped) as ctx:
            builder.run("2026-10-28T00:00:00+00:00", force_full=True)
        self.assertEqual(ctx.exception.reason, "sweep_refused")
        self.assertEqual(self.store.counts(), before)

    def test_invalid_row_keeps_prior_claims_after_a_full_walk(self):
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        # The SAME artist row returns with one claim the contract
        # refuses (a NUL inside a genre tag): its new claims are
        # skipped whole, but the row was SEEN — the sweep must not
        # eat the claims it already had.
        bad = {**ARTIST, "genres": ["art rock", "bad\x00genre"]}
        fake.pages = {
            ("artist", ""): page([bad]),
            ("album", ""): page([ALBUM, ALBUM2]),
            ("track", ""): page([TRACK]),
        }
        out = builder.run("2026-10-28T00:00:00+00:00", force_full=True)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["items_skipped"], 1)
        self.assertIn("name", self.all_fields(ARTIST_ID))
        self.assertEqual(self.store.counts()["own_subjects"], 4)

    def test_abandoned_walk_owners_are_cleared_on_force_full(self):
        pages_seen = {"n": 0}

        def gate():
            pages_seen["n"] += 1
            return pages_seen["n"] <= 1

        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store,
            enabled_on=gate)
        with self.assertRaises(B.BuildStopped):
            builder.run(T0)
        # The first page landed and noted its owner under T0.
        self.assertEqual(self.store.walk_owner_count(T0), 1)
        builder.enabled_on = None  # the forced rerun has no live gate
        builder.run("2026-10-27T00:00:00+00:00", force_full=True)
        # The forced restart cleared the abandoned walk's rows.
        self.assertEqual(self.store.walk_owner_count(T0), 0)


class FullWalkCadenceTest(BuilderCase):
    def test_full_walk_due_uses_its_own_cadence(self):
        from datetime import UTC, datetime, timedelta
        t0 = datetime(2026, 10, 26, tzinfo=UTC)
        fake = FakeSoulSync(full_pages())
        builder = B.LibraryBuilder(fake, self.identity, self.store)
        builder.run(T0)
        self.assertFalse(builder.full_walk_due(t0 + timedelta(days=6)))
        self.assertTrue(builder.full_walk_due(t0 + timedelta(days=7)))
        # An incremental completion does NOT reset the cadence.
        builder.run("2026-11-01T00:00:00+00:00")
        self.assertTrue(builder.full_walk_due(t0 + timedelta(days=7)))


if __name__ == "__main__":
    unittest.main()

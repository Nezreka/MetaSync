"""Responder tests: queries -> signed answers from the local store.

Covers exact-by-subject, exact-by-provider-id (the detail-page
jump-start shape, including provider_link resolution), text match,
the §9 mode matrix with the §7.1 re-serve filter, the service wiring
for the meta_answering opt-in, and one full round trip: two real
relay clients against the real relay server, the answering node
serving a by-provider-id query end to end.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from metasync_node.config import Config
from metasync_node.identity import NodeIdentity, load_or_create_identity
from metasync_node.meta import builder as B
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta.responder import Responder
from metasync_node.meta.store import MetaStore
from metasync_node.relay import RelayClient
from metasync_node.service import NodeService
from metasync_node.store import Store

from tests.test_meta_builder import (
    ALBUM,
    ALBUM_ID,
    ARTIST,
    TRACK,
    TRACK_ID,
    FakeSoulSync,
    full_pages,
    page,
)
from tests.test_relay_client import (
    FULL_SCOPES,
    KEY_ID,
    RelayServerThread,
    issue_token,
    wait_until,
)


def build_store(tmp, identity) -> MetaStore:
    store = MetaStore(tmp)
    builder = B.LibraryBuilder(FakeSoulSync(full_pages()), identity, store)
    builder.run("2026-10-26T00:00:00+00:00")
    return store


def claim_set(envelopes):
    return sorted(
        (c["subject"], c["field"], C.canonical_bytes(c["value"]).decode())
        for env in envelopes for c in env["batch"]["claims"])


class ResponderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = build_store(self.tmp.name, self.identity)
        self.mode = {"value": "own_library"}
        self.responder = Responder(
            self.store, self.identity,
            mode_provider=lambda: self.mode["value"])

    def handle(self, query):
        return self.responder.handle(query)


def all_claims(envelopes):
    return [c for env in envelopes for c in env["batch"]["claims"]]


class ExactTest(ResponderCase):
    def test_exact_by_subject(self):
        out = self.handle({"kind": "album", "mode": "exact",
                           "subjects": [ALBUM_ID]})
        # Two batches: the album's re-servable partition and its
        # own-only one (GAP 2's split), together exactly its claims.
        self.assertEqual(len(out), 2)
        for envelope in out:
            C.verify_batch(self.identity.public_key_b64url, envelope)
        claims = all_claims(out)
        self.assertEqual({c["subject"] for c in claims}, {ALBUM_ID})
        self.assertIn("name", {c["field"] for c in claims})

    def test_exact_by_provider_id_in_subjects(self):
        out = self.handle({"kind": "album", "mode": "exact",
                           "subjects": ["deezer:3135556"]})
        self.assertEqual(len(out), 2)
        # The re-servable partition leads the answer, and it carries
        # the ID claims. Self-checking: the very claim that matched
        # rides the answer.
        claims = out[0]["batch"]["claims"]
        self.assertIn({"subject": ALBUM_ID, "kind": "album",
                       "field": "deezer_id", "value": "3135556",
                       "source": "deezer",
                       "soulid_version": "kinded_v2"}, claims)
        self.assertEqual({c["subject"] for c in claims}, {ALBUM_ID})

    def test_exact_by_provider_ids_key(self):
        out = self.handle({"kind": "album", "mode": "exact",
                           "provider_ids": ["spotify:19RKaTlM7omb6LsyY9C7KE"]})
        self.assertEqual(len(out), 2)
        self.assertEqual({c["subject"] for c in all_claims(out)},
                         {ALBUM_ID})

    def test_exact_track_carries_its_slot_claim(self):
        out = self.handle({"kind": "track", "mode": "exact",
                           "subjects": [TRACK_ID]})
        self.assertEqual(len(out), 2)
        fields = {c["field"] for c in all_claims(out)}
        self.assertIn("album_track", fields)
        self.assertIn("duration_ms", fields)

    def test_slot_claim_not_duplicated_when_album_also_asked(self):
        out = self.handle({"kind": "track", "mode": "exact",
                           "subjects": [TRACK_ID, ALBUM_ID]})
        slots = [c for env in out for c in env["batch"]["claims"]
                 if c["field"] == "album_track"]
        self.assertEqual(len(slots), 1)

    def test_provider_link_resolution(self):
        # A provider_link claim ties the album to deezer artist 399 —
        # an entity this node holds no direct id claim for. Both ends
        # of the link resolve to the album.
        link = {"subject": ALBUM_ID, "kind": "album",
                "field": "provider_link",
                "value": {"provider": "deezer", "from_id": "3135556",
                          "to_kind": "artist", "to_id": "399",
                          "relation": "album_of"},
                "source": "deezer", "soulid_version": "kinded_v2"}
        self.store.replace_owner_claims(
            "seed:link", [link], self.identity.public_key_b64url,
            self.identity.signing_private_key)
        for ref in ("deezer:399", "deezer:3135556"):
            out = self.handle({"kind": "album", "mode": "exact",
                               "subjects": [ref]})
            self.assertTrue(out, ref)
            self.assertEqual(
                {c["subject"] for c in all_claims(out)}, {ALBUM_ID})

    def test_unknown_subject_answers_nothing(self):
        out = self.handle({"kind": "album", "mode": "exact",
                           "subjects": ["soul_0000000000000000"]})
        self.assertEqual(out, [])

    def test_provider_refs_match_forgivingly(self):
        # Whitespace stripped, provider name case-insensitive; the
        # ID itself stays exact (provider IDs are case-sensitive).
        for query in (
                {"subjects": ["  deezer:3135556 "]},
                {"subjects": ["Deezer:3135556"]},
                {"subjects": ["DEEZER : 3135556"]},
                {"provider_ids": [" deezer: 3135556 "]}):
            out = self.handle({"kind": "album", "mode": "exact",
                               **query})
            self.assertTrue(out, query)
            self.assertEqual(
                {c["subject"] for c in all_claims(out)}, {ALBUM_ID})

    def test_exact_inputs_are_capped_in_design_order(self):
        from metasync_node.meta.responder import MAX_QUERY_SUBJECTS
        subjects = [f"soul_{n:016x}" for n in range(250)]
        resolved = self.responder._resolve_exact(
            {"subjects": subjects})
        self.assertEqual(len(resolved), MAX_QUERY_SUBJECTS)
        self.assertEqual(resolved, subjects[:MAX_QUERY_SUBJECTS])
        # Provider refs get only the budget plain subjects leave.
        self.store.resolve_provider_ref = \
            lambda ref: [f"resolved:{ref}"]
        resolved = self.responder._resolve_exact({
            "subjects": subjects[:150],
            "provider_ids": [f"deezer:{n}" for n in range(150)]})
        self.assertEqual(len(resolved), MAX_QUERY_SUBJECTS)
        self.assertEqual(resolved[:150], subjects[:150])
        self.assertEqual(resolved[150], "resolved:deezer:0")
        self.assertEqual(resolved[-1], "resolved:deezer:49")


class TextTest(ResponderCase):
    def test_typo_tolerant_match(self):
        out = self.handle({"kind": "artist", "mode": "text",
                           "terms": ["radiohed"]})
        self.assertEqual(len(out), 1)
        names = [c["value"] for c in out[0]["batch"]["claims"]
                 if c["field"] == "name"]
        self.assertEqual(names, [["Radiohead"]])

    def test_multi_word_terms_match_fused_name(self):
        out = self.handle({"kind": "album", "mode": "text",
                           "terms": ["ok", "computer"]})
        self.assertEqual(len(out), 2)
        self.assertEqual({c["subject"] for c in all_claims(out)},
                         {ALBUM_ID})

    def test_kind_filter(self):
        out = self.handle({"kind": "artist", "mode": "text",
                           "terms": ["ok", "computer"]})
        self.assertEqual(out, [])
        out = self.handle({"kind": "track", "mode": "text",
                           "terms": ["radiohead"]})
        # The track's batches. Their only foreign-subject claim
        # is the album_track slot (subject = the album) that a track
        # answer deliberately carries.
        self.assertEqual(len(out), 2)
        claims = all_claims(out)
        self.assertEqual(
            {c["subject"] for c in claims if c["field"] != "album_track"},
            {TRACK_ID})
        self.assertEqual(
            {c["subject"] for c in claims if c["field"] == "album_track"},
            {ALBUM_ID})

    def test_text_terms_are_capped(self):
        from metasync_node.meta.responder import MAX_QUERY_TERMS
        queried = []
        original = self.store.subjects_with_token

        def counting(token, kind):
            queried.append(token)
            return original(token, kind)

        self.store.subjects_with_token = counting
        self.handle({"kind": "artist", "mode": "text",
                     "terms": [f"zzterm{n:02d}" for n in range(40)]})
        self.assertLessEqual(len(set(queried)), MAX_QUERY_TERMS)

    def test_text_subjects_capped(self):
        for n in range(30):
            name = f"Test Artist {n:02d}"
            sid = S.generate_soul_id("artist", name)
            claim = {"subject": sid, "kind": "artist", "field": "name",
                     "value": [name], "source": "file_tags",
                     "soulid_version": "kinded_v2"}
            self.store.replace_owner_claims(
                f"artist:{sid}", [claim],
                self.identity.public_key_b64url,
                self.identity.signing_private_key)
        out = self.handle({"kind": "artist", "mode": "text",
                           "terms": ["artist"]})
        self.assertEqual(len(out), 25)


class ModeTest(ResponderCase):
    def _seed_cache(self, origin_identity, claims, subject):
        batch = C.make_batch(origin_identity.public_key_b64url, claims)
        envelope = C.sign_batch(origin_identity.signing_private_key, batch)
        self.store.put_cached_batch(envelope)
        return envelope

    def _other_artist(self):
        sid = S.generate_soul_id("artist", "Cached Artist")
        claims = [
            {"subject": sid, "kind": "artist", "field": "name",
             "value": ["Cached Artist"], "source": "file_tags",
             "soulid_version": "kinded_v2"},
            {"subject": sid, "kind": "artist", "field": "genres",
             "value": "jazz", "source": "musicbrainz",
             "soulid_version": "kinded_v2"},
        ]
        return sid, claims

    def test_own_library_never_reserves(self):
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        self._seed_cache(origin, claims, sid)
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(out, [])

    def test_full_reserves_with_forwarding_envelope(self):
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        envelope = self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(len(out), 1)
        wrapped = out[0]
        # The origin envelope rides untouched and still verifies
        # against the ORIGIN key.
        self.assertEqual(wrapped["envelope"], envelope)
        C.verify_batch(origin.public_key_b64url, wrapped["envelope"])
        # The forwarding attestation is this node's, over exactly the
        # fields it names.
        fwd = wrapped["forward"]
        self.assertEqual(fwd["origin_node"],
                         origin.public_key_b64url)
        self.assertEqual(fwd["origin_batch_id"],
                         C.batch_digest(envelope["batch"]))
        signed = {k: fwd[k] for k in
                  ("origin_batch_id", "origin_node", "forwarded_at")}
        pub = Ed25519PublicKey.from_public_bytes(
            self.identity.public_key_bytes)
        pub.verify(C.b64url_decode(fwd["sig"]),
                   C.canonical_bytes(signed))

    def test_full_reserves_bare_provider_id_links(self):
        # GAP 2's exception: a bare provider-id claim is a link,
        # not catalogue data — §7.1 names bare ID links as what
        # travels, whatever source stamped them. A cached batch
        # whose only provider-sourced claim is the deezer_id IS
        # re-served.
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        claims.append({"subject": sid, "kind": "artist",
                       "field": "deezer_id", "value": "4242",
                       "source": "deezer",
                       "soulid_version": "kinded_v2"})
        envelope = self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["envelope"], envelope)

    def test_full_refuses_provider_sourced_values(self):
        # What still blocks a batch is a provider-sourced VALUE:
        # a genre Deezer asserted is catalogue data, not a link.
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        claims[1] = {**claims[1], "source": "deezer"}
        self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(out, [])

    def test_full_refuses_filetags_duration_and_isrc_batch(self):
        # §7.1 is field-level, not only source-level: a batch whose
        # claims are ALL file_tags-sourced still stays home when it
        # carries a duration or ISRC — those fields are answered
        # from a node's own library but never re-served, because
        # they usually originated in provider data upstream.
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid = S.generate_soul_id("track", "Cached Artist", "Cached Song")
        claims = [
            {"subject": sid, "kind": "track", "field": "name",
             "value": ["Cached Artist", "Cached Song"],
             "source": "file_tags", "soulid_version": "kinded_v2"},
            {"subject": sid, "kind": "track", "field": "duration_ms",
             "value": 201000, "source": "file_tags",
             "soulid_version": "kinded_v2"},
            {"subject": sid, "kind": "track", "field": "isrc",
             "value": "USRC10000001", "source": "file_tags",
             "soulid_version": "kinded_v2"},
        ]
        self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "track", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(out, [])

    def test_full_reserves_musicbrainz_duration_and_isrc(self):
        # The same fields ARE re-servable when MusicBrainz-sourced.
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid = S.generate_soul_id("track", "Cached Artist", "Cached Song")
        claims = [
            {"subject": sid, "kind": "track", "field": "name",
             "value": ["Cached Artist", "Cached Song"],
             "source": "musicbrainz", "soulid_version": "kinded_v2"},
            {"subject": sid, "kind": "track", "field": "duration_ms",
             "value": 201000, "source": "musicbrainz",
             "soulid_version": "kinded_v2"},
            {"subject": sid, "kind": "track", "field": "isrc",
             "value": "USRC10000001", "source": "musicbrainz",
             "soulid_version": "kinded_v2"},
        ]
        self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "track", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(len(out), 1)

    def test_full_refuses_filetags_display_name_batch(self):
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        claims.append({"subject": sid, "kind": "artist",
                       "field": "display_name",
                       "value": ["Cached Artist"], "source": "file_tags",
                       "soulid_version": "kinded_v2"})
        self._seed_cache(origin, claims, sid)
        self.mode["value"] = "full"
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(out, [])

    def test_reserving_tolerates_an_empty_answer_cache_table(self):
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid, claims = self._other_artist()
        self._seed_cache(origin, claims, sid)
        # Step 5 owns provenance writes; a cached batch with no
        # answer_cache row must still read (and re-serve) fine.
        self.store._db.execute("DELETE FROM answer_cache")
        self.store._db.commit()
        self.mode["value"] = "full"
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": [sid]})
        self.assertEqual(len(out), 1)

    def test_own_provider_sourced_claims_are_served(self):
        # The §7.1 caution binds RE-SERVING, not one's own library:
        # the album's deezer-sourced id claim is answered as-is.
        out = self.handle({"kind": "album", "mode": "exact",
                           "subjects": [ALBUM_ID]})
        sources = {c["source"] for c in out[0]["batch"]["claims"]}
        self.assertIn("deezer", sources)

    def test_leech_answers_nothing(self):
        self.mode["value"] = "leech"
        self.assertEqual(self.handle(
            {"kind": "album", "mode": "exact", "subjects": [ALBUM_ID]}),
            [])
        self.assertEqual(self.handle(
            {"kind": "artist", "mode": "text", "terms": ["radiohead"]}),
            [])

    def test_batch_cap(self):
        subjects = []
        for n in range(60):
            name = f"Cap Artist {n:02d}"
            sid = S.generate_soul_id("artist", name)
            subjects.append(sid)
            claim = {"subject": sid, "kind": "artist", "field": "name",
                     "value": [name], "source": "file_tags",
                     "soulid_version": "kinded_v2"}
            self.store.replace_owner_claims(
                f"artist:{sid}", [claim],
                self.identity.public_key_b64url,
                self.identity.signing_private_key)
        out = self.handle({"kind": "artist", "mode": "exact",
                           "subjects": subjects})
        self.assertEqual(len(out), 50)


SPLIT_TRACK = S.generate_soul_id("track", "Split Artist", "Split Song")
SPLIT_TRIO = [
    {"subject": SPLIT_TRACK, "kind": "track", "field": "deezer_id",
     "value": "9988", "source": "deezer", "soulid_version": "kinded_v2"},
    {"subject": SPLIT_TRACK, "kind": "track", "field": "duration_ms",
     "value": 187000, "source": "file_tags",
     "soulid_version": "kinded_v2"},
    {"subject": SPLIT_TRACK, "kind": "track", "field": "year",
     "value": 2001, "source": "musicbrainz",
     "soulid_version": "kinded_v2"},
]


def fields_of(envelope):
    return {c["field"] for c in envelope["batch"]["claims"]}


class TwoBatchEmissionTest(unittest.TestCase):
    """GAP 2: a subject's claims answer as two batches split by
    the §7.1 rule — never one mixed batch that a single held-back
    claim would make unforwardable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = MetaStore(self.tmp.name)
        self.responder = Responder(
            self.store, self.identity,
            mode_provider=lambda: "own_library")

    def seed(self, claims):
        self.store.replace_owner_claims(
            "seed:split", claims, self.identity.public_key_b64url,
            self.identity.signing_private_key)

    def answer(self):
        return self.responder.handle(
            {"kind": "track", "mode": "exact",
             "subjects": [SPLIT_TRACK]})

    def test_mixed_subject_emits_two_batches_with_exact_partitions(self):
        self.seed(SPLIT_TRIO)
        out = self.answer()
        self.assertEqual(len(out), 2)
        # Re-servable first: exactly the bare provider-id claim
        # (exempt despite its deezer source) and the MusicBrainz
        # year. The file_tags duration stays own-only.
        self.assertEqual(fields_of(out[0]), {"deezer_id", "year"})
        self.assertEqual(fields_of(out[1]), {"duration_ms"})
        for envelope in out:
            C.verify_batch(self.identity.public_key_b64url, envelope)

    def test_all_reservable_subject_emits_one_batch(self):
        self.seed([
            {"subject": SPLIT_TRACK, "kind": "track", "field": "name",
             "value": ["Split Artist", "Split Song"],
             "source": "file_tags", "soulid_version": "kinded_v2"},
            {"subject": SPLIT_TRACK, "kind": "track",
             "field": "genres", "value": "rock",
             "source": "musicbrainz", "soulid_version": "kinded_v2"},
            SPLIT_TRIO[0],
        ])
        out = self.answer()
        self.assertEqual(len(out), 1)
        self.assertEqual(fields_of(out[0]),
                         {"name", "genres", "deezer_id"})

    def test_all_held_subject_emits_one_own_only_batch(self):
        self.seed([SPLIT_TRIO[1],
                   {"subject": SPLIT_TRACK, "kind": "track",
                    "field": "isrc", "value": "USRC10000002",
                    "source": "file_tags",
                    "soulid_version": "kinded_v2"}])
        out = self.answer()
        self.assertEqual(len(out), 1)
        self.assertEqual(fields_of(out[0]), {"duration_ms", "isrc"})

    def test_stored_materialization_is_the_same_two_batches(self):
        self.seed(SPLIT_TRIO)
        stored = self.store.own_batches(SPLIT_TRACK)
        self.assertEqual(len(stored), 2)
        self.assertEqual(fields_of(stored[0]), {"deezer_id", "year"})
        self.assertEqual(fields_of(stored[1]), {"duration_ms"})
        digests = {C.batch_digest(env["batch"]) for env in stored}
        self.assertEqual(len(digests), 2)  # no id collision


class ReserveRoundTripTest(unittest.TestCase):
    """GAP 2 end to end: node A answers a mixed subject; node B
    caches both batches and, in full mode, re-serves the
    re-servable one and refuses the own-only one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = load_or_create_identity(self.tmp.name + "-a")
        store_a = MetaStore(self.tmp.name + "-a")
        store_a.replace_owner_claims(
            "seed:split", SPLIT_TRIO, self.origin.public_key_b64url,
            self.origin.signing_private_key)
        responder_a = Responder(store_a, self.origin,
                                mode_provider=lambda: "own_library")
        self.answer = responder_a.handle(
            {"kind": "track", "mode": "exact",
             "subjects": [SPLIT_TRACK]})
        self.assertEqual(len(self.answer), 2)
        self.peer = load_or_create_identity(self.tmp.name + "-b")
        self.store_b = MetaStore(self.tmp.name + "-b")
        for envelope in self.answer:
            self.store_b.put_cached_batch(envelope)

    def test_cache_filter_separates_the_partitions(self):
        reservable = self.store_b.cached_batches_for_subjects(
            [SPLIT_TRACK], reservable_only=True)
        self.assertEqual(
            [C.batch_digest(e["batch"]) for e in reservable],
            [C.batch_digest(self.answer[0]["batch"])])
        everything = self.store_b.cached_batches_for_subjects(
            [SPLIT_TRACK], reservable_only=False)
        self.assertEqual(
            {C.batch_digest(e["batch"]) for e in everything},
            {C.batch_digest(e["batch"]) for e in self.answer})

    def test_full_mode_reserves_only_the_reservable_batch(self):
        responder_b = Responder(self.store_b, self.peer,
                                mode_provider=lambda: "full")
        out = responder_b.handle(
            {"kind": "track", "mode": "exact",
             "subjects": [SPLIT_TRACK]})
        self.assertEqual(len(out), 1)
        self.assertIn("forward", out[0])
        self.assertEqual(out[0]["envelope"], self.answer[0])
        self.assertEqual(fields_of(out[0]["envelope"]),
                         {"deezer_id", "year"})


class ReservableRuleTest(unittest.TestCase):
    """The SQL cache filter and the Python predicate are one rule:
    for every claim shape, they agree on whether it may travel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = load_or_create_identity(self.tmp.name + "-o")

    def test_sql_filter_matches_the_python_predicate(self):
        from metasync_node.meta.store import claim_reservable
        track = S.generate_soul_id("track", "Case Artist", "Case Song")
        album = S.generate_soul_id("album", "Case Artist", "Case Album")
        other = S.generate_soul_id("track", "Case Artist", "Other")

        def value_for(field):
            if field in ("name", "display_name"):
                return ["Case Artist", "Case Song"]
            if field == "album_track":
                return {"track": other, "disc": 1, "position": 1,
                        "variant": "mb:rel-1"}
            if field == "provider_link":
                return {"provider": "deezer", "from_id": "1",
                        "to_kind": "artist", "to_id": "2",
                        "relation": "album_of"}
            if field == "duration_ms":
                return 1000
            if field == "year":
                return 1999
            if field == "isrc":
                return "USRC10000001"
            return "x-1"

        # (kind, subject, field, source, may it travel?)
        cases = [
            ("track", track, "deezer_id", "deezer", True),
            ("track", track, "spotify_id", "spotify", True),
            ("track", track, "recording_mbid", "deezer", True),
            ("track", track, "provider_link", "spotify", True),
            ("track", track, "duration_ms", "file_tags", False),
            ("track", track, "duration_ms", "musicbrainz", True),
            ("track", track, "isrc", "file_tags", False),
            ("track", track, "isrc", "musicbrainz", True),
            ("track", track, "display_name", "file_tags", False),
            ("track", track, "name", "spotify", False),
            ("track", track, "name", "file_tags", True),
            ("track", track, "genres", "deezer", False),
            ("track", track, "genres", "file_tags", True),
            ("track", track, "year", "spotify", False),
            ("track", track, "year", "file_tags", True),
            ("track", track, "label", "musicbrainz", True),
            ("album", album, "album_track", "file_tags", True),
            ("album", album, "album_track", "spotify", False),
        ]
        for n, (kind, subject, field, source, expected) in \
                enumerate(cases):
            claim = {"subject": subject, "kind": kind, "field": field,
                     "value": value_for(field), "source": source,
                     "soulid_version": "kinded_v2"}
            self.assertEqual(claim_reservable(claim), expected,
                             (field, source))
            store = MetaStore(f"{self.tmp.name}/case{n}")
            batch = C.make_batch(self.origin.public_key_b64url, [claim])
            store.put_cached_batch(
                C.sign_batch(self.origin.signing_private_key, batch))
            served = store.cached_batches_for_subjects(
                [subject], reservable_only=True)
            self.assertEqual(len(served) == 1, expected, (field, source))
            store.close()


class StoreInternalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = build_store(self.tmp.name, self.identity)

    def test_busy_timeout_is_set(self):
        row = self.store._db.execute("PRAGMA busy_timeout").fetchone()
        self.assertEqual(row[0], 5000)

    def test_slot_lookup_uses_the_expression_index(self):
        plan = self.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT subject FROM claims "
            "WHERE origin = 'own' AND field = 'album_track' "
            "AND json_extract(value_json, '$.track') IN (?)",
            (TRACK_ID,)).fetchall()
        text = " ".join(str(row) for row in plan)
        self.assertIn("claims_album_track_track", text)
        # And the lookup itself behaves as before.
        slots = self.store.album_track_claims_for_tracks([TRACK_ID])
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["subject"], ALBUM_ID)

    def test_tokens_with_prefix_pages_through_the_whole_bucket(self):
        # Over 20,000 tokens share the first character — the old
        # single-LIMIT scan silently truncated at 20,000. The
        # two-character bucket scan returns every one of them. (A
        # fresh store: the built one already holds real tokens.)
        store = MetaStore(self.tmp.name + "-fresh")
        tokens = [f"a{chr(98 + (i % 24))}{i:05d}" for i in range(20350)]
        tokens += [f"ab{i:04d}zz" for i in range(300)]
        store._db.executemany(
            "INSERT INTO name_index(token, subject, kind, claim_id) "
            "VALUES (?, 'soul_0000000000000001', 'artist', ?)",
            [(token, n) for n, token in enumerate(tokens)])
        store._db.commit()
        self.assertEqual(store.tokens_with_prefix("a", None),
                         sorted(set(tokens)))
        ab_bucket = sorted({t for t in tokens if t.startswith("ab")})
        self.assertEqual(store.tokens_with_prefix("ab", None),
                         ab_bucket)

    def test_put_cached_batch_refuses_malformed_envelopes(self):
        for bad in ([],
                    {},
                    {"batch": {}},
                    {"batch": {"header": {}}},
                    {"batch": {"header": {"node": 5}}, "sig": "zz"},
                    {"batch": {"header": {"node": "x"}}}):
            with self.assertRaises(C.ClaimError, msg=repr(bad)):
                self.store.put_cached_batch(bad)

    def _cached_asserted_at(self, batch_id):
        row = self.store._db.execute(
            "SELECT asserted_at FROM batches WHERE batch_id = ?",
            (batch_id,)).fetchone()
        return None if row is None else row[0]

    def test_cached_batch_ages_from_the_origins_asserted_at(self):
        # MF-3 / §6: a re-served old batch keeps its ORIGINAL age
        # basis in the receiving cache — receipt time must not
        # refresh it, or retracted claims would never age out.
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid = S.generate_soul_id("artist", "Old Batch Artist")
        claims = [
            {"subject": sid, "kind": "artist", "field": "name",
             "value": ["Old Batch Artist"], "source": "file_tags",
             "soulid_version": "kinded_v2"},
        ]
        origin_time = 1_600_000_000  # the origin asserted this long ago
        batch = C.make_batch(origin.public_key_b64url, claims,
                             asserted_at=origin_time)
        envelope = C.sign_batch(origin.signing_private_key, batch)
        batch_id = self.store.put_cached_batch(
            envelope, received_at=1_700_000_000.0)
        self.assertEqual(self._cached_asserted_at(batch_id),
                         float(origin_time))

    def test_cached_batch_future_asserted_at_is_capped_at_receipt(self):
        # §6: a future-dated batch cannot extend its own life — the
        # cache records min(asserted_at, receipt).
        origin = load_or_create_identity(self.tmp.name + "-origin2")
        sid = S.generate_soul_id("artist", "Future Batch Artist")
        claims = [
            {"subject": sid, "kind": "artist", "field": "name",
             "value": ["Future Batch Artist"], "source": "file_tags",
             "soulid_version": "kinded_v2"},
        ]
        batch = C.make_batch(origin.public_key_b64url, claims,
                             asserted_at=1_900_000_000)  # far future
        envelope = C.sign_batch(origin.signing_private_key, batch)
        batch_id = self.store.put_cached_batch(
            envelope, received_at=1_700_000_000.0)
        self.assertEqual(self._cached_asserted_at(batch_id),
                         1_700_000_000.0)

    def test_full_mode_skips_corrupt_cache_rows(self):
        origin = load_or_create_identity(self.tmp.name + "-origin")
        sid = S.generate_soul_id("artist", "Cached Artist")
        claims = [
            {"subject": sid, "kind": "artist", "field": "name",
             "value": ["Cached Artist"], "source": "file_tags",
             "soulid_version": "kinded_v2"},
        ]
        batch = C.make_batch(origin.public_key_b64url, claims)
        self.store.put_cached_batch(
            C.sign_batch(origin.signing_private_key, batch))
        # Hand-corrupted rows claiming the same subject: one whose
        # envelope does not parse, one that parses to a non-shape.
        for batch_id, envelope in (
                ("corrupt-a", "{}"),
                ("corrupt-b", '{"batch": {"header": {"node": 42}}}')):
            self.store._db.execute(
                "INSERT INTO batches(batch_id, origin, origin_node,"
                " subject, asserted_at, envelope) "
                "VALUES (?, 'cache', 'x', NULL, 0, ?)",
                (batch_id, envelope))
            self.store._db.execute(
                "INSERT INTO claims(origin, owner, batch_id, subject,"
                " kind, field, value_json, source) VALUES ('cache',"
                " NULL, ?, ?, 'artist', 'name', '[\"Cached Artist\"]',"
                " 'file_tags')", (batch_id, sid))
        self.store._db.commit()
        responder = Responder(self.store, self.identity,
                              mode_provider=lambda: "full")
        out = responder.handle({"kind": "artist", "mode": "exact",
                                "subjects": [sid]})
        # Only the genuine batch is re-served; the corrupt rows are
        # skipped, not a KeyError.
        self.assertEqual(len(out), 1)
        self.assertIn("forward", out[0])


class ServiceWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.identity = NodeIdentity(key="ab" * 32)
        self.service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, env={})
        self.addCleanup(self.service.stop_relay_client)

    def test_answering_defaults_off_and_registers_the_handler(self):
        self.assertFalse(self.service.meta_answering())
        self.store.settings["meta_relay_url"] = "ws://127.0.0.1:9"
        self.store.settings["meta_network"] = True
        self.service.sync_relay_client()
        client = self.service._relay_client
        self.assertIsNotNone(client)
        # Network on, answering off: no handler — the node asks and
        # stays silent, however many queries arrive.
        self.assertIsNone(client._query_handler)
        self.store.settings["meta_answering"] = True
        self.service.sync_relay_client()
        self.assertIsNotNone(client._query_handler)
        self.store.settings["meta_answering"] = False
        self.service.sync_relay_client()
        self.assertIsNone(client._query_handler)

    def test_meta_store_constructs_once_under_concurrency(self):
        import threading
        barrier = threading.Barrier(16)
        stores, responders = [], []

        def grab():
            barrier.wait()
            stores.append(self.service.meta_store())
            responders.append(self.service._meta_responder_obj())

        threads = [threading.Thread(target=grab) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({id(s) for s in stores}), 1)
        self.assertEqual(len({id(r) for r in responders}), 1)

    def test_status_and_sync_never_create_the_store_when_off(self):
        from pathlib import Path as _Path
        meta_db = _Path(self.tmp.name) / "meta_claims.sqlite3"
        self.assertFalse(meta_db.exists())
        status = self.service.meta_status()
        self.assertEqual(status["store"]["own_claims"], 0)
        for _ in range(3):  # scheduler ticks, everything off
            self.service.sync_meta_store()
        self.assertFalse(meta_db.exists())

    def test_full_walk_cadence_drives_service_rebuilds(self):
        from datetime import UTC, datetime, timedelta
        fake = FakeSoulSync(full_pages())
        service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, soulsync=fake,
            env={})
        self.store.settings["meta_answering"] = True
        t0 = datetime(2026, 10, 26, tzinfo=UTC)
        out = service.rebuild_meta_store(t0)
        self.assertEqual(out["status"], "completed")
        # Seven days on, an incremental completion is minutes old —
        # the plain 20 h rule cannot be what fires — yet a rebuild
        # is due, because the full-walk cadence is.
        due_now = t0 + timedelta(days=7)
        self.assertTrue(service.meta_rebuild_due(due_now))
        out = service.rebuild_meta_store(due_now)
        self.assertEqual(out["status"], "completed")
        build = service.meta_status()["build"]
        self.assertEqual(build["last_full_completed_at"],
                         due_now.isoformat())
        self.assertFalse(service.meta_rebuild_due(due_now))

    def test_rebuild_and_status(self):
        fake = FakeSoulSync({
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        })
        service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, soulsync=fake,
            env={})
        self.store.settings["meta_answering"] = True
        now = service.clock()
        self.assertTrue(service.meta_rebuild_due(now))
        out = service.rebuild_meta_store(now)
        self.assertEqual(out["status"], "completed")
        status = service.meta_status()
        self.assertEqual(status["store"]["own_subjects"], 3)
        self.assertEqual(status["build"]["last_status"], "completed")
        self.assertFalse(service.meta_rebuild_due(now))

    def test_preview_answers_matches_the_responder(self):
        fake = FakeSoulSync({
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        })
        service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, soulsync=fake,
            env={})
        service.rebuild_meta_store(service.clock())
        query = {"kind": "album", "mode": "exact",
                 "subjects": ["deezer:3135556"]}
        preview = service.preview_answers(query)
        direct = service._meta_responder_obj().handle(query)
        self.assertEqual(claim_set(preview["batches"]),
                         claim_set(direct))
        self.assertEqual(len(preview["batches"]), 2)


class PreviewRouteTest(unittest.TestCase):
    """POST /local/meta/preview: the local-key machine surface over
    preview_answers (app.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.identity = NodeIdentity(key="ef" * 32)
        fake = FakeSoulSync({
            ("artist", ""): page([ARTIST]),
            ("album", ""): page([ALBUM]),
            ("track", ""): page([TRACK]),
        })
        self.service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, soulsync=fake,
            env={})
        self.service.rebuild_meta_store(self.service.clock())
        from metasync_node.app import create_app
        self.client = create_app(self.service).test_client()
        self.key = self.service.issue_local_api_key()

    def test_preview_route_requires_the_local_key(self):
        res = self.client.post("/local/meta/preview", json={
            "query": {"kind": "album", "mode": "exact",
                      "subjects": [ALBUM_ID]}})
        self.assertEqual(res.status_code, 401)

    def test_preview_route_answers_like_the_responder(self):
        res = self.client.post(
            "/local/meta/preview",
            json={"query": {"kind": "album", "mode": "exact",
                            "subjects": ["deezer:3135556"]}},
            headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(len(body["batches"]), 2)
        self.assertEqual(
            {c["subject"] for c in body["batches"][0]["batch"]["claims"]},
            {ALBUM_ID})


class RelayRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        cls.pool_private = Ed25519PrivateKey.generate()
        cls.pool_public = cls.pool_private.public_key()

    def test_by_provider_id_query_end_to_end(self):
        server = RelayServerThread(self.pool_public)
        server.start()
        self.addCleanup(server.stop)
        clients = []
        self.addCleanup(lambda: [c.stop() for c in clients])

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        responder_identity = load_or_create_identity(tmp.name)
        store = build_store(tmp.name, responder_identity)
        responder = Responder(store, responder_identity,
                              mode_provider=lambda: "own_library")

        def connect(identity):
            # Protocol v2: no binding step — the relay verifies the
            # token offline; binding lives at the token exchange.
            token = issue_token(self.pool_private, KEY_ID,
                                identity.node_id, FULL_SCOPES)
            client = RelayClient(
                identity,
                url_provider=lambda: server.url,
                mode_provider=lambda: "own_library",
                token_provider=lambda: token)
            clients.append(client)
            client.start()
            wait_until(lambda: client.connected)
            return client

        answering = connect(responder_identity)
        answering.set_query_handler(responder.handle)
        asker_identity = NodeIdentity(key="cd" * 32)
        asker = connect(asker_identity)

        results = list(asker.query(
            "album", "exact", subjects=["spotify:19RKaTlM7omb6LsyY9C7KE"],
            window_s=0.5, grace_s=1.0))
        # The album arrives as its two partitions (re-servable +
        # own-only); together they are the album's claims.
        self.assertEqual(len(results), 2)
        claims = []
        for envelope in results:
            batch = C.verify_batch(responder_identity.public_key_b64url,
                                   envelope)
            claims += batch["claims"]
        subjects = {c["subject"] for c in claims}
        self.assertEqual(subjects, {ALBUM_ID})
        # Self-checking: the claim that resolved the provider id is
        # in the answer, so the asker verifies the mapping locally.
        self.assertIn("spotify_id", {c["field"] for c in claims})


if __name__ == "__main__":
    unittest.main()

"""Querier tests (§5/§6/§9, step 6): cache-first asking, streaming
admission, merged views, revocation mid-flight, cache aging — and
round trips through the REAL relay server, where a second
identical query must be answered from cache with zero new relay
queries (asserted on the relay's own counters).
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from metasync_node.identity import NodeIdentity, b64url_encode
from metasync_node.meta import builder as B
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta import verify as V
from metasync_node.meta.querier import Querier
from metasync_node.meta.responder import Responder
from metasync_node.meta.store import MetaStore, node_id_for_key
from metasync_node.relay import RelayClient
from metasync_node.revocations import RevocationStore, list_signing_text

from tests.test_meta_admission import (
    ISSUER,
    ISSUER_PRIVATE,
    ISSUER_PUBLIC,
    envelope,
    issue,
    track_claims,
)
from tests.test_meta_builder import (
    ALBUM,
    ALBUM_ID,
    FakeSoulSync,
    full_pages,
    page,
)
from tests.test_meta_responder import build_store
from tests.test_meta_verify import FakeFetcher
from tests.test_relay_client import (
    FULL_SCOPES,
    KEY_ID,
    RelayServerThread,
    issue_token,
    wait_until,
)

DAY = 24 * 3600
NOW = int(time.time())
# ISSUER / ISSUER_PRIVATE / ISSUER_PUBLIC come from the admission
# tests: issue() signs with that key, so the querier's trusted
# issuer pair must be the same one (MF-1 made the door actually
# verify the signature — two generated keypairs no longer pass
# for each other).
SUBJECT = S.generate_soul_id("track", "Radiohead", "Airbag")


def standing_attestation(identity):
    return issue(identity.public_key_b64url,
                 bound_at=NOW - 40 * DAY, issued_at=NOW - DAY,
                 expires=NOW + 6 * DAY)


class FakeRelayClient:
    """Stands in for RelayClient: serves canned envelopes, counts
    the queries it was asked."""

    def __init__(self, envelopes=()):
        self.envelopes = list(envelopes)
        self.queries = []

    def query(self, kind, mode, subjects=None, terms=None, **kwargs):
        self.queries.append(
            {"kind": kind, "mode": mode, "subjects": subjects,
             "terms": terms})
        return iter(list(self.envelopes))


class QuerierCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = NodeIdentity(key="ee" * 32)
        self.store = MetaStore(self.tmp.name)
        self.origin = NodeIdentity(key="aa" * 32)

    def querier(self, relay=None, **kwargs):
        kwargs.setdefault("clock", lambda: float(NOW))
        # MF-1: the asker is configured with its pool's issuer key
        # + id, the trusted pair standing verifies against.
        kwargs.setdefault("issuer_key_provider", lambda: ISSUER_PUBLIC)
        kwargs.setdefault("own_issuer_provider", lambda: ISSUER)
        return Querier(self.store, self.identity,
                       relay_provider=lambda: relay, **kwargs)


class CacheFirstTest(QuerierCase):
    def test_own_library_answers_without_network(self):
        store = build_store(self.tmp.name, self.identity)
        relay = FakeRelayClient()
        querier = Querier(store, self.identity,
                          relay_provider=lambda: relay,
                          clock=lambda: float(NOW))
        out = querier.query_exact("album", [ALBUM_ID])
        self.assertFalse(out["asked_network"])
        self.assertEqual(relay.queries, [])
        record = out["records"][0]
        self.assertTrue(record["found"])
        self.assertEqual(record["name"], ["Radiohead", "OK Computer"])
        deezer = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(deezer["value"], "3135556")
        self.assertTrue(deezer["local"])

    def test_provider_ref_resolves_locally(self):
        store = build_store(self.tmp.name, self.identity)
        querier = Querier(store, self.identity,
                          relay_provider=lambda: FakeRelayClient(),
                          clock=lambda: float(NOW))
        out = querier.query_exact("album", ["deezer:3135556"])
        self.assertFalse(out["asked_network"])
        self.assertEqual(out["records"][0]["subject"], ALBUM_ID)

    def test_network_answer_admits_then_cache_serves(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        relay = FakeRelayClient([env])
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(out["asked_network"])
        self.assertEqual(out["admitted"], 1)
        record = out["records"][0]
        self.assertTrue(record["found"])
        e = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["value"], "12345")
        self.assertEqual(e["support"], 1)  # standing origin counts
        self.assertEqual(e["support_label"], "1-2 nodes")
        # Second identical query: pure cache, zero new relay queries.
        out2 = querier.query_exact("track", [SUBJECT])
        self.assertFalse(out2["asked_network"])
        self.assertEqual(len(relay.queries), 1)
        self.assertTrue(out2["records"][0]["found"])

    def test_zero_support_origin_still_displays(self):
        env = envelope(self.origin, claims=track_claims())  # no attestation
        querier = self.querier(FakeRelayClient([env]))
        out = querier.query_exact("track", [SUBJECT])
        e = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["support"], 0)
        self.assertEqual(e["support_label"], "unverified")

    def test_text_query_local_then_network(self):
        store = build_store(self.tmp.name, self.identity)
        querier = Querier(store, self.identity,
                          relay_provider=lambda: FakeRelayClient(),
                          clock=lambda: float(NOW))
        out = querier.query_text("album", ["OK Computer"])
        self.assertFalse(out["asked_network"])
        self.assertEqual(out["records"][0]["subject"], ALBUM_ID)
        # Nothing local matches: the network is asked, and what
        # lands is re-resolved locally. (A fresh store: the one
        # above now knows the whole Radiohead library.)
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        relay = FakeRelayClient([env])
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        querier2 = Querier(MetaStore(fresh.name), self.identity,
                           relay_provider=lambda: relay,
                           clock=lambda: float(NOW))
        out2 = querier2.query_text("track", ["Airbag"])
        self.assertTrue(out2["asked_network"])
        self.assertEqual(out2["records"][0]["subject"], SUBJECT)

    def test_record_is_local_only(self):
        querier = self.querier(FakeRelayClient())
        self.assertIsNone(querier.record(SUBJECT))
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        querier.admit(env)
        record = querier.record(SUBJECT)
        self.assertIsNotNone(record)
        self.assertTrue(record["found"])
        self.assertIsNotNone(querier.record("deezer:12345"))


class FabricatedStandingTest(QuerierCase):
    """The MF-1 exploit at the merged view: a fresh key embeds a
    fabricated attestation (bound a year ago, random-bytes sig).
    The asker IS configured with the real issuer key — under the
    shape-only door this record showed support 1."""

    def test_fabricated_attestation_counts_zero_support(self):
        fresh = NodeIdentity(key="ff" * 32)
        fabricated = {"issuer": ISSUER,
                      "node_key": fresh.public_key_b64url,
                      "bound_at": NOW - 365 * DAY,
                      "issued_at": NOW - DAY, "expires": NOW + 6 * DAY,
                      "sig": b64url_encode(b"\x07" * 64)}
        env = envelope(fresh, claims=track_claims(),
                       attestation=fabricated)
        querier = self.querier(FakeRelayClient([env]))
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(out["records"][0]["found"])
        e = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["support"], 0)
        self.assertEqual(e["support_label"], "unverified")


class RecordingVerifier:
    """Captures the querier's scheduling decisions (subjects,
    force) instead of verifying anything."""

    def __init__(self):
        self.calls = []

    def verify_subjects(self, subjects, force=False):
        self.calls.append((sorted(subjects), force))
        return []

    def note_conflict(self, origin, subject, field):
        return False


class RecheckForceTest(QuerierCase):
    """MF-2 at the scheduling seam: unverdicted claims schedule a
    plain first verification; a strong verdict older than §6's
    reliance window schedules a FORCED re-check."""

    def test_stale_strong_verdict_schedules_forced_recheck(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        fake = RecordingVerifier()
        querier = self.querier(FakeRelayClient([env]), verifier=fake,
                               verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        self.assertIn(([SUBJECT], False), fake.calls)
        self.store.set_verdict(
            self.origin.public_key_b64url, SUBJECT, "deezer_id",
            "12345", "strong-verified", NOW - 100 * DAY, "old")
        fake.calls.clear()
        querier.query_exact("track", [SUBJECT])  # cache-served
        self.assertIn(([SUBJECT], True), fake.calls)

    def test_fresh_strong_verdict_schedules_no_recheck(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        fake = RecordingVerifier()
        querier = self.querier(FakeRelayClient([env]), verifier=fake,
                               verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        for field, value in (("deezer_id", "12345"),):
            self.store.set_verdict(
                self.origin.public_key_b64url, SUBJECT, field,
                value, "strong-verified", NOW - DAY, "fresh")
        fake.calls.clear()
        querier.query_exact("track", [SUBJECT])
        self.assertNotIn(([SUBJECT], True), fake.calls)


ARTIST_ID = S.generate_soul_id("artist", "Radiohead")
ALBUM_SUBJ = S.generate_soul_id("album", "Radiohead", "OK Computer")
DZ_LINK = {"provider": "deezer", "from_id": "3135556",
           "to_kind": "artist", "to_id": "399",
           "relation": "album_of"}


def artist_claims():
    return [
        {"subject": ARTIST_ID, "kind": "artist", "field": "name",
         "value": ["Radiohead"], "source": "file_tags",
         "soulid_version": C.SOULID_VERSION},
        {"subject": ARTIST_ID, "kind": "artist",
         "field": "deezer_id", "value": "399", "source": "deezer",
         "soulid_version": C.SOULID_VERSION},
    ]


def album_claims():
    return [
        {"subject": ALBUM_SUBJ, "kind": "album", "field": "name",
         "value": ["Radiohead", "OK Computer"],
         "source": "file_tags", "soulid_version": C.SOULID_VERSION},
        {"subject": ALBUM_SUBJ, "kind": "album",
         "field": "deezer_id", "value": "3135556",
         "source": "deezer", "soulid_version": C.SOULID_VERSION},
        {"subject": ALBUM_SUBJ, "kind": "album",
         "field": "provider_link", "value": dict(DZ_LINK),
         "source": "deezer", "soulid_version": C.SOULID_VERSION},
    ]


class RelianceGatingTest(QuerierCase):
    """R2-SF-4: verification scheduling is reliance-gated —
    record() and the text path schedule nothing; an exact ask
    schedules its subjects plus the disambiguation closure."""

    def _querier_with(self, fake, envelopes=()):
        return self.querier(FakeRelayClient(list(envelopes)),
                            verifier=fake, verify_inline=True)

    def test_record_schedules_nothing(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        self.querier().admit(env)
        fake = RecordingVerifier()
        querier = self._querier_with(fake)
        record = querier.record("deezer:12345")
        self.assertIsNotNone(record)
        self.assertEqual(fake.calls, [])

    def test_text_bystander_is_not_verified(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        self.querier().admit(env)
        fake = RecordingVerifier()
        querier = self._querier_with(fake)
        out = querier.query_text("track", ["Airbag"])
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(fake.calls, [])

    def test_exact_ask_schedules_its_subjects(self):
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        fake = RecordingVerifier()
        querier = self._querier_with(fake, [env])
        querier.query_exact("track", [SUBJECT])
        self.assertIn(([SUBJECT], False), fake.calls)

    def test_artist_ask_schedules_the_disambiguation_closure(self):
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=album_claims(),
                     attestation=standing_attestation(self.origin)),
        ]
        fake = RecordingVerifier()
        querier = self._querier_with(fake, envs)
        querier.query_exact("artist", [ARTIST_ID])
        self.assertIn(([ARTIST_ID], False), fake.calls)
        self.assertIn(([ALBUM_SUBJ], False), fake.calls)


class SanityVerifier(RecordingVerifier):
    """RecordingVerifier + a conflict log: note_conflict always
    allows the trigger, so what is asserted is WHO the querier
    asks about."""

    def __init__(self):
        super().__init__()
        self.conflicts = []

    def note_conflict(self, origin, subject, field):
        self.conflicts.append((origin, subject, field))
        return True


class SanityScopeTest(QuerierCase):
    """R2-SF-8: §8 triggers need a prior STRONG-verified value
    and a network challenger — disputes among the unverified, and
    challenges to the local value alone, fire nothing."""

    def setUp(self):
        super().setUp()
        self.other = NodeIdentity(key="cc" * 32)

    def _env(self, identity, claims):
        return envelope(identity, claims=claims,
                        attestation=standing_attestation(identity))

    def _track_env(self, identity, deezer_id):
        claims = [
            {"subject": SUBJECT, "kind": "track", "field": "name",
             "value": ["Radiohead", "Airbag"], "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": SUBJECT, "kind": "track",
             "field": "deezer_id", "value": deezer_id,
             "source": "deezer", "soulid_version": C.SOULID_VERSION},
        ]
        return self._env(identity, claims)

    def test_dispute_without_a_strong_value_fires_nothing(self):
        fake = SanityVerifier()
        querier = self.querier(
            FakeRelayClient([self._track_env(self.origin, "12345"),
                             self._track_env(self.other, "99999")]),
            verifier=fake, verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        self.assertEqual(fake.conflicts, [])

    def test_network_challenge_to_a_strong_value_fires(self):
        self.store.set_verdict(
            self.origin.public_key_b64url, SUBJECT, "deezer_id",
            "12345", "strong-verified", NOW - 3600, "verified")
        fake = SanityVerifier()
        querier = self.querier(
            FakeRelayClient([self._track_env(self.origin, "12345"),
                             self._track_env(self.other, "99999")]),
            verifier=fake, verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        # Only the CHALLENGING origin is gated — the strong
        # value's own origin agrees with the verified value.
        self.assertEqual(
            fake.conflicts,
            [(self.other.public_key_b64url, SUBJECT, "deezer_id")])
        self.assertIn(([SUBJECT], True), fake.calls)  # forced

    def test_challenge_to_the_local_value_alone_fires_nothing(self):
        # The local library asserts 111; a strong-verified network
        # value (12345) agrees with a second node; nobody
        # challenges the STRONG value — the dispute is with the
        # local value, which is not the network's business.
        own_claims = [
            {"subject": SUBJECT, "kind": "track",
             "field": "deezer_id", "value": "111",
             "source": "deezer", "soulid_version": C.SOULID_VERSION},
        ]
        self.store.replace_owner_claims(
            "seed:own", own_claims, self.identity.public_key_b64url,
            self.identity.signing_private_key)
        self.store.set_verdict(
            self.origin.public_key_b64url, SUBJECT, "deezer_id",
            "12345", "strong-verified", NOW - 3600, "verified")
        fake = SanityVerifier()
        querier = self.querier(
            FakeRelayClient([self._track_env(self.origin, "12345"),
                             self._track_env(self.other, "12345")]),
            verifier=fake, verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        self.assertEqual(fake.conflicts, [])

    def test_album_track_variant_conflict_fires(self):
        def album_env(identity, variant):
            claims = [
                {"subject": ALBUM_SUBJ, "kind": "album",
                 "field": "name",
                 "value": ["Radiohead", "OK Computer"],
                 "source": "file_tags",
                 "soulid_version": C.SOULID_VERSION},
                {"subject": ALBUM_SUBJ, "kind": "album",
                 "field": "album_track",
                 "value": {"track": SUBJECT, "disc": 1,
                           "position": 1, "variant": variant},
                 "source": "musicbrainz",
                 "soulid_version": C.SOULID_VERSION},
            ]
            return self._env(identity, claims)

        slot_a = {"track": SUBJECT, "disc": 1, "position": 1,
                  "variant": "mb:rel-1"}
        self.store.set_verdict(
            self.origin.public_key_b64url, ALBUM_SUBJ, "album_track",
            slot_a, "strong-verified", NOW - 3600, "verified")
        fake = SanityVerifier()
        querier = self.querier(
            FakeRelayClient([album_env(self.origin, "mb:rel-1"),
                             album_env(self.other, "mb:rel-2")]),
            verifier=fake, verify_inline=True)
        querier.query_exact("album", [ALBUM_SUBJ])
        self.assertEqual(
            fake.conflicts,
            [(self.other.public_key_b64url, ALBUM_SUBJ,
              "album_track")])
        self.assertIn(([ALBUM_SUBJ], True), fake.calls)


class DisambiguationWiringTest(QuerierCase):
    """R2-SF-7: merged artist records carry the §8 settlement."""

    def test_fetched_link_settles_and_timestamps(self):
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=album_claims(),
                     attestation=standing_attestation(self.origin)),
        ]
        # MF-2 (gate r2): the fetched-link rung settles only when
        # the link's from_id is an id the USER'S OWN claims assert
        # for the album — the album is in this node's own library
        # and its own deezer_id claim is seeded alongside its name
        # (the link row itself arrives from the network).
        self.store.replace_owner_claims(
            "seed:album", [album_claims()[0], album_claims()[1]],
            self.identity.public_key_b64url,
            self.identity.signing_private_key)
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        # The album_of link itself was verified against the
        # providers (a §7 fetched link) an hour ago.
        self.store.set_verdict(
            self.origin.public_key_b64url, ALBUM_SUBJ,
            "provider_link", DZ_LINK, "strong-verified",
            NOW - 3600, "fetched")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertEqual(block["settled"], "deezer:399")
        self.assertEqual(block["via"], "fetched_link")
        self.assertTrue(block["promotion"])
        self.assertEqual(block["settled_at"], NOW - 3600)

    def test_unverified_links_need_three_local_albums(self):
        # Three albums in THIS node's own library link to the
        # artist's deezer id; the artist arrives from the network.
        albums = []
        for i in range(3):
            subject = S.generate_soul_id(
                "album", "Radiohead", f"Album {i}")
            albums.append({
                "subject": subject, "kind": "album",
                "field": "provider_link",
                "value": {"provider": "deezer",
                          "from_id": str(1000 + i),
                          "to_kind": "artist", "to_id": "399",
                          "relation": "album_of"},
                "source": "deezer",
                "soulid_version": C.SOULID_VERSION})
        self.store.replace_owner_claims(
            "seed:links", albums, self.identity.public_key_b64url,
            self.identity.signing_private_key)
        env = envelope(self.origin, claims=artist_claims(),
                       attestation=standing_attestation(self.origin))
        querier = self.querier(FakeRelayClient([env]))
        out = querier.query_exact("artist", [ARTIST_ID])
        block = out["records"][0]["disambiguation"]
        self.assertEqual(block["settled"], "deezer:399")
        self.assertEqual(block["via"], "local_link_count")
        self.assertFalse(block["promotion"])  # display only

    def test_strangers_link_on_the_users_album_settles_nothing(
            self):
        # Gate r2 MF-2 — the review's attack, end to end. The
        # user's album is SELF-TITLED, so a stranger's album by
        # the namesake artist shares its subject (subjects are
        # keyed by artist name + title). The stranger asserts
        # THEIR deezer album id and an album_of link on the
        # user's album subject, and the link grades strong — the
        # provider really does assert it, about the stranger's
        # album. The user's own claims assert a different deezer
        # id for the album, so the link's from_id is not one of
        # the user's own ids: no settlement, no promotion.
        album2 = S.generate_soul_id("album", "Radiohead",
                                    "Radiohead")
        own = [
            {"subject": album2, "kind": "album", "field": "name",
             "value": ["Radiohead", "Radiohead"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": album2, "kind": "album",
             "field": "deezer_id", "value": "111",
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION},
        ]
        self.store.replace_owner_claims(
            "seed:album2", own, self.identity.public_key_b64url,
            self.identity.signing_private_key)
        stranger_link = {"provider": "deezer", "from_id": "999",
                         "to_kind": "artist", "to_id": "555",
                         "relation": "album_of"}
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=[
                {"subject": ARTIST_ID, "kind": "artist",
                 "field": "deezer_id", "value": "555",
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
            ], attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=[
                {"subject": album2, "kind": "album",
                 "field": "name",
                 "value": ["Radiohead", "Radiohead"],
                 "source": "file_tags",
                 "soulid_version": C.SOULID_VERSION},
                {"subject": album2, "kind": "album",
                 "field": "deezer_id", "value": "999",
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
                {"subject": album2, "kind": "album",
                 "field": "provider_link",
                 "value": dict(stranger_link),
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
            ], attestation=standing_attestation(self.origin)),
        ]
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        self.store.set_verdict(
            self.origin.public_key_b64url, album2,
            "provider_link", stranger_link, "strong-verified",
            NOW - 3600, "fetched")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertIsNone(block["settled"])
        self.assertFalse(block["promotion"])

    def test_link_on_the_users_own_asserted_id_settles(self):
        # The control for the attack above: the same self-titled
        # album, but the network link's from_id IS the deezer id
        # the user's own claims assert for it — the rung fires,
        # on one album, with promotion.
        album2 = S.generate_soul_id("album", "Radiohead",
                                    "Radiohead")
        own = [
            {"subject": album2, "kind": "album", "field": "name",
             "value": ["Radiohead", "Radiohead"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": album2, "kind": "album",
             "field": "deezer_id", "value": "111",
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION},
        ]
        self.store.replace_owner_claims(
            "seed:album2", own, self.identity.public_key_b64url,
            self.identity.signing_private_key)
        link = {"provider": "deezer", "from_id": "111",
                "to_kind": "artist", "to_id": "399",
                "relation": "album_of"}
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=[
                {"subject": album2, "kind": "album",
                 "field": "name",
                 "value": ["Radiohead", "Radiohead"],
                 "source": "file_tags",
                 "soulid_version": C.SOULID_VERSION},
                {"subject": album2, "kind": "album",
                 "field": "provider_link", "value": dict(link),
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
            ], attestation=standing_attestation(self.origin)),
        ]
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        self.store.set_verdict(
            self.origin.public_key_b64url, album2,
            "provider_link", link, "strong-verified",
            NOW - 3600, "fetched")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertEqual(block["settled"], "deezer:399")
        self.assertEqual(block["via"], "fetched_link")
        self.assertTrue(block["promotion"])

    def test_own_link_contradiction_blocks_the_rung(self):
        # The network link's from_id is the user's own asserted
        # id — but the user's OWN album_of data links that album
        # to the real artist, contradicting the network link's
        # namesake target. The user's own data wins: no settle.
        album2 = S.generate_soul_id("album", "Radiohead",
                                    "Radiohead")
        own = [
            {"subject": album2, "kind": "album", "field": "name",
             "value": ["Radiohead", "Radiohead"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": album2, "kind": "album",
             "field": "deezer_id", "value": "111",
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION},
            {"subject": album2, "kind": "album",
             "field": "provider_link",
             "value": {"provider": "deezer", "from_id": "111",
                       "to_kind": "artist", "to_id": "399",
                       "relation": "album_of"},
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION},
        ]
        self.store.replace_owner_claims(
            "seed:album2", own, self.identity.public_key_b64url,
            self.identity.signing_private_key)
        bad_link = {"provider": "deezer", "from_id": "111",
                    "to_kind": "artist", "to_id": "555",
                    "relation": "album_of"}
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=[
                {"subject": ARTIST_ID, "kind": "artist",
                 "field": "deezer_id", "value": "555",
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
            ], attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=[
                {"subject": album2, "kind": "album",
                 "field": "provider_link",
                 "value": dict(bad_link),
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION},
            ], attestation=standing_attestation(self.origin)),
        ]
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        self.store.set_verdict(
            self.origin.public_key_b64url, album2,
            "provider_link", bad_link, "strong-verified",
            NOW - 3600, "fetched")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertIsNone(block["settled"])
        self.assertFalse(block["promotion"])

    def test_no_link_evidence_no_disambiguation_block(self):
        env = envelope(self.origin, claims=artist_claims(),
                       attestation=standing_attestation(self.origin))
        querier = self.querier(FakeRelayClient([env]))
        out = querier.query_exact("artist", [ARTIST_ID])
        self.assertNotIn("disambiguation", out["records"][0])

    def test_fetched_link_on_a_network_only_album_settles_nothing(
            self):
        # Gate MF-2: the same fetched link as the settling test,
        # but the album is NOT in this node's own library — the
        # rung must not fire on a stranger's album.
        envs = [
            envelope(self.origin, claims=artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=album_claims(),
                     attestation=standing_attestation(self.origin)),
        ]
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        self.store.set_verdict(
            self.origin.public_key_b64url, ALBUM_SUBJ,
            "provider_link", DZ_LINK, "strong-verified",
            NOW - 3600, "fetched")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertIsNone(block["settled"])

    def _mbid_artist_claims(self):
        return [
            {"subject": ARTIST_ID, "kind": "artist", "field": "name",
             "value": ["Radiohead"], "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": ARTIST_ID, "kind": "artist",
             "field": "artist_mbid", "value": "mb-art-1",
             "source": "musicbrainz",
             "soulid_version": C.SOULID_VERSION},
            {"subject": ARTIST_ID, "kind": "artist",
             "field": "deezer_id", "value": "399", "source": "deezer",
             "soulid_version": C.SOULID_VERSION},
        ]

    def _mbid_album_claims(self):
        return [
            {"subject": ALBUM_SUBJ, "kind": "album", "field": "name",
             "value": ["Radiohead", "OK Computer"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": ALBUM_SUBJ, "kind": "album",
             "field": "release_group_mbid", "value": "mb-rg-1",
             "source": "musicbrainz",
             "soulid_version": C.SOULID_VERSION},
            {"subject": ALBUM_SUBJ, "kind": "album",
             "field": "provider_link",
             "value": {"provider": "musicbrainz",
                       "from_id": "mb-rg-1", "to_kind": "artist",
                       "to_id": "mb-art-1", "relation": "album_of"},
             "source": "musicbrainz",
             "soulid_version": C.SOULID_VERSION},
        ]

    def test_network_only_mbid_does_not_settle(self):
        # Gate MF-2: a strong-verified MBID that only the network
        # asserts is the crowd agreeing with itself — settlement
        # needs the MBID shared with the user's own data.
        envs = [
            envelope(self.origin, claims=self._mbid_artist_claims(),
                     attestation=standing_attestation(self.origin)),
            envelope(self.origin, claims=self._mbid_album_claims(),
                     attestation=standing_attestation(self.origin)),
        ]
        querier = self.querier(FakeRelayClient(envs))
        querier.query_exact("artist", [ARTIST_ID])
        self.store.set_verdict(
            self.origin.public_key_b64url, ARTIST_ID, "artist_mbid",
            "mb-art-1", "strong-verified", NOW - 3600, "verified")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertIsNone(block["settled"])

    def test_locally_shared_mbid_settles(self):
        # The control: the user's own library carries the same
        # MBID, and the network's copy is strong-verified — the
        # shared-MBID rung fires. (Envelopes are admitted directly:
        # once the own claim lands, the subject is live and an ask
        # would never go to the network for it.)
        querier = self.querier(FakeRelayClient())
        for env in (
                envelope(self.origin,
                         claims=self._mbid_artist_claims(),
                         attestation=standing_attestation(
                             self.origin)),
                envelope(self.origin,
                         claims=self._mbid_album_claims(),
                         attestation=standing_attestation(
                             self.origin))):
            querier.admit(env)
        self.store.replace_owner_claims(
            "seed:artist",
            [{"subject": ARTIST_ID, "kind": "artist",
              "field": "artist_mbid", "value": "mb-art-1",
              "source": "musicbrainz",
              "soulid_version": C.SOULID_VERSION}],
            self.identity.public_key_b64url,
            self.identity.signing_private_key)
        self.store.set_verdict(
            self.origin.public_key_b64url, ARTIST_ID, "artist_mbid",
            "mb-art-1", "strong-verified", NOW - 3600, "verified")
        record = querier.record(ARTIST_ID)
        block = record["disambiguation"]
        self.assertEqual(block["settled"], "musicbrainz:mb-art-1")
        self.assertEqual(block["via"], "shared_mbid")
        self.assertEqual(block["settled_at"], NOW - 3600)


class ExpiredSchedulingTest(QuerierCase):
    """Gate MF-1 + SF-6 (querier side): expired claims schedule
    no verification — a verdict must never resurrect them — and
    fields §7 can never grade don't hold a subject in the queue."""

    def test_expired_claims_schedule_no_verification(self):
        env = envelope(self.origin, claims=track_claims(),
                       asserted_at=NOW - 100 * DAY)
        self.querier().admit(env)
        fake = RecordingVerifier()
        querier = self.querier(FakeRelayClient(), verifier=fake,
                               verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        self.assertEqual(fake.calls, [])

    def test_unverifiable_fields_alone_schedule_nothing(self):
        claims = [
            {"subject": SUBJECT, "kind": "track", "field": "name",
             "value": ["Radiohead", "Airbag"], "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
        ]
        env = envelope(self.origin, claims=claims,
                       attestation=standing_attestation(self.origin))
        fake = RecordingVerifier()
        querier = self.querier(FakeRelayClient([env]), verifier=fake,
                               verify_inline=True)
        querier.query_exact("track", [SUBJECT])
        self.assertEqual(fake.calls, [])

    def test_expired_challenger_fires_no_sanity_trigger(self):
        # Gate MF-1: _album_track_conflicts judges challengers on
        # live rows only — an expired origin's stale variant is
        # not a challenge.
        querier = self.querier(FakeRelayClient())
        entry = {"variants": {
            "mb:rel-1": {"tracks": [{"tier": "strong-verified"}]},
            "mb:rel-2": {"tracks": [{"tier": "unverified"}]}}}

        def slot_row(origin, variant, asserted_at, verdict=None):
            return {"subject": ALBUM_SUBJ, "kind": "album",
                    "field": "album_track",
                    "value": {"track": SUBJECT, "disc": 1,
                              "position": 1, "variant": variant},
                    "source": "musicbrainz", "origin": origin,
                    "asserted_at": asserted_at, "verdict": verdict}

        strong = {"tier": "strong-verified",
                  "verified_at": NOW - 100}
        rows = [
            slot_row("origin-a", "mb:rel-1", NOW - 100, strong),
            slot_row("origin-b", "mb:rel-2", NOW - 100),
            slot_row("origin-c", "mb:rel-2", NOW - 100 * DAY),
        ]
        self.assertEqual(
            querier._album_track_conflicts(entry, rows),
            {"origin-b"})


class ExpiredAskTest(QuerierCase):
    """MF-3: cache rows expired under §6 aging count as MISSING
    on both ask paths — the subject is re-asked, and the merged
    record's found is computed after expiry filtering."""

    def _seed_expired(self):
        env = envelope(self.origin, claims=track_claims(),
                       asserted_at=NOW - 100 * DAY)
        self.querier().admit(env)
        self.assertEqual(self.store.counts()["cached_batches"], 1)

    def _fresh_envelope(self):
        return envelope(self.origin, claims=track_claims(),
                        attestation=standing_attestation(self.origin))

    def test_expired_only_subject_reports_not_found(self):
        self._seed_expired()
        record = self.querier().record("deezer:12345")
        self.assertIsNotNone(record)  # the shell still surfaces
        self.assertFalse(record["found"])
        self.assertEqual(record["fields"], {})

    def test_expired_only_subject_is_reasked_on_exact_path(self):
        self._seed_expired()
        relay = FakeRelayClient([self._fresh_envelope()])
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(out["asked_network"])
        self.assertEqual(out["admitted"], 1)
        self.assertTrue(relay.queries)  # the ask really went out
        record = out["records"][0]
        self.assertTrue(record["found"])
        e = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["value"], "12345")
        self.assertEqual(e["support"], 1)  # fresh standing origin

    def test_expired_only_subject_is_reasked_on_text_path(self):
        self._seed_expired()
        relay = FakeRelayClient([self._fresh_envelope()])
        querier = self.querier(relay)
        out = querier.query_text("track", ["Airbag"])
        self.assertTrue(out["asked_network"])
        self.assertEqual(out["admitted"], 1)
        self.assertEqual(len(out["records"]), 1)
        self.assertTrue(out["records"][0]["found"])

    def test_live_cache_still_answers_without_asking(self):
        relay = FakeRelayClient([self._fresh_envelope()])
        querier = self.querier(relay)
        querier.query_exact("track", [SUBJECT])
        relay.queries.clear()
        out = querier.query_exact("track", [SUBJECT])
        self.assertFalse(out["asked_network"])
        self.assertEqual(relay.queries, [])
        self.assertTrue(out["records"][0]["found"])


class LegacyAliasTest(QuerierCase):
    def test_legacy_resolves_only_through_verified_alias(self):
        querier = self.querier(FakeRelayClient())
        legacy = "soul_legacy_airbag"
        out = querier.query_exact("track", [legacy])
        self.assertFalse(out["records"][0]["found"])
        self.store.learn_legacy_alias(legacy, SUBJECT)
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        querier.admit(env)
        out = querier.query_exact("track", [legacy])
        self.assertTrue(out["records"][0]["found"])
        self.assertEqual(out["records"][0]["subject"], SUBJECT)

    def test_builder_learns_alias_from_export_rows(self):
        album_row = dict(ALBUM)
        album_row["soul_id_legacy"] = "soul_legacy_okcomputer"
        pages = full_pages(album_items=(album_row,))
        store = MetaStore(self.tmp.name)
        builder = B.LibraryBuilder(FakeSoulSync(pages), self.identity,
                                   store)
        builder.run("2026-10-26T00:00:00+00:00")
        self.assertEqual(store.legacy_subject("soul_legacy_okcomputer"),
                         ALBUM_ID)

    def test_builder_refuses_unverified_alias(self):
        # The legacy id is served, but the row's v2 id does NOT
        # recompute from its name parts (the export lied): nothing
        # is learned.
        album_row = dict(ALBUM)
        album_row["soul_id"] = S.generate_soul_id(
            "album", "Radiohead", "Kid A")
        album_row["soul_id_legacy"] = "soul_legacy_okcomputer"
        pages = {
            ("artist", ""): page([]),
            ("album", ""): page([album_row]),
            ("track", ""): page([]),
        }
        store = MetaStore(self.tmp.name)
        builder = B.LibraryBuilder(FakeSoulSync(pages), self.identity,
                                   store)
        builder.run("2026-10-26T00:00:00+00:00")
        self.assertIsNone(store.legacy_subject("soul_legacy_okcomputer"))


class RevocationMidFlightTest(QuerierCase):
    def test_purge_invalidates_the_merged_view(self):
        revocations = RevocationStore()
        self.store = MetaStore(self.tmp.name, revocations=revocations)
        querier = self.querier(FakeRelayClient())
        env = envelope(self.origin, claims=track_claims(),
                       attestation=standing_attestation(self.origin))
        querier.admit(env)
        self.assertIsNotNone(querier.record(SUBJECT))
        # The list lands (the relay client's revocations_applied
        # wiring calls exactly this purge in service).
        node_id = node_id_for_key(self.origin.public_key_b64url)
        sig = ISSUER_PRIVATE.sign(
            list_signing_text(ISSUER, 1, NOW, [node_id]))
        applied = revocations.apply(
            {"issuer": ISSUER, "seq": 1, "issued_at": NOW,
             "keys": [node_id], "sig": b64url_encode(sig)},
            ISSUER_PUBLIC)
        self.assertIsNotNone(applied)
        dropped = self.store.purge_revoked(set(applied.keys))
        self.assertEqual(dropped, 1)
        self.assertIsNone(querier.record(SUBJECT))
        # And a fresh copy of the same batch is refused at the door.
        out = querier.admit(env)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "origin_revoked")


class AgingTest(QuerierCase):
    def test_old_unverified_batch_ages_out_of_the_view(self):
        old = envelope(self.origin, claims=track_claims(),
                       asserted_at=NOW - 100 * DAY)
        querier = self.querier(FakeRelayClient())
        out = querier.admit(old, now=float(NOW))
        self.assertTrue(out.admitted)
        record = querier.record(SUBJECT)
        self.assertIsNotNone(record)
        self.assertEqual(record["fields"], {})
        self.assertGreaterEqual(record["expired_claims"], 1)

    def test_reserve_keeps_origin_age(self):
        # A forwarder re-serves the old batch TODAY: the claims
        # still age from the ORIGIN's asserted_at, never from the
        # forwarding.
        inner = envelope(self.origin, claims=track_claims(),
                         asserted_at=NOW - 100 * DAY)
        forwarder = NodeIdentity(key="dd" * 32)
        fwd = {"origin_batch_id": C.batch_digest(inner["batch"]),
               "origin_node": self.origin.public_key_b64url,
               "forwarded_at": "2026-10-27T00:00:00+00:00"}
        fwd["sig"] = forwarder.sign(C.canonical_bytes(fwd))
        querier = self.querier(FakeRelayClient())
        out = querier.admit({"forward": fwd, "envelope": inner},
                            now=float(NOW))
        self.assertTrue(out.admitted)
        record = querier.record(SUBJECT)
        self.assertEqual(record["fields"], {})


class RelayRoundTripTest(unittest.TestCase):
    """Two and three nodes against the real relay server."""

    @classmethod
    def setUpClass(cls):
        cls.pool_private = Ed25519PrivateKey.generate()
        cls.pool_public = cls.pool_private.public_key()

    def setUp(self):
        self.server = RelayServerThread(self.pool_public)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.clients = []
        self.addCleanup(lambda: [c.stop() for c in self.clients])
        self.tmps = []
        self.addCleanup(lambda: [t.cleanup() for t in self.tmps])

    def _tmp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmps.append(tmp)
        return tmp.name

    def _connect(self, identity, mode="own_library"):
        token = issue_token(self.pool_private, KEY_ID,
                            identity.node_id, FULL_SCOPES)
        client = RelayClient(
            identity, url_provider=lambda: self.server.url,
            mode_provider=lambda: mode,
            token_provider=lambda: token)
        self.clients.append(client)
        client.start()
        wait_until(lambda: client.connected)
        return client

    def _responder_node(self, key_hex, album_row=None):
        tmp = self._tmp()
        from metasync_node.identity import load_or_create_identity
        identity = load_or_create_identity(tmp)
        rows = (album_row,) if album_row is not None else None
        store = build_store(tmp, identity) if rows is None else None
        if store is None:
            store = MetaStore(tmp)
            builder = B.LibraryBuilder(
                FakeSoulSync(full_pages(album_items=rows)), identity,
                store)
            builder.run("2026-10-26T00:00:00+00:00")
        store.set_attestation(standing_attestation(identity))
        responder = Responder(store, identity,
                              mode_provider=lambda: "own_library")
        client = self._connect(identity)
        client.set_query_handler(responder.handle)
        return identity, store

    def _album_row_with_group(self):
        row = dict(ALBUM)
        row["musicbrainz_release_group_id"] = "mb-rg-1"
        return row

    def _fake_verifier(self, store):
        mb = FakeFetcher("musicbrainz", {
            ("album_by_id", "mb-rg-1"): {
                "provider": "musicbrainz", "id": "mb-rg-1",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997,
                "links": {"deezer": "3135556"}, "release_id": "rel-1",
                "tracks": []},
        })
        dz = FakeFetcher("deezer", {
            ("album_by_id", "3135556"): {
                "provider": "deezer", "id": "3135556",
                "title": "OK Computer", "artist": "Radiohead",
                "upc": None, "year": 1997, "links": {},
                "release_id": None, "tracks": []},
        })
        return V.Verifier(store, {"musicbrainz": mb, "deezer": dz},
                          clock=lambda: float(NOW))

    def test_two_node_round_trip_and_cache_hit(self):
        self._responder_node("a1" * 16,
                             album_row=self._album_row_with_group())
        asker_tmp = self._tmp()
        asker_identity = NodeIdentity(key="ee" * 32)
        asker_store = MetaStore(asker_tmp)
        asker_client = self._connect(asker_identity)
        querier = Querier(
            asker_store, asker_identity,
            relay_provider=lambda: asker_client,
            verifier=self._fake_verifier(asker_store),
            issuer_key_provider=lambda: ISSUER_PUBLIC,
            own_issuer_provider=lambda: ISSUER,
            clock=lambda: float(NOW), verify_inline=True)

        routed_before = self.server.relay.counters.queries_routed
        out = querier.query_exact("album", ["deezer:3135556"])
        self.assertTrue(out["asked_network"])
        self.assertGreaterEqual(out["admitted"], 1)
        self.assertGreater(self.server.relay.counters.queries_routed,
                           routed_before)
        record = out["records"][0]
        self.assertEqual(record["subject"], ALBUM_ID)
        self.assertTrue(record["found"])
        # The fake MB/DZ pair cross-matches: strong-verified.
        deezer = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(deezer["value"], "3135556")
        self.assertEqual(deezer["tier"], "strong-verified")
        self.assertEqual(deezer["support"], 1)
        group = record["fields"]["release_group_mbid"]["values"][0]
        self.assertEqual(group["tier"], "strong-verified")

        # Second identical query: answered from cache, and the
        # relay's own counters prove zero new queries were routed.
        routed_mid = self.server.relay.counters.queries_routed
        out2 = querier.query_exact("album", ["deezer:3135556"])
        self.assertFalse(out2["asked_network"])
        self.assertEqual(self.server.relay.counters.queries_routed,
                         routed_mid)
        self.assertTrue(out2["records"][0]["found"])
        self.assertEqual(
            out2["records"][0]["fields"]["deezer_id"]["values"][0]
            ["tier"], "strong-verified")

    def test_three_node_support_counts_standing_origins(self):
        row = self._album_row_with_group()
        self._responder_node("a1" * 16, album_row=row)
        self._responder_node("a2" * 16, album_row=row)
        asker_tmp = self._tmp()
        asker_identity = NodeIdentity(key="ee" * 32)
        asker_store = MetaStore(asker_tmp)
        asker_client = self._connect(asker_identity)
        querier = Querier(asker_store, asker_identity,
                          relay_provider=lambda: asker_client,
                          issuer_key_provider=lambda: ISSUER_PUBLIC,
                          own_issuer_provider=lambda: ISSUER,
                          clock=lambda: float(NOW))
        out = querier.query_exact("album", [ALBUM_ID])
        self.assertTrue(out["asked_network"])
        record = out["records"][0]
        self.assertTrue(record["found"])
        deezer = record["fields"]["deezer_id"]["values"][0]
        # Two distinct standing origins assert the same id: the
        # support is 2, deduped by origin key (each responder sent
        # two batches — re-servable + own-only partitions).
        self.assertEqual(deezer["support"], 2)
        self.assertEqual(deezer["support_label"], "1-2 nodes")
        self.assertEqual(len(deezer["origins"]), 2)


class ServiceSurfaceTest(unittest.TestCase):
    """POST /local/meta/query, GET /local/meta/record/<subject>,
    and the meta_status querier addition — the established
    local-key pattern, mirroring /local/meta/preview."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from metasync_node.config import Config
        from metasync_node.service import NodeService
        from metasync_node.store import Store
        self.store = Store(self.tmp.name)
        self.identity = NodeIdentity(key="ef" * 32)
        fake = FakeSoulSync(full_pages())
        self.service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity, soulsync=fake,
            env={})
        self.service.rebuild_meta_store(self.service.clock())
        from metasync_node.app import create_app
        self.client = create_app(self.service).test_client()
        self.key = self.service.issue_local_api_key()

    def auth(self):
        return {"Authorization": f"Bearer {self.key}"}

    def test_query_route_requires_the_local_key(self):
        res = self.client.post("/local/meta/query", json={
            "kind": "album", "mode": "exact", "subjects": [ALBUM_ID]})
        self.assertEqual(res.status_code, 401)

    def test_query_route_answers_from_local_library(self):
        res = self.client.post(
            "/local/meta/query",
            json={"kind": "album", "mode": "exact",
                  "subjects": ["deezer:3135556"]},
            headers=self.auth())
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        # No relay client exists in this service: nothing was asked.
        self.assertFalse(body["asked_network"])
        record = body["records"][0]
        self.assertEqual(record["subject"], ALBUM_ID)
        self.assertEqual(record["version"], 1)
        self.assertTrue(
            record["fields"]["deezer_id"]["values"][0]["local"])

    def test_query_route_rejects_bad_asks(self):
        for payload in ({"kind": "album", "mode": "exact"},
                        {"kind": "nope", "mode": "exact",
                         "subjects": [ALBUM_ID]},
                        {"kind": "album", "mode": "sideways",
                         "subjects": [ALBUM_ID]}):
            res = self.client.post("/local/meta/query", json=payload,
                                   headers=self.auth())
            self.assertEqual(res.status_code, 400, payload)
            self.assertFalse(res.get_json()["ok"])

    def test_query_route_serves_the_coarse_projection(self):
        # R1-SF-3/R2-SF-9: bucketed support, and no provenance
        # internals anywhere in the served JSON.
        res = self.client.post(
            "/local/meta/query",
            json={"kind": "album", "mode": "exact",
                  "subjects": ["deezer:3135556"]},
            headers=self.auth())
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertNotIn("origins", text)
        self.assertNotIn("batches", text)
        record = res.get_json()["records"][0]
        e = record["fields"]["deezer_id"]["values"][0]
        self.assertTrue(e["local"])
        self.assertEqual(e["support"], "none")  # a bucket, not 0

    def test_query_route_carries_no_admission_tallies(self):
        # Gate MF-3: admitted/dropped are exact counts of
        # answering batches — they stay on the node-internal
        # status surface and never ride this response.
        res = self.client.post(
            "/local/meta/query",
            json={"kind": "album", "mode": "exact",
                  "subjects": ["deezer:3135556"]},
            headers=self.auth())
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertNotIn("admitted", text)
        self.assertNotIn("dropped", text)
        body = res.get_json()
        self.assertIn("asked_network", body)
        self.assertIn("unresolved", body)

    def test_record_route(self):
        res = self.client.get(f"/local/meta/record/{ALBUM_ID}",
                              headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["record"]["subject"], ALBUM_ID)
        # A provider ref rides the path too.
        res = self.client.get("/local/meta/record/deezer:3135556",
                              headers=self.auth())
        self.assertEqual(res.status_code, 200)
        res = self.client.get(
            f"/local/meta/record/{S.generate_soul_id('album', 'X', 'Y')}",
            headers=self.auth())
        self.assertEqual(res.status_code, 404)
        res = self.client.get(f"/local/meta/record/{ALBUM_ID}")
        self.assertEqual(res.status_code, 401)

    def test_status_carries_the_querier_block(self):
        status = self.service.meta_status()
        querier = status["querier"]
        self.assertEqual(querier["cached_batches"], 0)
        self.assertEqual(querier["cached_claims"], 0)
        self.assertIn("revocations", querier)
        self.assertEqual(
            querier["verification"],
            {"unverified_claims": 0, "stale_verdicts": 0})
        self.assertEqual(status["store"]["legacy_aliases"], 0)


class AttestationSelectionTest(unittest.TestCase):
    """Step 6's per-pool selection: the embedded attestation is
    the SESSION pool's (the first joined pool, whose token the
    relay session runs on) — never another pool's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from metasync_node.config import Config
        from metasync_node.service import NodeService
        from metasync_node.store import Store
        self.store = Store(self.tmp.name)
        self.identity = NodeIdentity(key="ef" * 32)
        self.service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity,
            soulsync=FakeSoulSync(full_pages()), env={})
        self.att_a = {"issuer": "pool-a"}
        self.att_b = {"issuer": "pool-b"}
        self.pools = {
            "a": {"id": "a", "url": "https://a.example",
                  "api_key": "k", "enabled": True},
            "b": {"id": "b", "url": "https://b.example",
                  "api_key": "k", "enabled": True},
        }
        self.atts = {"a": None, "b": self.att_b}

    def _wire(self, order):
        clients = {}

        class FakeClient:
            def __init__(self, att):
                self._att = att

            def current_attestation(self):
                return self._att

        for pool_id in order:
            clients[pool_id] = FakeClient(self.atts[pool_id])
        self.service.pools = lambda: [self.pools[p] for p in order]
        self.service.pool_client = lambda pool: clients[pool["id"]]

    def test_session_pool_attestation_wins(self):
        self._wire(["a", "b"])
        self.assertIsNone(self.service._current_attestation())
        note = self.service._attestation_pool_note()
        self.assertIn("pool a", note)
        self.atts["a"] = self.att_a
        self._wire(["a", "b"])
        self.assertEqual(self.service._current_attestation(),
                         self.att_a)
        self.assertIsNone(self.service._attestation_pool_note())

    def test_single_pool_unchanged(self):
        self.atts["a"] = self.att_a
        self._wire(["a"])
        self.assertEqual(self.service._current_attestation(),
                         self.att_a)
        self.assertIsNone(self.service._attestation_pool_note())


class _CloseStream:
    """A query stream the querier CAN close: records the close so a
    test can tell an early-closed ask from a full-window one."""

    def __init__(self, envelopes):
        self._it = iter(list(envelopes))
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


class _UnnegotiatedStream:
    """A stream from a session that never negotiated early close
    (gate fold 2, SF-1): close_early() is the negotiated path —
    it is consulted at the bar and refuses, nothing is ever sent —
    so the ask must consume the full window, and the plain
    close() must never be reached on this path either."""

    def __init__(self, envelopes):
        self._it = iter(list(envelopes))
        self.close_early_calls = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close_early(self):
        self.close_early_calls += 1
        return False

    def close(self):
        self.closed = True


class UnnegotiatedFakeRelayClient(FakeRelayClient):
    def query(self, kind, mode, subjects=None, terms=None, **kwargs):
        self.queries.append(
            {"kind": kind, "mode": mode, "subjects": subjects,
             "terms": terms})
        self.stream = _UnnegotiatedStream(self.envelopes)
        return self.stream


class ClosingFakeRelayClient(FakeRelayClient):
    def query(self, kind, mode, subjects=None, terms=None, **kwargs):
        self.queries.append(
            {"kind": kind, "mode": mode, "subjects": subjects,
             "terms": terms})
        self.stream = _CloseStream(self.envelopes)
        return self.stream


class EarlyCloseTest(QuerierCase):
    """Early close (§19): an EXACT ask stops waiting once the
    MERGED view is sufficient — every carried subject found with
    a value at support ≥ EARLY_CLOSE_SUPPORT (5 distinct standing
    origins). Niche asks never close; a stream that cannot be
    closed behaves exactly as asks always have. TEXT asks never
    close at all: their resolution set grows as answers land, so
    a mid-stream check would truncate the slow terms."""

    def _popular(self, count, deezer_ids=None):
        envs = []
        for n in range(count):
            origin = NodeIdentity(key=f"{n + 1:02x}" * 32)
            deezer_id = "12345" if deezer_ids is None else deezer_ids[n]
            envs.append(envelope(
                origin, claims=track_claims(deezer_id=deezer_id),
                attestation=standing_attestation(origin)))
        return envs

    @staticmethod
    def _claims(subject, name, *, deezer_id=None, spotify_id=None):
        claims = [
            {"subject": subject, "kind": "track", "field": "name",
             "value": name, "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
        ]
        if deezer_id is not None:
            claims.append(
                {"subject": subject, "kind": "track",
                 "field": "deezer_id", "value": deezer_id,
                 "source": "deezer",
                 "soulid_version": C.SOULID_VERSION})
        if spotify_id is not None:
            claims.append(
                {"subject": subject, "kind": "track",
                 "field": "spotify_id", "value": spotify_id,
                 "source": "spotify",
                 "soulid_version": C.SOULID_VERSION})
        return claims

    @staticmethod
    def _forwarded(inner, forwarder):
        fwd = {"origin_batch_id": C.batch_digest(inner["batch"]),
               "origin_node": inner["batch"]["header"]["node"],
               "forwarded_at": "2026-10-27T00:00:00+00:00"}
        fwd["sig"] = forwarder.sign(C.canonical_bytes(fwd))
        return {"forward": fwd, "envelope": inner}

    def test_popular_exact_stream_closes_at_the_bar(self):
        relay = ClosingFakeRelayClient(self._popular(6))
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(relay.stream.closed)
        # The close fired the moment the fifth standing origin
        # landed: the sixth envelope was never even admitted.
        self.assertEqual(out["admitted"], 5)
        record = out["records"][0]
        self.assertTrue(record["found"])
        entry = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 5)

    def test_text_ask_rides_the_full_window_for_late_terms(self):
        # A text ask NEVER closes early (fold, §19): its terms
        # re-resolve over a PROGRESSIVELY growing set, so a
        # sufficiency check mid-stream judges only the subjects
        # resolved so far — here the fast popular term alone, at
        # the bar after five envelopes — and would close the
        # query over the obscure term whose only answer arrives
        # LAST. The pin: the obscure term's record comes back.
        popular = self._popular(6)
        obscure_subject = S.generate_soul_id(
            "track", "Zqx Wobble", "Obscure Song")
        obscure_claims = [
            {"subject": obscure_subject, "kind": "track",
             "field": "name", "value": ["Zqx Wobble", "Obscure Song"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": obscure_subject, "kind": "track",
             "field": "deezer_id", "value": "777",
             "source": "deezer", "soulid_version": C.SOULID_VERSION},
        ]
        slow_origin = NodeIdentity(key="07" * 32)
        slow = envelope(
            slow_origin, claims=obscure_claims,
            attestation=standing_attestation(slow_origin))
        relay = ClosingFakeRelayClient(popular + [slow])
        querier = self.querier(relay)
        out = querier.query_text("track", ["Airbag", "Obscure Song"])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 7)
        by_subject = {r["subject"]: r for r in out["records"]}
        self.assertTrue(by_subject[SUBJECT]["found"])
        obscure = by_subject[obscure_subject]
        self.assertTrue(obscure["found"])
        self.assertEqual(
            obscure["fields"]["deezer_id"]["values"][0]["value"],
            "777")

    def test_niche_stream_never_closes(self):
        relay = ClosingFakeRelayClient(self._popular(2))
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 2)
        self.assertTrue(out["records"][0]["found"])

    def test_busy_but_disagreeing_stream_never_closes(self):
        # Six standing origins, six DIFFERENT deezer ids: plenty of
        # answers, no value five origins agree on. Head-count alone
        # is not sufficiency — the window runs out in full.
        relay = ClosingFakeRelayClient(
            self._popular(6, deezer_ids=[str(2000 + n) for n in range(6)]))
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 6)

    def test_unresolved_verbatim_ref_blocks_the_close(self):
        # The query carries deezer:999999 verbatim; the stream is
        # popular-shaped but never resolves that ref, so some later
        # answer might still have held it — no close.
        relay = ClosingFakeRelayClient(self._popular(6))
        querier = self.querier(relay)
        out = querier.query_exact("track", ["deezer:999999"])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 6)
        self.assertEqual(out["unresolved"], ["deezer:999999"])

    def test_verbatim_ref_closes_on_its_matching_identity(self):
        # The positive side of the verbatim rule: five direct
        # standing origins assert the very deezer id the asker
        # named, on the subject it resolves to — the matching
        # value carries the bar and the ask closes there.
        relay = ClosingFakeRelayClient(self._popular(6))
        querier = self.querier(relay)
        out = querier.query_exact("track", ["deezer:12345"])
        self.assertTrue(relay.stream.closed)
        self.assertEqual(out["admitted"], 5)
        self.assertEqual(out["unresolved"], [])
        record = out["records"][0]
        self.assertEqual(record["subject"], SUBJECT)
        entry = record["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 5)

    def test_verbatim_ref_needs_the_matching_identity_supported(self):
        # The gate's steering trace, end to end: ONE zero-standing
        # key plants deezer:X on a popular subject (admission writes
        # the provider-index link even at zero support); five
        # forwarded standing batches then make an UNRELATED identity
        # on that subject clear the general bar; one more direct
        # standing batch supplies the second direct answerer, so the
        # direct-answerer rule is satisfied too. Only the verbatim
        # rule can keep this open: the asked-for deezer value on the
        # resolved subject has support 0. The real owner of X
        # answers last and must be read.
        ref_id = "424242"
        attacker = NodeIdentity(key="a1" * 32)
        steering = envelope(
            attacker,
            claims=self._claims(
                SUBJECT, ["Radiohead", "Airbag"], deezer_id=ref_id))
        forwarder = NodeIdentity(key="f1" * 32)
        forwarded = []
        for n in range(5):
            origin = NodeIdentity(key=f"{n + 1:02x}" * 32)
            inner = envelope(
                origin,
                claims=self._claims(
                    SUBJECT, ["Radiohead", "Airbag"],
                    spotify_id="spotify-popular"),
                attestation=standing_attestation(origin))
            forwarded.append(self._forwarded(inner, forwarder))
        direct_supporter = NodeIdentity(key="06" * 32)
        direct = envelope(
            direct_supporter,
            claims=self._claims(
                SUBJECT, ["Radiohead", "Airbag"],
                spotify_id="spotify-popular"),
            attestation=standing_attestation(direct_supporter))
        real_subject = S.generate_soul_id(
            "track", "Radiohead", "Real Airbag")
        real_owner = NodeIdentity(key="07" * 32)
        real_answer = envelope(
            real_owner,
            claims=self._claims(
                real_subject, ["Radiohead", "Real Airbag"],
                deezer_id=ref_id),
            attestation=standing_attestation(real_owner))

        relay = ClosingFakeRelayClient(
            [steering, *forwarded, direct, real_answer])
        querier = self.querier(relay)
        out = querier.query_exact("track", [f"deezer:{ref_id}"])

        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 8)
        by_subject = {r["subject"]: r for r in out["records"]}
        steered = by_subject[SUBJECT]
        self.assertTrue(steered["found"])
        # The unrelated identity really did clear the general bar —
        # under the old any-identity rule this ask closed before the
        # real owner arrived.
        spotify = steered["fields"]["spotify_id"]["values"][0]
        self.assertEqual(spotify["support"], 6)
        planted = steered["fields"]["deezer_id"]["values"][0]
        self.assertEqual(planted["value"], ref_id)
        self.assertEqual(planted["support"], 0)
        # The dispute is visible in the final result and the real
        # owner's answer was read, not cut off.
        real = by_subject[real_subject]
        self.assertTrue(real["found"])
        real_value = real["fields"]["deezer_id"]["values"][0]
        self.assertEqual(real_value["value"], ref_id)
        self.assertEqual(real_value["support"], 1)

    def test_forwarded_replays_alone_never_close(self):
        # Five standing origins' batches, every one replayed by a
        # SINGLE forwarder: the support bar is met on paper, but no
        # node answered this ask with its own batch, so sufficiency
        # is never even checked and the window runs out in full.
        forwarder = NodeIdentity(key="f1" * 32)
        relay = ClosingFakeRelayClient(
            [self._forwarded(inner, forwarder)
             for inner in self._popular(5)])
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 5)
        entry = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 5)

    def test_forwarded_bar_closes_after_two_direct_answerers(self):
        # The same forwarded bar, followed by direct batches from
        # two further origins: the deferred check runs when the
        # second direct answerer lands and closes there — the
        # eighth envelope is never admitted.
        forwarder = NodeIdentity(key="f1" * 32)
        envs = [self._forwarded(inner, forwarder)
                for inner in self._popular(5)]
        for key in ("06", "07", "08"):
            origin = NodeIdentity(key=key * 32)
            envs.append(envelope(
                origin, claims=track_claims(),
                attestation=standing_attestation(origin)))
        relay = ClosingFakeRelayClient(envs)
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(relay.stream.closed)
        self.assertEqual(out["admitted"], 7)
        entry = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 7)

    def test_shapeless_forward_replays_never_close(self):
        # Gate fold 2, the must-fix regression: one forwarder
        # replays five standing origins' batches as
        # {"forward": {}, "envelope": <origin batch>} — a dict
        # `forward` with the signed forwarding fields left out.
        # Unwrap used to hand admission the inner batch while the
        # classifier called it NOT forwarded, so each replay
        # counted as its own DIRECT answerer: standing 5, direct
        # 5, and the ask closed on one node's cache. Admission now
        # drops the shape as malformed before any accounting, so
        # nothing is admitted and the window runs out in full.
        relay = ClosingFakeRelayClient(
            [{"forward": {}, "envelope": inner}
             for inner in self._popular(5)])
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 0)
        self.assertEqual(out["dropped"], 5)
        self.assertFalse(out["records"][0]["found"])

    def test_direct_answerers_need_standing(self):
        # Gate fold 2, SF-2: the 2-answerer rule counts only
        # direct answerers whose own batches carry STANDING. The
        # forwarded bar plus two direct answers from fresh
        # zero-standing keys — and even one direct standing
        # answer — must not clear it; only the SECOND direct
        # standing answer does.
        forwarder = NodeIdentity(key="f1" * 32)
        envs = [self._forwarded(inner, forwarder)
                for inner in self._popular(5)]
        for key in ("11", "12"):
            fresh = NodeIdentity(key=key * 32)
            envs.append(envelope(fresh, claims=track_claims()))
        for key in ("06", "07", "08"):
            origin = NodeIdentity(key=key * 32)
            envs.append(envelope(
                origin, claims=track_claims(),
                attestation=standing_attestation(origin)))
        relay = ClosingFakeRelayClient(envs)
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(relay.stream.closed)
        # 5 forwarded + 2 zero-standing direct + "06" (first
        # standing direct: rule still short) + "07" (second: the
        # deferred check runs and closes). "08" is never read —
        # had zero-standing directs counted, the close would have
        # fired two envelopes earlier.
        self.assertEqual(out["admitted"], 9)

    def test_own_key_never_counts_as_a_direct_answerer(self):
        # Gate fold 2, fix (c): this node's own batch arriving as
        # a direct standing answer to its own ask is not ANOTHER
        # node answering. The forwarded bar plus the asker's own
        # direct batch plus one other direct standing batch stays
        # deferred; the close fires only when a second OTHER node
        # answers directly.
        forwarder = NodeIdentity(key="f1" * 32)
        envs = [self._forwarded(inner, forwarder)
                for inner in self._popular(5)]
        envs.append(envelope(
            self.identity, claims=track_claims(),
            attestation=standing_attestation(self.identity)))
        for key in ("06", "07", "08"):
            origin = NodeIdentity(key=key * 32)
            envs.append(envelope(
                origin, claims=track_claims(),
                attestation=standing_attestation(origin)))
        relay = ClosingFakeRelayClient(envs)
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertTrue(relay.stream.closed)
        # 5 forwarded + own + "06" + "07" (the second other direct
        # standing answerer) = 8; had the own key counted, the
        # close would have fired at 7.
        self.assertEqual(out["admitted"], 8)

    def test_unnegotiated_session_rides_the_full_window(self):
        # Gate fold 2, SF-1: sufficiency is reached (five direct
        # standing answers), the negotiated path is consulted —
        # and refuses, because this session never negotiated the
        # close. The ask must then consume the FULL window: no
        # local early exit while the relay keeps forwarding.
        relay = UnnegotiatedFakeRelayClient(self._popular(6))
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertGreaterEqual(relay.stream.close_early_calls, 1)
        self.assertFalse(relay.stream.closed)
        self.assertEqual(out["admitted"], 6)
        entry = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 6)

    def test_stream_without_close_is_exactly_todays_behavior(self):
        # The plain fake's stream has no close(): the ask consumes
        # everything and merges it all, as asks did before the
        # message existed (the lost-close case, by construction).
        relay = FakeRelayClient(self._popular(6))
        querier = self.querier(relay)
        out = querier.query_exact("track", [SUBJECT])
        self.assertEqual(out["admitted"], 6)
        entry = out["records"][0]["fields"]["deezer_id"]["values"][0]
        self.assertEqual(entry["support"], 6)


if __name__ == "__main__":
    unittest.main()

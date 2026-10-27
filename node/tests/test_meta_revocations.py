"""Revocation enforcement tests (DESIGN-METADATA.md §9/§10; gate
M2/M3/S2).

The trust model's kill path, node side: a verified issuer-signed
list must actually DO something. These tests pin:

- admission: MetaStore.put_cached_batch refuses a batch whose
  origin key is named on a held list — including a batch whose
  embedded attestation names a MADE-UP issuer: the header's
  issuer claim is unverified at receipt, so admission and the
  read filter use the union over held lists (gate re-pass N1);
- re-serving: cached batches of a revoked origin are filtered out
  of every cache read (and so out of the responder's re-serve),
  even when they were cached before the key was revoked;
- purge: purge_revoked drops a named key's cached batches (claims
  and provenance rows with them), never own batches;
- durability: the per-issuer high-water mark AND the signed list
  survive a restart — the list re-verified against the issuer key
  on load, never trusted as file bytes — so an older list can
  never be replayed at a fresh store and enforcement is live
  again without waiting for a re-delivery (S2, gate re-pass S-a);
- the exchange channel: PoolClient hands an exchange response's
  `revocations` field to its sink — the delivery path a withholding
  relay does not sit on (M3). The portal emitting the field is a
  separate follow-up; the node consumes it when present;
- the relay client fires its applied callback exactly when a NEW
  list lands, which is what the service hangs the purge off.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from metasync_node import attestation as A
from metasync_node import revocations as R
from metasync_node.identity import (
    NodeIdentity,
    b64url_encode,
    load_or_create_identity,
)
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta.responder import Responder
from metasync_node.meta.store import MetaStore
from metasync_node.pool import PoolClient
from metasync_node.relay import RelayClient

ISSUER_PRIVATE = Ed25519PrivateKey.generate()
ISSUER_PUBLIC = ISSUER_PRIVATE.public_key()
ISSUER_ID = "pool-1"


def signed_list(seq: int, keys, issuer: str = ISSUER_ID,
                issued_at: int = 1700000000) -> dict:
    sig = ISSUER_PRIVATE.sign(
        R.list_signing_text(issuer, seq, issued_at, keys))
    return {"issuer": issuer, "seq": seq, "issued_at": issued_at,
            "keys": sorted(keys), "sig": b64url_encode(sig)}


def apply(store: R.RevocationStore, seq: int, keys,
          issuer: str = ISSUER_ID):
    return store.apply(signed_list(seq, keys, issuer=issuer),
                       ISSUER_PUBLIC)


def artist_claims(sid: str, name: str = "Cached Artist") -> list[dict]:
    return [
        {"subject": sid, "kind": "artist", "field": "name",
         "value": [name], "source": "file_tags",
         "soulid_version": "kinded_v2"},
        {"subject": sid, "kind": "artist", "field": "genres",
         "value": "jazz", "source": "musicbrainz",
         "soulid_version": "kinded_v2"},
    ]


def envelope_from(identity, claims, attestation=None):
    batch = C.make_batch(identity.public_key_b64url, claims,
                         attestation=attestation)
    return C.sign_batch(identity.signing_private_key, batch)


def forged_attestation(node_key_b64: str, issuer: str = "other") -> dict:
    """A well-shaped attestation naming `issuer`, signed by a key
    nobody trusts: exactly what a revoked origin would mint to
    claim a governing issuer the node holds no list for (N1)."""
    issuer_private = Ed25519PrivateKey.generate()
    now = int(time.time())
    sig = issuer_private.sign(A.attestation_signing_text(
        issuer, node_key_b64, now - 40 * 24 * 3600, now - 60,
        now - 60 + 7 * 24 * 3600))
    return {
        "issuer": issuer,
        "node_key": node_key_b64,
        "bound_at": now - 40 * 24 * 3600,
        "issued_at": now - 60,
        "expires": now - 60 + 7 * 24 * 3600,
        "sig": b64url_encode(sig),
    }


class StoreSemanticsTest(unittest.TestCase):
    """RevocationStore: issuer scoping and durable persistence —
    the high-water marks plus the signed lists, re-verified on
    load."""

    def test_is_revoked_union_and_issuer_scoped(self):
        store = R.RevocationStore()
        apply(store, 1, ["a" * 64])
        self.assertTrue(store.is_revoked("a" * 64))
        self.assertTrue(store.is_revoked("a" * 64, issuer=ISSUER_ID))
        # No list held for another issuer: §10 treats the key as
        # unrevoked FOR THAT ISSUER (foreign-list staleness is a
        # federation precondition, not a v1 mechanism).
        self.assertFalse(store.is_revoked("a" * 64, issuer="other"))
        # Once the other issuer's list is held, IT governs its scope
        # — and it does not name the key.
        apply(store, 1, [], issuer="other")
        self.assertFalse(store.is_revoked("a" * 64, issuer="other"))
        self.assertTrue(store.is_revoked("a" * 64, issuer=ISSUER_ID))
        self.assertTrue(store.is_revoked("a" * 64))  # union still sees it

    def test_high_water_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marks.json"
            first = R.RevocationStore(persist_path=path)
            apply(first, 3, ["b" * 64])
            self.assertTrue(path.exists())
            # A fresh store (restart / network toggle) with no key
            # available yet holds the persisted list as PENDING:
            # the floor survived, but nothing enforces until the
            # list is re-verified under a key.
            second = R.RevocationStore(persist_path=path)
            self.assertFalse(second.is_revoked("b" * 64))
            # The first key-bearing delivery restores the persisted
            # list BEFORE judging its own (S-a): the stale seq-2
            # list is refused against the restored seq-3 list, and
            # the restored list enforces from that moment — no
            # re-delivery of seq 3 was needed.
            self.assertIsNone(apply(second, 2, ["b" * 64]))
            self.assertTrue(second.is_revoked("b" * 64))
            # Re-delivering the held sequence is a duplicate now,
            # not a fresh apply.
            self.assertIsNone(apply(second, 3, ["b" * 64]))
            # And the next sequence still lands.
            self.assertIsNotNone(apply(second, 4, []))
            self.assertFalse(second.is_revoked("b" * 64))

    def test_persisted_list_enforces_after_restart_with_key(self):
        # S-a proper: with the issuer key available at construction
        # (the service wires its provider), the persisted signed
        # list is re-verified and HELD again — enforcement is live
        # immediately after the restart, no re-delivery needed.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marks.json"
            first = R.RevocationStore(persist_path=path)
            apply(first, 3, ["b" * 64])
            second = R.RevocationStore(
                persist_path=path,
                issuer_key_provider=lambda: ISSUER_PUBLIC)
            self.assertTrue(second.is_revoked("b" * 64))
            current = second.current(ISSUER_ID)
            self.assertIsNotNone(current)
            self.assertEqual(current.seq, 3)

    def test_persisted_list_from_another_key_is_dropped(self):
        # The persisted bytes are re-verified, not trusted: a file
        # whose list was signed by a DIFFERENT key restores nothing
        # under the real issuer key — while its mark still floors
        # replays, so dropping it reopens no rollback.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marks.json"
            other_private = Ed25519PrivateKey.generate()
            sig = other_private.sign(R.list_signing_text(
                ISSUER_ID, 3, 1700000000, ["b" * 64]))
            forged = {"issuer": ISSUER_ID, "seq": 3,
                      "issued_at": 1700000000, "keys": ["b" * 64],
                      "sig": b64url_encode(sig)}
            path.write_text(json.dumps(
                {"marks": {ISSUER_ID: 3}, "lists": {ISSUER_ID: forged}}),
                "utf-8")
            store = R.RevocationStore(
                persist_path=path,
                issuer_key_provider=lambda: ISSUER_PUBLIC)
            self.assertFalse(store.is_revoked("b" * 64))
            self.assertIsNone(store.current(ISSUER_ID))
            self.assertIsNone(apply(store, 2, ["b" * 64]))  # mark floors
            self.assertIsNotNone(apply(store, 3, ["b" * 64]))  # genuine
            self.assertTrue(store.is_revoked("b" * 64))

    def test_legacy_marks_only_file_still_floors(self):
        # Files written before lists were persisted are the bare
        # {issuer: seq} marks mapping: they still load as the floor.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marks.json"
            path.write_text(json.dumps({ISSUER_ID: 5}), "utf-8")
            store = R.RevocationStore(persist_path=path)
            self.assertIsNone(apply(store, 4, []))
            self.assertIsNotNone(apply(store, 5, []))

    def test_corrupt_marks_file_reads_as_none_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marks.json"
            path.write_text("{not json", "utf-8")
            store = R.RevocationStore(persist_path=path)
            self.assertIsNotNone(apply(store, 1, ["c" * 64]))


class AdmissionTest(unittest.TestCase):
    """MetaStore: the revocation check at admission, on reads, and
    in the purge (§9/§10)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.revocations = R.RevocationStore()
        self.store = MetaStore(self.tmp.name,
                               revocations=self.revocations)
        self.origin = load_or_create_identity(self.tmp.name + "-origin")
        self.sid = S.generate_soul_id("artist", "Cached Artist")
        self.envelope = envelope_from(
            self.origin, artist_claims(self.sid))

    def test_unrevoked_batch_is_admitted(self):
        self.store.put_cached_batch(self.envelope)
        self.assertEqual(self.store.counts()["cached_batches"], 1)

    def test_revoked_origin_refused_at_admission(self):
        apply(self.revocations, 1, [self.origin.node_id])
        with self.assertRaises(C.ClaimError):
            self.store.put_cached_batch(self.envelope)
        self.assertEqual(self.store.counts()["cached_batches"], 0)

    def test_other_key_revoked_does_not_block(self):
        apply(self.revocations, 1, ["e" * 64])
        self.store.put_cached_batch(self.envelope)
        self.assertEqual(self.store.counts()["cached_batches"], 1)

    def test_forged_attestation_issuer_does_not_escape_revocation(self):
        # Gate re-pass N1, flipped: this test previously asserted
        # the forged-issuer batch WAS admitted, locking the escape
        # in. The header attestation's issuer is never verified at
        # receipt, so it must not select the governing list: a
        # revoked origin naming a made-up issuer ("other", signed
        # by a key nobody trusts) is refused exactly like a batch
        # with no attestation at all — admission is the union over
        # held lists, the same set purge_revoked drops by.
        envelope = envelope_from(
            self.origin, artist_claims(self.sid),
            attestation=forged_attestation(self.origin.public_key_b64url))
        apply(self.revocations, 1, [self.origin.node_id])
        with self.assertRaises(C.ClaimError):
            self.store.put_cached_batch(envelope)
        self.assertEqual(self.store.counts()["cached_batches"], 0)

    def test_forged_issuer_on_unrevoked_origin_is_admitted(self):
        # The union refuses only keys a held list actually names:
        # an unrevoked origin carrying the same forged-issuer
        # attestation is admitted (its zero support is the merge's
        # business, not admission's).
        envelope = envelope_from(
            self.origin, artist_claims(self.sid),
            attestation=forged_attestation(self.origin.public_key_b64url))
        apply(self.revocations, 1, ["e" * 64])
        self.store.put_cached_batch(envelope)
        self.assertEqual(self.store.counts()["cached_batches"], 1)

    def test_forged_issuer_batch_is_filtered_on_read(self):
        # The read filter is the same union check: a batch cached
        # BEFORE its origin was revoked, carrying the forged
        # issuer, is never re-served once any held list names the
        # origin — the re-served resurrection N1 was about.
        envelope = envelope_from(
            self.origin, artist_claims(self.sid),
            attestation=forged_attestation(self.origin.public_key_b64url))
        self.store.put_cached_batch(envelope)
        self.assertEqual(
            len(self.store.cached_batches_for_subjects([self.sid])), 1)
        apply(self.revocations, 1, [self.origin.node_id])
        self.assertEqual(
            self.store.cached_batches_for_subjects([self.sid]), [])
        self.assertEqual(
            self.store.cached_batches_for_subjects(
                [self.sid], reservable_only=False), [])

    def test_cached_before_revocation_is_filtered_and_purged(self):
        self.store.put_cached_batch(self.envelope)
        self.assertEqual(
            len(self.store.cached_batches_for_subjects([self.sid])), 1)
        apply(self.revocations, 1, [self.origin.node_id])
        # The read filter: never re-served again, even pre-purge.
        self.assertEqual(
            self.store.cached_batches_for_subjects([self.sid]), [])
        self.assertEqual(
            self.store.cached_batches_for_subjects(
                [self.sid], reservable_only=False), [])
        # The purge: rows actually die (batches, claims, provenance).
        self.assertEqual(
            self.store.purge_revoked({self.origin.node_id}), 1)
        counts = self.store.counts()
        self.assertEqual(counts["cached_batches"], 0)
        self.assertEqual(counts["cached_claims"], 0)
        self.assertEqual(
            self.store.purge_revoked({self.origin.node_id}), 0)

    def test_responder_never_reserves_a_revoked_origin(self):
        self.store.put_cached_batch(self.envelope)
        apply(self.revocations, 1, [self.origin.node_id])
        me = load_or_create_identity(self.tmp.name + "-me")
        responder = Responder(self.store, me,
                              mode_provider=lambda: "full")
        out = responder.handle({"kind": "artist", "mode": "exact",
                                "subjects": [self.sid]})
        self.assertEqual(out, [])

    def test_unwired_store_enforces_nothing(self):
        # A MetaStore nobody wired holds no lists: admission behaves
        # exactly as before the gate existed.
        bare = MetaStore(self.tmp.name + "-bare")
        bare.put_cached_batch(self.envelope)
        self.assertEqual(bare.counts()["cached_batches"], 1)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def request(self, method, url, data=None, headers=None, timeout=None):
        return self._response


class ExchangeCarriageTest(unittest.TestCase):
    """PoolClient: the exchange response's revocations field reaches
    the sink (M3); its absence or a broken sink changes nothing."""

    def setUp(self):
        self.identity = NodeIdentity(key="ab" * 32)

    def _client(self, body: dict, sink):
        return PoolClient(
            {"id": "pool", "url": "https://pool.example", "api_key": "k"},
            identity=self.identity, auth_state={},
            session=_FakeSession(_FakeResponse(200, body)),
            revocation_sink=sink)

    def _body(self, **extra) -> dict:
        body = {"access_token": "tok-1", "expires_in": 3600,
                "node_id": self.identity.node_id,
                "scopes": ["meta:read"], "binding_status": "bound"}
        body.update(extra)
        return body

    def test_revocations_field_reaches_the_sink(self):
        seen = []
        lst = signed_list(7, ["d" * 64])
        client = self._client(self._body(revocations=lst), seen.append)
        client.exchange()
        self.assertEqual(seen, [lst])

    def test_no_field_no_sink_call(self):
        seen = []
        client = self._client(self._body(), seen.append)
        client.exchange()
        self.assertEqual(seen, [])

    def test_raising_sink_never_breaks_the_exchange(self):
        def boom(_payload):
            raise RuntimeError("sink exploded")

        client = self._client(
            self._body(revocations=signed_list(1, [])), boom)
        client.exchange()  # must not raise
        self.assertTrue(client.auth_state.get("access_token"))


class RelayClientCallbackTest(unittest.TestCase):
    """The applied callback fires exactly when a NEW list lands —
    the service's cache purge hangs off it, so a stale or refused
    list must never trigger it."""

    def test_callback_fires_on_new_lists_only(self):
        fired = []
        client = RelayClient(
            NodeIdentity(key="ab" * 32),
            url_provider=lambda: "",
            mode_provider=lambda: "own_library",
            token_provider=lambda: "tok",
            issuer_key_provider=lambda: ISSUER_PUBLIC,
            revocations_applied=fired.append,
        )
        client._apply_revocations(signed_list(1, ["f" * 64]))
        client._apply_revocations(signed_list(1, ["f" * 64]))  # stale
        client._apply_revocations({"issuer": ISSUER_ID})       # malformed
        client._apply_revocations(signed_list(2, []))
        self.assertEqual([lst.seq for lst in fired], [1, 2])
        # The client's own copy is the same store the callback saw.
        self.assertEqual(client.revocations()["seq"], 2)


if __name__ == "__main__":
    unittest.main()

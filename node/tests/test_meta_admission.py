"""Admission tests: the §9 four checks at the querier's door.

Each check failing in isolation drops (or zero-supports) the batch
for exactly its own reason; a clean batch lands in the answer cache
with its admission record written beside it.
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
from metasync_node import attestation as A
from metasync_node.identity import NodeIdentity, b64url_encode
from metasync_node.meta import admission as ADM
from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S
from metasync_node.meta.store import MetaStore, node_id_for_key
from metasync_node.revocations import RevocationStore, list_signing_text

DAY = 24 * 3600
NOW = int(time.time())
SUBJECT = S.generate_soul_id("track", "Radiohead", "Airbag")
ISSUER = "pool-1"
ISSUER_PRIVATE = Ed25519PrivateKey.generate()
ISSUER_PUBLIC = ISSUER_PRIVATE.public_key()


def issue(node_key_b64, bound_at, issued_at, expires, issuer=ISSUER,
          signing_key=ISSUER_PRIVATE):
    sig = signing_key.sign(A.attestation_signing_text(
        issuer, node_key_b64, bound_at, issued_at, expires))
    return {"issuer": issuer, "node_key": node_key_b64,
            "bound_at": bound_at, "issued_at": issued_at,
            "expires": expires, "sig": b64url_encode(sig)}


def track_claims(deezer_id="12345"):
    return [
        {"subject": SUBJECT, "kind": "track", "field": "name",
         "value": ["Radiohead", "Airbag"], "source": "file_tags",
         "soulid_version": C.SOULID_VERSION},
        {"subject": SUBJECT, "kind": "track", "field": "deezer_id",
         "value": deezer_id, "source": "deezer",
         "soulid_version": C.SOULID_VERSION},
    ]


def envelope(identity, claims=None, attestation=None, asserted_at=NOW - 60):
    batch = C.make_batch(identity.public_key_b64url,
                         claims if claims is not None else track_claims(),
                         attestation=attestation,
                         asserted_at=asserted_at)
    return C.sign_batch(identity.signing_private_key, batch)


def standing_attestation(identity):
    """Bound 40 days ago, renewed yesterday: asserted_at today is
    inside the window and the attested age clears 30 days."""
    return issue(identity.public_key_b64url,
                 bound_at=NOW - 40 * DAY, issued_at=NOW - DAY,
                 expires=NOW + 6 * DAY)


class AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MetaStore(self.tmp.name)
        self.origin = NodeIdentity(key="aa" * 32)

    def admit(self, env, **kwargs):
        kwargs.setdefault("now", float(NOW))
        # The node is configured with its pool's issuer key + id:
        # the trusted pair standing is verified against (MF-1).
        # Tests that want the unconfigured node pass both as None
        # explicitly.
        kwargs.setdefault("issuer_key", ISSUER_PUBLIC)
        kwargs.setdefault("own_issuer", ISSUER)
        return ADM.admit_envelope(self.store, env, **kwargs)

    def cached(self):
        return self.store.counts()["cached_batches"]


class MatrixTest(AdmissionCase):
    def test_clean_batch_without_attestation_admits_zero_support(self):
        out = self.admit(envelope(self.origin))
        self.assertTrue(out.admitted)
        self.assertFalse(out.forwarded)
        self.assertFalse(out.standing)
        self.assertEqual(out.reason, "admitted_zero_support")
        self.assertEqual(self.cached(), 1)
        record = self.store.admission_for([out.batch_id])[out.batch_id]
        self.assertFalse(record["standing"])
        self.assertIsNone(record["issuer"])

    def test_standing_attestation_admits_with_standing(self):
        out = self.admit(envelope(
            self.origin, attestation=standing_attestation(self.origin)))
        self.assertTrue(out.admitted)
        self.assertTrue(out.standing)
        record = self.store.admission_for([out.batch_id])[out.batch_id]
        self.assertTrue(record["standing"])
        self.assertEqual(record["issuer"], ISSUER)
        self.assertEqual(record["bound_at"], NOW - 40 * DAY)

    def test_young_binding_admits_zero_support(self):
        att = issue(self.origin.public_key_b64url,
                    bound_at=NOW - 10 * DAY, issued_at=NOW - DAY,
                    expires=NOW + 6 * DAY)
        out = self.admit(envelope(self.origin, attestation=att))
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)

    def test_asserted_outside_window_admits_zero_support(self):
        # Standing-age binding, but the batch was asserted BEFORE
        # this attestation was issued: the window check (c) fails,
        # which is zero support, not a drop.
        att = issue(self.origin.public_key_b64url,
                    bound_at=NOW - 40 * DAY, issued_at=NOW - DAY,
                    expires=NOW + 6 * DAY)
        out = self.admit(envelope(self.origin, attestation=att,
                                  asserted_at=NOW - 2 * DAY))
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)

    def test_bad_batch_signature_drops(self):
        env = envelope(self.origin)
        env["sig"] = b64url_encode(b"\x01" * 64)
        out = self.admit(env)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "batch_invalid")
        self.assertEqual(self.cached(), 0)

    def test_tampered_claim_drops(self):
        env = envelope(self.origin)
        env["batch"]["claims"][1]["value"] = "99999"
        out = self.admit(env)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "batch_invalid")
        self.assertEqual(self.cached(), 0)

    def test_attestation_naming_another_key_drops(self):
        other = NodeIdentity(key="bb" * 32)
        att = standing_attestation(other)
        out = self.admit(envelope(self.origin, attestation=att))
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "attestation_names_other_key")
        self.assertEqual(self.cached(), 0)

    def test_malformed_attestation_drops(self):
        # Honest minting refuses an unchecked attestation, so this
        # batch is forged by hand: a valid batch whose header
        # attestation is swapped for garbage and re-signed by the
        # origin — check (a) passes, admission's (b) shape gate is
        # what must drop it.
        env = envelope(self.origin)
        env["batch"]["header"]["attestation"] = {
            "issuer": ISSUER, "node_key": "not-a-key",
            "bound_at": 1, "issued_at": 2, "expires": 3, "sig": "x"}
        env = C.sign_batch(self.origin.signing_private_key, env["batch"])
        out = self.admit(env)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "attestation_malformed")
        self.assertEqual(self.cached(), 0)

    def test_malformed_envelope_drops(self):
        for junk in (None, "nope", {}, {"batch": {}}, {"forward": {}}):
            out = self.admit(junk)
            self.assertFalse(out.admitted, junk)
            self.assertEqual(out.reason, "malformed_envelope")

    def test_duplicate_admission_is_idempotent(self):
        env = envelope(self.origin,
                       attestation=standing_attestation(self.origin))
        first = self.admit(env)
        second = self.admit(env)
        self.assertTrue(first.admitted)
        self.assertTrue(second.admitted)
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(self.cached(), 1)


class RevocationTest(AdmissionCase):
    def setUp(self):
        super().setUp()
        self.revocations = RevocationStore()
        self.store = MetaStore(self.tmp.name,
                               revocations=self.revocations)

    def _revoke(self, identity):
        node_id = node_id_for_key(identity.public_key_b64url)
        keys = [node_id]
        sig = ISSUER_PRIVATE.sign(
            list_signing_text(ISSUER, 1, NOW, keys))
        applied = self.revocations.apply(
            {"issuer": ISSUER, "seq": 1, "issued_at": NOW,
             "keys": keys, "sig": b64url_encode(sig)}, ISSUER_PUBLIC)
        self.assertIsNotNone(applied)

    def test_revoked_origin_drops(self):
        self._revoke(self.origin)
        out = self.admit(envelope(
            self.origin, attestation=standing_attestation(self.origin)))
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "origin_revoked")
        self.assertEqual(self.cached(), 0)

    def test_unrelated_revocation_does_not_drop(self):
        self._revoke(NodeIdentity(key="cc" * 32))
        out = self.admit(envelope(self.origin))
        self.assertTrue(out.admitted)


class ForwardingTest(AdmissionCase):
    def test_forwarding_envelope_judged_by_its_origin(self):
        inner = envelope(self.origin,
                         attestation=standing_attestation(self.origin))
        forwarder = NodeIdentity(key="dd" * 32)
        fwd = {"origin_batch_id": C.batch_digest(inner["batch"]),
               "origin_node": self.origin.public_key_b64url,
               "forwarded_at": "2026-10-27T00:00:00+00:00"}
        fwd["sig"] = forwarder.sign(C.canonical_bytes(fwd))
        out = self.admit({"forward": fwd, "envelope": inner})
        self.assertTrue(out.admitted)
        self.assertTrue(out.forwarded)
        self.assertEqual(out.origin_node, self.origin.public_key_b64url)
        self.assertTrue(out.standing)

    def test_forward_key_without_the_signed_shape_drops(self):
        # Gate fold 2: unwrap and classification disagreed about
        # what a forwarding envelope is — a dict-valued `forward`
        # with the signed fields left out unwrapped to the inner
        # batch while classifying as NOT forwarded, so one node
        # replaying cached batches under this shape made every
        # origin look like a direct answerer. The honest inner
        # batch below is fully valid and standing; under the old
        # unwrap it admitted as a direct answer. Now any `forward`
        # key short of the full shape is malformed, however valid
        # what it wraps is.
        inner = envelope(self.origin,
                         attestation=standing_attestation(self.origin))
        for junk_forward in (
                {},
                {"origin_batch_id": C.batch_digest(inner["batch"])},
                {"origin_node": self.origin.public_key_b64url,
                 "forwarded_at": "2026-10-27T00:00:00+00:00",
                 "sig": "unsigned"},
                "not-even-a-dict"):
            out = self.admit({"forward": junk_forward, "envelope": inner})
            self.assertFalse(out.admitted, junk_forward)
            self.assertEqual(out.reason, "malformed_envelope")
        self.assertEqual(self.cached(), 0)

    def test_forward_key_beside_an_own_batch_drops(self):
        # The variant: a valid batch of the SENDER's at the top
        # level (which is what a stamp check reads) plus a
        # different origin's batch under `envelope` (which is
        # what the old unwrap judged). The envelope is malformed
        # as a whole — neither batch is admitted.
        inner = envelope(self.origin,
                         attestation=standing_attestation(self.origin))
        sender = NodeIdentity(key="dd" * 32)
        own = envelope(sender,
                       attestation=standing_attestation(sender))
        forged = dict(own)
        forged["forward"] = {}
        forged["envelope"] = inner
        out = self.admit(forged)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, "malformed_envelope")
        self.assertEqual(self.cached(), 0)


class OwnPoolForgeryTest(AdmissionCase):
    def test_forged_own_pool_attestation_earns_zero_support(self):
        # MF-1: a garbage signature under the trusted issuer's own
        # name is zero support, exactly like a missing attestation
        # — the batch's claims are genuinely the origin's (check
        # (a) passed), it is only the pool's vouching that is
        # absent, so the batch displays and never counts.
        forger = Ed25519PrivateKey.generate()
        att = issue(self.origin.public_key_b64url,
                    bound_at=NOW - 40 * DAY, issued_at=NOW - DAY,
                    expires=NOW + 6 * DAY, signing_key=forger)
        out = self.admit(envelope(self.origin, attestation=att),
                         issuer_key=ISSUER_PUBLIC, own_issuer=ISSUER)
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)
        self.assertEqual(out.reason, "admitted_zero_support")

    def test_genuine_own_pool_attestation_passes(self):
        out = self.admit(
            envelope(self.origin,
                     attestation=standing_attestation(self.origin)),
            issuer_key=ISSUER_PUBLIC, own_issuer=ISSUER)
        self.assertTrue(out.admitted)
        self.assertTrue(out.standing)

    def test_foreign_issuer_earns_zero_support(self):
        # MF-1: a genuinely-signed attestation from a pool whose
        # key this node does not hold. The signature is real — for
        # pool-2 — but this node holds no key for pool-2, so the
        # vouching cannot be verified here and standing is zero.
        # (The pre-fix door judged standing on shape + window
        # alone and granted it: this test pins the hole shut.)
        foreign_private = Ed25519PrivateKey.generate()
        att = issue(self.origin.public_key_b64url,
                    bound_at=NOW - 40 * DAY, issued_at=NOW - DAY,
                    expires=NOW + 6 * DAY, issuer="pool-2",
                    signing_key=foreign_private)
        out = self.admit(envelope(self.origin, attestation=att),
                         issuer_key=ISSUER_PUBLIC, own_issuer=ISSUER)
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)


class FabricatedAttestationTest(AdmissionCase):
    """The MF-1 exploit, end to end at the door: a FRESH key
    embeds an attestation-shaped object claiming a binding a year
    old, signed with random bytes. Under the shape-only door this
    admitted WITH standing; now it is zero support however it is
    dressed."""

    def _fabricated(self, identity, issuer=ISSUER):
        return {"issuer": issuer,
                "node_key": identity.public_key_b64url,
                "bound_at": NOW - 365 * DAY, "issued_at": NOW - DAY,
                "expires": NOW + 6 * DAY,
                "sig": b64url_encode(b"\x07" * 64)}

    def test_fabricated_attestation_earns_no_standing(self):
        fresh = NodeIdentity(key="ff" * 32)
        env = envelope(fresh, attestation=self._fabricated(fresh))
        out = self.admit(env)  # node configured with the real key
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)
        record = self.store.admission_for([out.batch_id])[out.batch_id]
        self.assertFalse(record["standing"])

    def test_fabricated_attestation_on_an_unconfigured_node(self):
        fresh = NodeIdentity(key="ff" * 32)
        env = envelope(fresh, attestation=self._fabricated(fresh))
        out = self.admit(env, issuer_key=None, own_issuer=None)
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)

    def test_node_holding_no_attestation_grants_no_standing(self):
        # The trusted issuer is unknown (own_issuer None — this
        # node holds no attestation of its own): even a GENUINE
        # pool signature cannot be matched to a trusted issuer,
        # so standing is zero until the node's own exchange names
        # one.
        env = envelope(self.origin,
                       attestation=standing_attestation(self.origin))
        out = self.admit(env, own_issuer=None)
        self.assertTrue(out.admitted)
        self.assertFalse(out.standing)


class ReadmissionUpgradeTest(AdmissionCase):
    """R1-SF-1: standing is not frozen at first admission."""

    def test_readmission_upgrades_once_verifiable(self):
        env = envelope(self.origin,
                       attestation=standing_attestation(self.origin))
        # First sight: this node cannot verify anything yet.
        first = self.admit(env, issuer_key=None, own_issuer=None)
        self.assertTrue(first.admitted)
        self.assertFalse(first.standing)
        # The same batch arrives again after configuration: the
        # stored verdict upgrades.
        second = self.admit(env)
        self.assertTrue(second.admitted)
        self.assertTrue(second.standing)
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(self.cached(), 1)
        record = self.store.admission_for([second.batch_id])[
            second.batch_id]
        self.assertTrue(record["standing"])
        self.assertEqual(record["issuer"], ISSUER)

    def test_readmission_never_downgrades(self):
        env = envelope(self.origin,
                       attestation=standing_attestation(self.origin))
        first = self.admit(env)
        self.assertTrue(first.standing)
        second = self.admit(env, issuer_key=None, own_issuer=None)
        self.assertTrue(second.admitted)
        record = self.store.admission_for([first.batch_id])[
            first.batch_id]
        self.assertTrue(record["standing"])

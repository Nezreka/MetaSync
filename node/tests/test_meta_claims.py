"""Claims + signed batches (DESIGN-METADATA §6)."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from metasync_node.meta import claims as C
from metasync_node.meta import soulid as S

PUB = None
PRIV = None


def setUpModule():
    global PRIV, PUB
    PRIV = Ed25519PrivateKey.generate()
    PUB = C.b64url_encode(PRIV.public_key().public_bytes_raw())


def name_claim(kind="track", parts=("Radiohead", "Karma Police")):
    # A name claim's subject is the SoulID its tuple recomputes to (§6).
    try:
        subject = S.generate_soul_id(kind, *parts)
    except ValueError:
        subject = "soul_0123456789abcdef"  # validate_claim raises first
    return {"subject": subject, "kind": kind,
            "field": "name", "value": list(parts), "source": "deezer",
            "soulid_version": "kinded_v2"}


def id_claim(**over):
    c = {"subject": "soul_0123456789abcdef", "kind": "track",
         "field": "isrc", "value": "GBAYE9700182", "source": "deezer",
         "soulid_version": "kinded_v2"}
    c.update(over)
    return c


class TestCanonical(unittest.TestCase):
    def test_key_order_irrelevant(self):
        a = {"b": 1, "a": {"y": [1, 2], "x": "s"}}
        b = {"a": {"x": "s", "y": [1, 2]}, "b": 1}
        self.assertEqual(C.canonical_bytes(a), C.canonical_bytes(b))

    def test_float_forbidden(self):
        with self.assertRaises(C.ClaimError):
            C.canonical_bytes({"duration": 1.5})

    def test_unicode_not_escaped(self):
        self.assertIn("周".encode(), C.canonical_bytes({"n": "周"}))


class TestValidateClaim(unittest.TestCase):
    def test_name_claim_ok(self):
        C.validate_claim(name_claim())

    def test_name_claim_wrong_arity(self):
        with self.assertRaises(C.ClaimError):
            C.validate_claim(name_claim(parts=("a", "b", "c")))

    def test_unknown_field_and_source(self):
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(field="bitrate"))
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(source="a_friend"))

    def test_wrong_soulid_version(self):
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(soulid_version="v1"))

    def test_album_track_shape(self):
        c = id_claim(field="album_track", kind="album",
                     value={"track": "soul_0123456789abcdef", "disc": 1,
                            "position": 3, "variant": "deezer:123"})
        C.validate_claim(c)
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(
                field="album_track", kind="album",
                value={"track": "soul_0123456789abcdef"}))

    def test_album_track_value_types(self):
        base = {"track": "soul_0123456789abcdef", "disc": 1,
                "position": 3, "variant": "deezer:123"}
        for bad in (dict(base, disc="1"), dict(base, position=True),
                    dict(base, track="not-a-soulid"),
                    dict(base, variant=["deezer:123"]),
                    dict(base, extra=1)):
            with self.assertRaises(C.ClaimError):
                C.validate_claim(id_claim(field="album_track", kind="album",
                                          value=bad))

    def test_name_claim_must_recompute_to_subject(self):
        c = name_claim()
        c["subject"] = "soul_0123456789abcdef"
        with self.assertRaises(C.ClaimError):
            C.validate_claim(c)

    def test_subject_must_be_a_real_soulid(self):
        for bad in ("soul_", "soul_unnamed_123", "soul_zzzzzzzzzzzzzzzz"):
            with self.assertRaises(C.ClaimError):
                C.validate_claim(id_claim(subject=bad))

    def test_id_values_must_be_strings(self):
        # an int/list ID passes canonical JSON but crashes the merge's
        # set sorting — refused here instead (gate finding, Oct 26)
        for field, value in (("deezer_id", 123), ("deezer_id", ["x"]),
                             ("isrc", 42), ("duration_ms", "abc"),
                             ("year", "1997"), ("year", True),
                             ("genres", ["rock"])):
            with self.assertRaises(C.ClaimError):
                C.validate_claim(id_claim(field=field, value=value))
        C.validate_claim(id_claim(field="duration_ms", value=213000))
        C.validate_claim(id_claim(field="year", value=1997))
        C.validate_claim(id_claim(field="genres", value="rock"))

    def test_user_edit_reserved_and_hydrabase_source(self):
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(source="user_edit"))
        C.validate_claim(id_claim(source="hydrabase"))

    def test_canonical_int_bound(self):
        with self.assertRaises(C.ClaimError):
            C.canonical_bytes({"x": 2**60})


class TestBatchSignVerify(unittest.TestCase):
    def test_roundtrip(self):
        batch = C.make_batch(PUB, [name_claim(), id_claim()])
        env = C.sign_batch(PRIV, batch)
        out = C.verify_batch(PUB, env)
        self.assertEqual(out["header"]["claim_count"], 2)

    def test_tampered_claim_rejected(self):
        batch = C.make_batch(PUB, [id_claim()])
        env = C.sign_batch(PRIV, batch)
        env["batch"]["claims"][0]["value"] = "USRC10000001"
        with self.assertRaises(C.ClaimError):
            C.verify_batch(PUB, env)

    def test_wrong_key_rejected(self):
        batch = C.make_batch(PUB, [id_claim()])
        env = C.sign_batch(PRIV, batch)
        other = Ed25519PrivateKey.generate()
        other_pub = C.b64url_encode(other.public_key().public_bytes_raw())
        with self.assertRaises(C.ClaimError):
            C.verify_batch(other_pub, env)

    def test_digest_is_identity(self):
        b1 = C.make_batch(PUB, [name_claim(), id_claim()])
        b2 = C.make_batch(PUB, [id_claim(), name_claim()])
        self.assertNotEqual(C.batch_digest(b1), C.batch_digest(b2))

    def test_identical_claim_sets_are_still_distinct_batches(self):
        # The header nonce is what lets claim X be sent, retracted,
        # and sent again: each sending is its own batch.
        b1 = C.make_batch(PUB, [id_claim()])
        b2 = C.make_batch(PUB, [id_claim()])
        self.assertNotEqual(b1["header"]["nonce"], b2["header"]["nonce"])
        self.assertNotEqual(C.batch_digest(b1), C.batch_digest(b2))
        out = C.verify_batch(PUB, C.sign_batch(PRIV, b1))
        self.assertEqual(out["header"]["nonce"], b1["header"]["nonce"])

    def test_retraction_must_be_a_full_claim(self):
        with self.assertRaises(C.ClaimError):
            C.make_batch(PUB, [id_claim()],
                         retractions=[{"subject": "soul_0123456789abcdef"}])
        ok = C.make_batch(PUB, [name_claim()], retractions=[id_claim()])
        C.verify_batch(PUB, C.sign_batch(PRIV, ok))

    def test_nul_and_surrogate_values_refused(self):
        with self.assertRaises(C.ClaimError):
            C.make_batch(PUB, [id_claim(field="label", value="a\x00b")])
        with self.assertRaises(C.ClaimError):
            C.make_batch(PUB, [id_claim(field="label", value="a\ud800b")])

    def test_subject_with_trailing_newline_refused(self):
        with self.assertRaises(C.ClaimError):
            C.validate_claim(id_claim(
                subject="soul_0123456789abcdef\n"))

    def test_duplicate_name_claim_rejected(self):
        with self.assertRaises(C.ClaimError):
            C.make_batch(PUB, [name_claim(), name_claim()])

    def test_verify_rejects_wrong_version_and_bad_retractions(self):
        batch = C.make_batch(PUB, [id_claim()])
        env = C.sign_batch(PRIV, batch)
        env["batch"]["header"]["version"] = 99
        with self.assertRaises(C.ClaimError):
            C.verify_batch(PUB, env)
        batch = C.make_batch(PUB, [id_claim()])
        env = C.sign_batch(PRIV, batch)
        env["batch"]["retractions"] = ["not-an-object"]
        # the tamper also breaks the signature; either refusal is a
        # ClaimError, never a crash
        with self.assertRaises(C.ClaimError):
            C.verify_batch(PUB, env)
        with self.assertRaises(C.ClaimError):
            C.make_batch(PUB, [id_claim()], retractions=["nope"])

    def test_verify_malformed_key_is_claim_error(self):
        batch = C.make_batch(PUB, [id_claim()])
        env = C.sign_batch(PRIV, batch)
        with self.assertRaises(C.ClaimError):
            C.verify_batch("!!!not-base64!!!", env)

    def test_asserted_at_is_minted_signed_and_required(self):
        # MF-3 / §6: the header carries the node's signed assertion
        # time — cache ages and §9's attestation window/standing all
        # run from it, so it must be present, integral, and covered
        # by the signature.
        batch = C.make_batch(PUB, [id_claim()], asserted_at=1_700_000_000)
        self.assertEqual(batch["header"]["asserted_at"], 1_700_000_000)
        env = C.sign_batch(PRIV, batch)
        out = C.verify_batch(PUB, env)
        self.assertEqual(out["header"]["asserted_at"], 1_700_000_000)
        # Tampering with it breaks the signature (it is signed data,
        # not a hint the receiver may repair).
        env["batch"]["header"]["asserted_at"] = 1_800_000_000
        with self.assertRaises(C.ClaimError):
            C.verify_batch(PUB, env)
        # A batch with no asserted_at at all is refused even when
        # re-signed by the key holder: the shape itself is invalid.
        batch = C.make_batch(PUB, [id_claim()], asserted_at=1_700_000_000)
        del batch["header"]["asserted_at"]
        with self.assertRaises(C.ClaimError):
            C.verify_batch(PUB, C.sign_batch(PRIV, batch))
        # Non-integer assertion times are refused at minting.
        for bad in (1_700_000_000.5, "1700000000", -1, True):
            with self.assertRaises(C.ClaimError, msg=repr(bad)):
                C.make_batch(PUB, [id_claim()], asserted_at=bad)
        # The default is the node's clock now.
        before = int(time.time())
        batch = C.make_batch(PUB, [id_claim()])
        after = int(time.time())
        self.assertTrue(before <= batch["header"]["asserted_at"] <= after)


if __name__ == "__main__":
    unittest.main()

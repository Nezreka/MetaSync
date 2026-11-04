"""Binding attestation tests (DESIGN-METADATA.md §9, §13 step 5).

The carriage half of the attestation lifecycle, node side: the pool
signs {issuer, node_key, bound_at, issued_at, expires} at the token
exchange; the node verifies it (pool signature + it must name THIS
node's key), stores it, and embeds it in every batch header it
mints — the store's materialized batches and the responder's live
answers alike. A renewal re-mints the stored batches with the new
attestation. Issuance itself is the portal's (a separate PR); here
a fake issuer key signs exactly the canonical bytes attestation.py
pins.

The shape carries two times on purpose (§9): bound_at is the
binding moment (standing only) and issued_at..expires is the 7-day
validity window. The M1 regression tests below pin that a key bound
30+ days ago can hold a live 7-day attestation at all — under the
old window-from-bound_at shape it could not, and no key could ever
have standing.
"""
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
from metasync_node.identity import NodeIdentity, b64url_decode, b64url_encode
from metasync_node.meta import builder as B
from metasync_node.meta import claims as C
from metasync_node.meta.responder import Responder
from metasync_node.meta.store import MetaStore
from metasync_node.pool import PoolClient

from tests.test_meta_builder import (
    ALBUM_ID,
    ARTIST_ID,
    FakeSoulSync,
    full_pages,
)

ISSUER_PRIVATE = Ed25519PrivateKey.generate()
ISSUER_PUBLIC = ISSUER_PRIVATE.public_key()
ISSUER_ID = "pool-1"
# Windows are relative to now: the store's mint paths embed only an
# attestation whose window covers the moment of minting (SF-3), so a
# fixed historical window would read as expired everywhere it matters.
BOUND_AT = int(time.time()) - 24 * 3600  # bound a day ago
ISSUED_AT = BOUND_AT  # first attestation, issued at binding time
EXPIRES = ISSUED_AT + 7 * 24 * 3600  # §9: a 7-day lifetime from issuance


def issue(node_key_b64: str, bound_at: int = BOUND_AT,
          issued_at: int = ISSUED_AT, expires: int = EXPIRES,
          issuer: str = ISSUER_ID,
          signing_key: Ed25519PrivateKey = ISSUER_PRIVATE) -> dict:
    """An attestation exactly as the portal will issue it: the sig
    is over attestation_signing_text's canonical bytes."""
    sig = signing_key.sign(
        A.attestation_signing_text(issuer, node_key_b64, bound_at,
                                   issued_at, expires))
    return {"issuer": issuer, "node_key": node_key_b64,
            "bound_at": bound_at, "issued_at": issued_at,
            "expires": expires, "sig": b64url_encode(sig)}


class CanonicalFormTest(unittest.TestCase):
    """The signed bytes are pinned literally: the portal-side PR
    must reproduce them byte for byte or nothing verifies."""

    def test_signing_text_exact_bytes(self):
        text = A.attestation_signing_text(
            "pool-1", "NODEKEY", 1700000000, 1700000100, 1700604800)
        self.assertEqual(
            text,
            b"metasync-attestation\npool-1\nNODEKEY\n1700000000\n"
            b"1700000100\n1700604800\n")

    def test_verify_round_trip(self):
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url)
        out = A.verify_attestation(att, ISSUER_PUBLIC, identity.public_key_b64url)
        self.assertEqual(out, att)

    def test_tampered_window_dropped(self):
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url)
        att["expires"] += 3600  # the signature no longer covers it
        self.assertIsNone(
            A.verify_attestation(att, ISSUER_PUBLIC, identity.public_key_b64url))

    def test_tampered_signature_dropped(self):
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url)
        raw = bytearray(b64url_decode(att["sig"]))
        raw[0] ^= 1
        att["sig"] = b64url_encode(bytes(raw))
        self.assertIsNone(
            A.verify_attestation(att, ISSUER_PUBLIC, identity.public_key_b64url))

    def test_foreign_attestation_dropped(self):
        # Perfectly signed — for somebody else's key. Verification
        # against THIS node's key must refuse it.
        identity = NodeIdentity(key="ab" * 32)
        other = NodeIdentity(key="cd" * 32)
        att = issue(other.public_key_b64url)
        self.assertIsNone(
            A.verify_attestation(att, ISSUER_PUBLIC, identity.public_key_b64url))

    def test_wrong_issuer_dropped(self):
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url,
                    signing_key=Ed25519PrivateKey.generate())
        self.assertIsNone(
            A.verify_attestation(att, ISSUER_PUBLIC, identity.public_key_b64url))

    def test_no_issuer_key_drops_everything(self):
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url)
        self.assertIsNone(
            A.verify_attestation(att, None, identity.public_key_b64url))

    def test_malformed_shapes_dropped(self):
        identity = NodeIdentity(key="ab" * 32)
        good = issue(identity.public_key_b64url)
        for bad in (
            None, "attestation", [],
            {k: v for k, v in good.items() if k != "sig"},
            {k: v for k, v in good.items() if k != "issued_at"},
            {k: v for k, v in good.items() if k != "issuer"},
            {**good, "issuer": ""},
            {**good, "bound_at": float(BOUND_AT)},
            {**good, "bound_at": True},
            {**good, "expires": ISSUED_AT},          # empty window
            {**good, "expires": ISSUED_AT + 30 * 24 * 3600},  # too wide
            {**good, "bound_at": ISSUED_AT + 1},     # bound after issue
            {**good, "node_key": "not!base64"},
            {**good, "sig": b64url_encode(b"\x00" * 32)},
        ):
            self.assertIsNone(
                A.verify_attestation(bad, ISSUER_PUBLIC,
                                     identity.public_key_b64url),
                msg=f"accepted malformed attestation: {bad!r}")

    def test_tampered_issuer_dropped(self):
        # The issuer id is inside the signature: renaming it breaks
        # verification like any other field would.
        identity = NodeIdentity(key="ab" * 32)
        att = issue(identity.public_key_b64url)
        att["issuer"] = "someone-else"
        self.assertIsNone(
            A.verify_attestation(att, ISSUER_PUBLIC,
                                 identity.public_key_b64url))

    def test_aged_binding_holds_a_live_attestation(self):
        # M1 regression: a key bound 40 days ago gets a RENEWED
        # attestation — bound_at stays at the binding, issued_at is
        # now, the window is 7 days. expires − bound_at is 47 days:
        # the old shape check (window from bound_at, max 14 days)
        # dropped exactly this, so no key could ever reach the
        # 30-day standing rule. It must verify and carry.
        identity = NodeIdentity(key="ab" * 32)
        now = int(time.time())
        att = issue(identity.public_key_b64url,
                    bound_at=now - 40 * 24 * 3600,
                    issued_at=now - 3600,
                    expires=now - 3600 + 7 * 24 * 3600)
        out = A.verify_attestation(att, ISSUER_PUBLIC,
                                   identity.public_key_b64url)
        self.assertEqual(out, att)
        # And standing is arithmetically reachable: an assertion
        # inside the window is 30+ days after bound_at.
        self.assertGreaterEqual(now - att["bound_at"], 30 * 24 * 3600)
        self.assertTrue(att["issued_at"] <= now < att["expires"])


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    """The one requests.Session surface PoolClient uses."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url))
        return self._response


class PoolExchangeTest(unittest.TestCase):
    """A fake exchange handing the client an attestation."""

    def setUp(self):
        self.identity = NodeIdentity(key="ab" * 32)

    def _client(self, body: dict, issuer_key_provider=lambda: ISSUER_PUBLIC):
        session = _FakeSession(_FakeResponse(200, body))
        return PoolClient(
            {"id": "pool", "url": "https://pool.example", "api_key": "k"},
            identity=self.identity, auth_state={}, session=session,
            issuer_key_provider=issuer_key_provider)

    def _exchange_body(self, **extra) -> dict:
        body = {"access_token": "tok-1", "expires_in": 3600,
                "node_id": self.identity.node_id,
                "scopes": ["meta:read"], "binding_status": "bound"}
        body.update(extra)
        return body

    def test_valid_attestation_is_stored(self):
        att = issue(self.identity.public_key_b64url)
        client = self._client(self._exchange_body(attestation=att))
        client.exchange()
        self.assertEqual(client.auth_state.get("attestation"), att)
        self.assertEqual(client.current_attestation(), att)

    def test_tampered_attestation_is_dropped(self):
        att = issue(self.identity.public_key_b64url)
        att["expires"] += 60
        client = self._client(self._exchange_body(attestation=att))
        client.exchange()
        self.assertNotIn("attestation", client.auth_state)
        self.assertIsNone(client.current_attestation())

    def test_foreign_attestation_is_dropped(self):
        other = NodeIdentity(key="cd" * 32)
        att = issue(other.public_key_b64url)
        client = self._client(self._exchange_body(attestation=att))
        client.exchange()
        self.assertNotIn("attestation", client.auth_state)

    def test_unverifiable_attestation_is_dropped_without_issuer_key(self):
        att = issue(self.identity.public_key_b64url)
        client = self._client(
            self._exchange_body(attestation=att),
            issuer_key_provider=lambda: None)
        client.exchange()
        self.assertNotIn("attestation", client.auth_state)

    def test_exchange_without_attestation_stores_none(self):
        client = self._client(self._exchange_body())
        client.exchange()
        self.assertNotIn("attestation", client.auth_state)
        self.assertIsNone(client.current_attestation())

    def test_bad_attestation_never_replaces_a_verified_one(self):
        att = issue(self.identity.public_key_b64url)
        client = self._client(self._exchange_body(attestation=att))
        client.exchange()
        bad = issue(self.identity.public_key_b64url)
        bad["expires"] += 60
        client.session = _FakeSession(_FakeResponse(
            200, self._exchange_body(attestation=bad)))
        client.exchange(force=True)
        self.assertEqual(client.current_attestation(), att)


class MintingTest(unittest.TestCase):
    """Batches minted while the node holds an attestation carry it;
    batches minted without one carry none."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from metasync_node.identity import load_or_create_identity
        self.identity = load_or_create_identity(self.tmp.name)
        self.att = issue(self.identity.public_key_b64url)
        self.store = MetaStore(self.tmp.name)

    def _build(self, attestation=None) -> B.LibraryBuilder:
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store,
            attestation_provider=lambda: attestation)
        builder.run("2026-10-26T00:00:00+00:00")
        return builder

    def _headers(self, subject: str) -> list[dict]:
        return [env["batch"]["header"]
                for env in self.store.own_batches(subject)]

    def test_no_attestation_mints_headers_without_one(self):
        self._build()
        for header in self._headers(ARTIST_ID):
            self.assertNotIn("attestation", header)

    def test_builder_mints_headers_carrying_the_attestation(self):
        self._build(self.att)
        headers = self._headers(ARTIST_ID) + self._headers(ALBUM_ID)
        self.assertTrue(headers)
        for header in headers:
            self.assertEqual(header["attestation"], self.att)
        # The embedded attestation is inside the signed digest: the
        # batches still verify against the node's key.
        for env in self.store.own_batches(ARTIST_ID):
            C.verify_batch(self.identity.public_key_b64url, env)

    def test_remint_refresh_swaps_an_old_attestation_for_the_renewed(self):
        self._build(self.att)
        # A renewal: the same binding (bound_at does not move — it is
        # the binding moment), with a later expiry.
        renewed = issue(self.identity.public_key_b64url,
                        bound_at=BOUND_AT,
                        expires=EXPIRES + 24 * 3600)
        self.assertTrue(self.store.set_attestation(renewed))
        swapped = self.store.refresh_attestation_embeddings(
            self.identity.public_key_b64url,
            self.identity.signing_private_key)
        self.assertGreaterEqual(swapped, 1)
        for subject in (ARTIST_ID, ALBUM_ID):
            for header in self._headers(subject):
                self.assertEqual(header["attestation"], renewed)

    def test_second_build_with_renewed_attestation_remints(self):
        self._build(self.att)
        renewed = issue(self.identity.public_key_b64url,
                        bound_at=BOUND_AT,
                        expires=EXPIRES + 24 * 3600)
        self._build(renewed)
        for header in self._headers(ARTIST_ID):
            self.assertEqual(header["attestation"], renewed)

    def test_refresh_without_change_is_a_no_op(self):
        self._build(self.att)
        self.assertEqual(
            self.store.refresh_attestation_embeddings(
                self.identity.public_key_b64url,
                self.identity.signing_private_key),
            0)

    def test_responder_answers_carry_the_attestation(self):
        self._build()
        self.store.set_attestation(self.att)
        responder = Responder(self.store, self.identity,
                              mode_provider=lambda: "own_library")
        out = responder.handle({"kind": "artist", "mode": "exact",
                                "subjects": [ARTIST_ID]})
        self.assertTrue(out)
        for env in out:
            self.assertEqual(
                env["batch"]["header"]["attestation"], self.att)

    def test_responder_provider_attestation_is_used_and_noted(self):
        self._build()
        responder = Responder(
            self.store, self.identity,
            mode_provider=lambda: "own_library",
            attestation_provider=lambda: self.att)
        out = responder.handle({"kind": "artist", "mode": "exact",
                                "subjects": [ARTIST_ID]})
        self.assertTrue(out)
        for env in out:
            self.assertEqual(
                env["batch"]["header"]["attestation"], self.att)
        self.assertEqual(self.store.attestation(), self.att)

    def test_make_batch_refuses_a_malformed_attestation(self):
        claim = {"subject": ARTIST_ID, "kind": "artist", "field": "name",
                 "value": ["Radiohead"], "source": "file_tags",
                 "soulid_version": C.SOULID_VERSION}
        with self.assertRaises(C.ClaimError):
            C.make_batch(self.identity.public_key_b64url, [claim],
                         attestation={"node_key": "x"})


class FailureIsolationTest(unittest.TestCase):
    """SF-2: a raising attestation provider degrades to 'no
    attestation' everywhere — it must never break token exchange, a
    builder walk, or a responder answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from metasync_node.identity import load_or_create_identity
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = MetaStore(self.tmp.name)

    @staticmethod
    def _boom():
        raise RuntimeError("issuer key lookup exploded")

    def test_pool_note_attestation_survives_a_raising_key_provider(self):
        case = PoolExchangeTest(
            methodName="test_valid_attestation_is_stored")
        case.setUp()
        att = issue(case.identity.public_key_b64url)
        client = case._client(
            case._exchange_body(attestation=att),
            issuer_key_provider=self._boom)
        client.exchange()  # must not raise
        # The token exchange itself landed; only the attestation was
        # skipped (dropped as unverifiable), never stored.
        self.assertTrue(client.auth_state.get("access_token"))
        self.assertNotIn("attestation", client.auth_state)

    def test_builder_walk_survives_a_raising_provider(self):
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store,
            attestation_provider=self._boom)
        result = builder.run("2026-10-26T00:00:00+00:00")  # no raise
        self.assertIsInstance(result, dict)
        self.assertTrue(self.store.own_batches(ARTIST_ID))

    def test_responder_answers_survive_a_raising_provider(self):
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store)
        builder.run("2026-10-26T00:00:00+00:00")
        responder = Responder(
            self.store, self.identity,
            mode_provider=lambda: "own_library",
            attestation_provider=self._boom)
        out = responder.handle({"kind": "artist", "mode": "exact",
                                "subjects": [ARTIST_ID]})
        self.assertTrue(out)
        for env in out:
            self.assertNotIn("attestation", env["batch"]["header"])


class EmbedTimeFreshnessTest(unittest.TestCase):
    """SF-3: a stored attestation whose window does not cover now is
    treated as NO attestation at mint time — never embedded as a
    fossil that silently counts zero support."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from metasync_node.identity import load_or_create_identity
        self.identity = load_or_create_identity(self.tmp.name)
        self.store = MetaStore(self.tmp.name)

    def test_expired_window_attestation_reads_as_none(self):
        now = int(time.time())
        expired = issue(self.identity.public_key_b64url,
                        bound_at=now - 10 * 24 * 3600,
                        issued_at=now - 10 * 24 * 3600,
                        expires=now - 3 * 24 * 3600)
        self.store.set_attestation(expired)
        self.assertIsNone(self.store.attestation())

    def test_not_yet_valid_attestation_reads_as_none(self):
        now = int(time.time())
        future = issue(self.identity.public_key_b64url,
                       bound_at=now + 24 * 3600,
                       issued_at=now + 24 * 3600,
                       expires=now + 8 * 24 * 3600)
        self.store.set_attestation(future)
        self.assertIsNone(self.store.attestation())

    def test_aged_binding_with_live_window_is_embedded(self):
        # M1 at the mint path: bound 40 days ago, attestation issued
        # an hour ago — the window covers now, so the store hands it
        # out for embedding even though bound_at is far outside any
        # 7-day span. Under the old window-from-bound_at freshness
        # check this read as NO attestation.
        now = int(time.time())
        att = issue(self.identity.public_key_b64url,
                    bound_at=now - 40 * 24 * 3600,
                    issued_at=now - 3600,
                    expires=now - 3600 + 7 * 24 * 3600)
        self.store.set_attestation(att)
        self.assertEqual(self.store.attestation(), att)

    def test_live_window_attestation_is_embedded(self):
        att = issue(self.identity.public_key_b64url)
        self.store.set_attestation(att)
        self.assertEqual(self.store.attestation(), att)
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store)
        builder.run("2026-10-26T00:00:00+00:00")
        for env in self.store.own_batches(ARTIST_ID):
            self.assertEqual(env["batch"]["header"]["attestation"], att)

    def test_expired_attestation_is_not_embedded_by_a_build(self):
        now = int(time.time())
        expired = issue(self.identity.public_key_b64url,
                        bound_at=now - 10 * 24 * 3600,
                        issued_at=now - 10 * 24 * 3600,
                        expires=now - 3 * 24 * 3600)
        self.store.set_attestation(expired)
        builder = B.LibraryBuilder(
            FakeSoulSync(full_pages()), self.identity, self.store)
        builder.run("2026-10-26T00:00:00+00:00")
        for env in self.store.own_batches(ARTIST_ID):
            self.assertNotIn("attestation", env["batch"]["header"])


class AttestationPoolNoteTest(unittest.TestCase):
    """SF-4: with several pools, when the embedded attestation comes
    from a different pool than the relay session's (always the first
    enabled pool), relay_status says so instead of failing silently."""

    def _service(self, pools, attestations):
        from metasync_node.config import Config
        from metasync_node.service import NodeService
        from metasync_node.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        store.settings["pools"] = pools

        class _Client:
            def __init__(self, attestation):
                self._attestation = attestation

            def current_attestation(self):
                return self._attestation

        return NodeService(
            Config(state_dir=tmp.name),
            store=store,
            pool_clients={
                pool["id"]: _Client(attestations.get(pool["id"]))
                for pool in pools
            },
            env={},
        )

    def test_mismatched_pool_is_noted(self):
        pools = [{"id": "pool-a", "url": "https://a.example"},
                 {"id": "pool-b", "url": "https://b.example"}]
        att = issue("A" * 43)  # shape only matters via the fake client
        service = self._service(pools, {"pool-b": att})
        status = service.relay_status()
        self.assertIsNotNone(status["attestation_note"])
        self.assertIn("pool-b", status["attestation_note"])
        self.assertIn("pool-a", status["attestation_note"])

    def test_matching_pool_is_quiet(self):
        pools = [{"id": "pool-a", "url": "https://a.example"},
                 {"id": "pool-b", "url": "https://b.example"}]
        att = issue("A" * 43)
        service = self._service(pools, {"pool-a": att})
        self.assertIsNone(service.relay_status()["attestation_note"])

    def test_single_pool_is_quiet(self):
        pools = [{"id": "pool-a", "url": "https://a.example"}]
        att = issue("A" * 43)
        service = self._service(pools, {"pool-a": att})
        self.assertIsNone(service.relay_status()["attestation_note"])


class IssuerKeyNoteTest(unittest.TestCase):
    """S6: with no usable issuer key, the node drops every
    attestation and every revocation list — relay_status must SAY
    so (issuer_note), instead of the node silently minting
    zero-support batches and holding no list."""

    def _service(self, settings_extra: dict):
        from metasync_node.config import Config
        from metasync_node.service import NodeService
        from metasync_node.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        store.settings["pools"] = []
        store.settings.update(settings_extra)
        return NodeService(
            Config(state_dir=tmp.name), store=store,
            pool_clients={}, env={})

    def test_unset_issuer_key_is_noted(self):
        status = self._service({}).relay_status()
        self.assertIsNotNone(status["issuer_note"])
        self.assertIn("not set", status["issuer_note"])

    def test_unparsable_issuer_key_is_noted(self):
        status = self._service(
            {"meta_issuer_public_key": "not-a-key"}).relay_status()
        self.assertIsNotNone(status["issuer_note"])
        self.assertIn("does not parse", status["issuer_note"])

    def test_configured_issuer_key_is_quiet(self):
        from cryptography.hazmat.primitives import serialization

        raw = ISSUER_PUBLIC.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        status = self._service(
            {"meta_issuer_public_key": b64url_encode(raw)}).relay_status()
        self.assertIsNone(status["issuer_note"])


if __name__ == "__main__":
    unittest.main()

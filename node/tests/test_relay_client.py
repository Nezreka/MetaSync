"""Relay client tests — the REAL relay server, in-process, and the
real node RelayClient against it (DESIGN-METADATA.md section 10).

The server runs on an ephemeral port in its own thread/loop with a
fake key store (the relay/tests/conftest.py pattern) and tokens from
an in-test issuer mirroring issueAccessToken in src/sin/logic.ts:
header {"alg": "EdDSA", "typ": "SIN"}, claims exactly
{key_id, node_id, scopes, exp, iat}, Ed25519 over the dotted input.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay"))

from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metasync_node import relay as relay_module
from metasync_node.config import Config
from metasync_node.identity import NodeIdentity, b64url_encode
from metasync_node.relay import (
    RelayClient,
    RelayError,
    RelayNotConnected,
    _QueryState,
    _QueryStream,
)
from metasync_node.service import NodeService
from metasync_node.store import Store
from metasync_relay.config import RelayConfig
from metasync_relay.server import Relay, hello_signing_text, start_server
from metasync_relay.tokens import decode_base64url, encode_base64url
from websockets.asyncio.client import connect as ws_connect

FULL_SCOPES = ["report:write", "index:read", "playlists:read", "whoami:read", "meta:read"]
KEY_ID = "key-1"


def issue_token(pool_private, key_id, node_id, scopes, now=None, ttl=24 * 3600):
    """Mirror of issueAccessToken (src/sin/logic.ts)."""
    iat = int(now if now is not None else time.time())
    claims = {
        "key_id": key_id,
        "node_id": node_id,
        "scopes": list(scopes),
        "exp": iat + ttl,
        "iat": iat,
    }
    header = {"alg": "EdDSA", "typ": "SIN"}
    signing_input = (
        encode_base64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + encode_base64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    signature = pool_private.sign(signing_input.encode("utf-8"))
    return f"{signing_input}.{encode_base64url(signature)}"


class FakeKeyStore:
    """In-memory guest list, the relay/tests/conftest.py pattern."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._bound: dict[str, tuple[str, bytes]] = {}

    def bind(self, key_id: str, node_id: str, public_raw: bytes) -> None:
        self._bound[key_id] = (node_id, public_raw)

    def is_bound_and_valid(self, key_id: str, node_id: str, node_public_key: bytes) -> bool:
        self.calls.append(("is_bound_and_valid", key_id, node_id))
        return self._bound.get(key_id) == (node_id, node_public_key)


class RelayServerThread:
    """The real relay server on an ephemeral port, own thread + loop."""

    def __init__(self, pool_public, **config_overrides) -> None:
        self.store = FakeKeyStore()
        config = RelayConfig(window_seconds=0.5, grace_seconds=1.0, **config_overrides)
        self.relay = Relay(self.store, pool_public, config)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._server = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="test-relay-server")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._server = self.loop.run_until_complete(start_server(self.relay, "127.0.0.1", self.port))
        self._started.set()
        self.loop.run_forever()

    def start(self) -> None:
        self.thread.start()
        if not self._started.wait(10):
            raise RuntimeError("relay server did not start")

    def stop(self) -> None:
        async def shutdown() -> None:
            self._server.close()
            await self._server.wait_closed()
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.thread.join(timeout=10)

    def drop(self, node_id: str) -> None:
        """Close one node's socket from the server side."""
        conn = self.relay.presence.get(node_id)
        assert conn is not None, f"node {node_id} not present"
        asyncio.run_coroutine_threadsafe(conn.ws.close(), self.loop).result(timeout=10)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


class BadSignIdentity(NodeIdentity):
    """Presents a real key but signs garbage: the relay must refuse."""

    def sign(self, message) -> str:
        """Sign with garbage bytes instead of the real key."""
        return b64url_encode(b"\x00" * 64)


class RelayClientCase(unittest.TestCase):
    pool_private = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.pool_private = Ed25519PrivateKey.generate()
        cls.pool_public = cls.pool_private.public_key()

    def setUp(self) -> None:
        self.server = RelayServerThread(self.pool_public)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.clients: list[RelayClient] = []
        self.addCleanup(self._stop_clients)

    def _stop_clients(self) -> None:
        for client in self.clients:
            client.stop()

    def token_for(self, identity: NodeIdentity) -> str:
        return issue_token(self.pool_private, KEY_ID, identity.node_id, FULL_SCOPES)

    def bind(self, identity: NodeIdentity) -> None:
        self.server.store.bind(KEY_ID, identity.node_id, identity.public_key_bytes)

    def make_client(self, identity=None, mode="own_library", url=None, token_provider=None) -> RelayClient:
        identity = identity or NodeIdentity(key=secrets.token_hex(32))
        url = self.server.url if url is None else url
        mode_value = mode
        client = RelayClient(
            identity,
            url_provider=lambda: url,
            mode_provider=lambda: mode_value,
            token_provider=token_provider or (lambda: self.token_for(identity)),
        )
        self.clients.append(client)
        return client

    def connect_client(self, identity=None, **kwargs) -> RelayClient:
        identity = identity or NodeIdentity(key=secrets.token_hex(32))
        self.bind(identity)
        client = self.make_client(identity, **kwargs)
        client.start()
        wait_until(lambda: client.connected)
        return client


class TestHello(RelayClientCase):
    def test_connect_welcome_and_status(self) -> None:
        client = self.connect_client()
        status = client.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["peers"], 0)  # alone in the room
        self.assertEqual(status["mode"], "own_library")
        self.assertEqual(status["relay_url"], self.server.url)
        self.assertIsNone(status["last_error"])

    def test_second_client_sees_one_peer(self) -> None:
        self.connect_client()
        second = self.connect_client()
        self.assertEqual(second.status()["peers"], 1)

    def test_refused_on_bad_signature(self) -> None:
        identity = BadSignIdentity(key=secrets.token_hex(32))
        self.bind(identity)
        client = self.make_client(identity)
        client.start()
        wait_until(lambda: (client.status()["last_error"] or "").startswith("refused"))
        self.assertFalse(client.connected)
        self.assertTrue(client.is_alive())  # backing off, not spun or dead

    def test_mode_defaults_to_own_library(self) -> None:
        identity = NodeIdentity(key=secrets.token_hex(32))
        self.bind(identity)
        client = self.make_client(identity, mode="")
        client.start()
        wait_until(lambda: client.connected)
        self.assertEqual(client.status()["mode"], "own_library")
        conn = self.server.relay.presence[identity.node_id]
        self.assertEqual(conn.mode, "own_library")


class TestQueryRoundTrip(RelayClientCase):
    def test_query_round_trip_between_two_clients(self) -> None:
        batch = {"claims": [{"subject": "soul_x", "field": "name"}], "signer": "b"}
        seen_queries: list[dict] = []
        received_types_b: list[str] = []

        responder = self.connect_client()
        asker = self.connect_client()

        def handler(query):
            seen_queries.append(query)
            return [batch]

        responder.set_query_handler(handler)
        original_dispatch = responder._dispatch

        def spy_dispatch(raw, outbox):
            received_types_b.append(json.loads(raw).get("type"))
            return original_dispatch(raw, outbox)

        responder._dispatch = spy_dispatch

        results = list(
            asker.query("track", "exact", subjects=["soul_x"], window_s=0.5, grace_s=1.0)
        )
        self.assertEqual(results, [batch])
        self.assertEqual(len(seen_queries), 1)
        self.assertEqual(seen_queries[0]["subjects"], ["soul_x"])
        self.assertEqual(seen_queries[0]["kind"], "track")
        # The responder received the query and nothing else: answers are
        # routed to the asker only, never echoed to other nodes.
        self.assertEqual(received_types_b, ["query"])

    def test_handler_exception_still_sends_done_and_connection_survives(self) -> None:
        responder = self.connect_client()  # first: the asker sees one peer
        asker = self.connect_client()
        self.assertEqual(asker.status()["peers"], 1)

        def exploding(query):
            raise RuntimeError("boom")

        responder.set_query_handler(exploding)
        started = time.monotonic()
        results = list(
            asker.query("album", "text", terms=["radiohead"], window_s=0.3, grace_s=0.4)
        )
        elapsed = time.monotonic() - started
        self.assertEqual(results, [])
        # The handler threw, the done still went out (the responder is
        # not left hanging at the relay), and the asker's stream ran
        # to its window+grace deadline: the client has no done-based
        # early close, because it cannot know the responder set (see
        # RelayClient.query), so the deadline is the only close.
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertLess(elapsed, 3.0)
        self.assertTrue(responder.connected)

        batch = {"claims": [], "ok": True}
        responder.set_query_handler(lambda query: [batch])
        results = list(
            asker.query("album", "text", terms=["radiohead"], window_s=0.3, grace_s=2.0)
        )
        self.assertEqual(results, [batch])

    def test_no_handler_answers_done_only(self) -> None:
        responder = self.connect_client()
        asker = self.connect_client()
        results = list(
            asker.query("artist", "text", terms=["bjork"], window_s=0.3, grace_s=1.0)
        )
        self.assertEqual(results, [])
        self.assertTrue(responder.connected)
        self.assertTrue(asker.connected)


class TestReconnect(RelayClientCase):
    def test_reconnect_after_server_side_drop(self) -> None:
        batch = {"claims": [{"n": 1}]}
        responder = self.connect_client()
        responder.set_query_handler(lambda query: [batch])
        asker_identity = NodeIdentity(key=secrets.token_hex(32))
        asker = self.connect_client(asker_identity)

        self.server.drop(asker_identity.node_id)
        wait_until(lambda: not asker.connected)
        # The client re-hellos on its own and can query again.
        wait_until(lambda: asker.connected)
        self.assertIn(asker_identity.node_id, self.server.relay.presence)
        results = list(
            asker.query("track", "exact", subjects=["soul_y"], window_s=0.5, grace_s=1.0)
        )
        self.assertEqual(results, [batch])


class TestAdmission(RelayClientCase):
    def test_query_requires_connection_and_valid_shape(self) -> None:
        client = self.make_client()  # never started
        with self.assertRaises(ValueError):
            client.query("label", "exact", subjects=["x"])
        with self.assertRaises(ValueError):
            client.query("track", "exact", subjects=[])
        with self.assertRaises(ValueError):
            client.query("track", "text", terms=None)
        with self.assertRaises(RelayNotConnected):
            client.query("track", "exact", subjects=["soul_x"])

    def test_answers_for_unknown_queries_dropped_by_client(self) -> None:
        client = self.make_client()
        # No open queries: routed answers and dones are silent no-ops.
        client._dispatch(
            json.dumps({"type": "answer", "query_id": "e" * 32, "batch": {"x": 1}}), None
        )
        client._dispatch(json.dumps({"type": "done", "query_id": "e" * 32}), None)
        client._dispatch("not json at all", None)
        client._dispatch(json.dumps({"type": "mystery"}), None)

    def test_unrouted_answer_charged_by_relay(self) -> None:
        identity = NodeIdentity(key=secrets.token_hex(32))
        self.bind(identity)
        token = self.token_for(identity)

        async def scenario() -> None:
            async with ws_connect(self.server.url) as ws:
                challenge = json.loads(await ws.recv())
                assert challenge["type"] == "challenge"
                nonce = decode_base64url(challenge["nonce"], 32)
                timestamp = int(time.time())
                hello = {
                    "type": "hello",
                    "version": 1,
                    "node_key": identity.public_key_b64url,
                    "mode": "own_library",
                    "token": token,
                    "timestamp": timestamp,
                    "challenge": challenge["nonce"],
                    "signature": identity.sign(hello_signing_text(token, timestamp, nonce)),
                }
                await ws.send(json.dumps(hello))
                welcome = json.loads(await ws.recv())
                assert welcome["type"] == "welcome", welcome
                # An answer for a query the relay never routed: dropped
                # and charged as a violation, never delivered anywhere.
                await ws.send(
                    json.dumps({"type": "answer", "query_id": "f" * 32, "batch": {}})
                )
                await asyncio.sleep(0.5)

        asyncio.run(scenario())
        state = self.server.relay.node_state[identity.node_id]
        self.assertEqual(state.violations, 1)


class StubPoolClient:
    """A pool client stand-in for the service wiring: exchange() hands
    back a pre-minted token in auth_state, like the real client does."""

    def __init__(self, token: str, scopes=FULL_SCOPES) -> None:
        self.auth_state: dict = {}
        self._token = token
        self._scopes = scopes

    def exchange(self, force: bool = False) -> dict:
        self.auth_state["access_token"] = self._token
        if self._scopes is not None:
            self.auth_state["scopes"] = list(self._scopes)
        return {"access_token": self._token}


class TestServiceWiring(RelayClientCase):
    def test_meta_network_switch_drives_the_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            identity = NodeIdentity(key=secrets.token_hex(32))
            self.bind(identity)
            token = self.token_for(identity)
            store = Store(tmp)
            config = Config(state_dir=tmp, musicbrainz_min_interval_s=0.0, http_timeout_s=5)
            service = NodeService(
                config,
                store=store,
                identity=identity,
                pool_clients={"sin": StubPoolClient(token)},
                env={},
            )
            self.addCleanup(service.stop_relay_client)
            store.settings["pools"] = [
                {
                    "id": "sin",
                    "name": "SIN",
                    "url": "https://pool.example",
                    "api_key": "test-key",
                    "role": "node",
                    "enabled": True,
                }
            ]
            store.settings["meta_relay_url"] = self.server.url

            # Default: the network is off, nothing connects.
            status = service.relay_status()
            self.assertFalse(status["enabled"])
            self.assertFalse(status["connected"])
            service.sync_relay_client()
            self.assertFalse(service.relay_status()["connected"])

            # Switch on: the client connects and reports through.
            store.settings["meta_network"] = True
            service.sync_relay_client()
            wait_until(lambda: service.relay_status()["connected"])
            status = service.relay_status()
            self.assertTrue(status["enabled"])
            self.assertEqual(status["mode"], "own_library")
            self.assertEqual(status["relay_url"], self.server.url)

            # Switch off: the client stops.
            store.settings["meta_network"] = False
            status = service.sync_relay_client()
            self.assertFalse(status["enabled"])
            self.assertFalse(status["connected"])


class TestOptionalDependency(unittest.TestCase):
    """Finding 1: websockets is an optional dependency of this feature
    only. With it unimportable, the node package (service included)
    must still import, the client must construct, and a connect
    attempt must degrade to a recorded error — never an ImportError
    at import time bricking the whole node."""

    def test_service_and_client_import_without_websockets(self) -> None:
        import subprocess
        import textwrap

        node_dir = str(Path(__file__).resolve().parents[1])
        code = textwrap.dedent(
            f"""
            import importlib.abc
            import secrets
            import sys
            import time

            class Blocker(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name == "websockets" or name.startswith("websockets."):
                        raise ImportError("blocked: no websockets in this test")
                    return None

            sys.meta_path.insert(0, Blocker())
            sys.path.insert(0, {node_dir!r})

            import metasync_node.relay as relay
            import metasync_node.service  # noqa: F401 - must import cleanly
            from metasync_node.identity import NodeIdentity

            client = relay.RelayClient(
                NodeIdentity(key=secrets.token_hex(32)),
                url_provider=lambda: "ws://127.0.0.1:9",
                mode_provider=lambda: "own_library",
                token_provider=lambda: "tok",
            )
            client.start()
            err = None
            deadline = time.time() + 20
            while time.time() < deadline:
                err = client.status()["last_error"]
                if err and "websockets" in err:
                    break
                time.sleep(0.1)
            assert err and "websockets" in err, err
            assert client.stop() is True
            print("ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)


class TestHandlerOffLoop(RelayClientCase):
    """Finding 2: a handler returning a lazy generator must have that
    generator consumed in the worker thread, not on the read loop."""

    def test_generator_handler_iterated_off_the_loop_thread(self) -> None:
        iterated_on: list[int] = []

        responder = self.connect_client()
        asker = self.connect_client()

        def handler(query):
            def gen():
                for n in range(3):
                    iterated_on.append(threading.get_ident())
                    yield {"claims": [{"n": n}]}
                    time.sleep(0.05)

            return gen()

        responder.set_query_handler(handler)
        results = list(
            asker.query("track", "exact", subjects=["soul_g"], window_s=0.5, grace_s=1.0)
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(len(iterated_on), 3)
        self.assertNotIn(responder._loop_thread_id, iterated_on)


class TestSessionBackoff(unittest.TestCase):
    """Finding 3: the reconnect backoff resets only after a session
    that lived long enough to count as healthy; short clean closes
    grow it like failures."""

    def _count_attempts(self, session_seconds: float, run_for: float) -> int:
        identity = NodeIdentity(key=secrets.token_hex(32))
        client = RelayClient(
            identity,
            url_provider=lambda: "ws://unused.invalid",
            mode_provider=lambda: "own_library",
            token_provider=lambda: "tok",
        )
        attempts: list[int] = []

        async def fake_connect(url, mode, token):
            attempts.append(1)
            return session_seconds

        client._connect_once = fake_connect
        patches = [
            mock.patch.object(relay_module, "_MIN_SESSION_SECONDS", 3600.0),
            mock.patch.object(relay_module, "_BACKOFF_INITIAL_SECONDS", 0.05),
            mock.patch.object(relay_module, "_BACKOFF_MAX_SECONDS", 0.4),
            mock.patch.object(relay_module.random, "uniform", lambda a, b: 1.0),
        ]
        for patcher in patches:
            patcher.start()
        try:
            client.start()
            time.sleep(run_for)
            self.assertTrue(client.stop())
        finally:
            for patcher in patches:
                patcher.stop()
        return len(attempts)

    def test_short_clean_closes_grow_the_backoff(self) -> None:
        attempts = self._count_attempts(session_seconds=0.0, run_for=1.35)
        # Uncapped ~1s-floor retries would attempt ~27 times; growing
        # backoff (0.05, 0.1, 0.2, 0.4, 0.4...) attempts a handful.
        self.assertGreaterEqual(attempts, 3)
        self.assertLessEqual(attempts, 10)

    def test_healthy_sessions_reset_the_backoff(self) -> None:
        attempts = self._count_attempts(session_seconds=7200.0, run_for=1.35)
        self.assertGreaterEqual(attempts, 15)


class TestStopSemantics(RelayClientCase):
    """Finding 4: stop() must never leave a zombie client behind."""

    def test_stop_during_blocked_token_fetch(self) -> None:
        release = threading.Event()
        provider_calls: list[int] = []
        connect_calls: list[int] = []

        def blocking_provider() -> str:
            provider_calls.append(1)
            release.wait(timeout=30)
            return "tok"

        identity = NodeIdentity(key=secrets.token_hex(32))
        client = RelayClient(
            identity,
            url_provider=lambda: self.server.url,
            mode_provider=lambda: "own_library",
            token_provider=blocking_provider,
        )

        async def fake_connect(url, mode, token):
            connect_calls.append(1)
            return 0.0

        client._connect_once = fake_connect
        self.clients.append(client)
        client.start()
        wait_until(lambda: len(provider_calls) == 1)
        started = time.monotonic()
        stopped = client.stop()
        elapsed = time.monotonic() - started
        release.set()
        # The loop thread never blocked on the provider, so the stop
        # landed promptly — and the stop flag was re-checked before
        # any connect attempt, so the wedged fetch bought no session.
        self.assertTrue(stopped)
        self.assertLess(elapsed, 5.0)
        self.assertFalse(client.is_alive())
        self.assertEqual(connect_calls, [])

    def test_join_timeout_keeps_the_thread_handle(self) -> None:
        client = self.make_client()  # never started against the server
        release = threading.Event()
        zombie = threading.Thread(target=lambda: release.wait(30), daemon=True)
        zombie.start()
        real_join = threading.Thread.join
        zombie.join = lambda timeout=None: None  # the join "times out"
        client._thread = zombie
        try:
            self.assertFalse(client.stop())
            # Handle kept: start() must not spawn a duelist.
            self.assertIs(client._thread, zombie)
            client.start()
            self.assertIs(client._thread, zombie)
        finally:
            release.set()
            real_join(zombie, timeout=10)
        client._thread = None


class TestQueryBuffering(unittest.TestCase):
    """Finding 5: per-query buffering is capped, and teardown forgets
    finished query states."""

    def _client_with_query(self):
        identity = NodeIdentity(key=secrets.token_hex(32))
        client = RelayClient(
            identity,
            url_provider=lambda: "",
            mode_provider=lambda: "own_library",
            token_provider=lambda: "tok",
        )
        state = _QueryState(query_id="a" * 32)
        with client._lock:
            client._queries[state.query_id] = state
        return client, state

    @staticmethod
    def _answer_frame(state: _QueryState, batch: dict) -> str:
        return json.dumps({"type": "answer", "query_id": state.query_id, "batch": batch})

    def test_batch_count_cap_finishes_the_query(self) -> None:
        client, state = self._client_with_query()
        with mock.patch.object(relay_module, "MAX_QUEUED_BATCHES_PER_QUERY", 3):
            for n in range(5):
                client._dispatch(self._answer_frame(state, {"n": n}), None)
        self.assertTrue(state.finished.is_set())
        self.assertNotIn(state.query_id, client._queries)
        stream = _QueryStream(client, state)
        self.assertEqual([next(stream) for _ in range(3)], [{"n": 0}, {"n": 1}, {"n": 2}])
        with self.assertRaises(StopIteration):
            next(stream)

    def test_byte_cap_finishes_the_query(self) -> None:
        client, state = self._client_with_query()
        client._dispatch(self._answer_frame(state, {"d": "x"}), None)
        self.assertFalse(state.finished.is_set())
        self.assertEqual(state.queued_batches, 1)
        with mock.patch.object(
            relay_module, "MAX_QUEUED_BYTES_PER_QUERY", state.queued_bytes
        ):
            client._dispatch(self._answer_frame(state, {"d": "y"}), None)
        self.assertTrue(state.finished.is_set())
        self.assertNotIn(state.query_id, client._queries)
        self.assertEqual(state.batches.qsize(), 1)

    def test_teardown_clears_finished_states(self) -> None:
        client, state = self._client_with_query()
        other = _QueryState(query_id="b" * 32)
        with client._lock:
            client._queries[other.query_id] = other
        client._finish_all_queries()
        self.assertEqual(client._queries, {})
        self.assertTrue(state.finished.is_set())
        self.assertTrue(other.finished.is_set())


class TestNoEarlyClose(RelayClientCase):
    """Finding 6: there is no done-based early close. The client cannot
    know the responder set, so a responder's done — however early —
    leaves the asker's stream open until the window+grace deadline."""

    def test_done_does_not_close_the_stream_early(self) -> None:
        responder = self.connect_client()
        asker = self.connect_client()
        batch = {"claims": [{"n": 1}]}
        responder.set_query_handler(lambda query: [batch])
        started = time.monotonic()
        results = list(
            asker.query("track", "exact", subjects=["soul_e"], window_s=0.4, grace_s=0.8)
        )
        elapsed = time.monotonic() - started
        self.assertEqual(results, [batch])
        self.assertGreaterEqual(elapsed, 1.1)  # the full 1.2s deadline ran


class TestDeliverRace(unittest.TestCase):
    """Finding 7: a query whose captured outbox is no longer the live
    connection's fails at once instead of draining to the deadline."""

    def _client(self) -> RelayClient:
        identity = NodeIdentity(key=secrets.token_hex(32))
        return RelayClient(
            identity,
            url_provider=lambda: "",
            mode_provider=lambda: "own_library",
            token_provider=lambda: "tok",
        )

    def test_stale_outbox_fails_the_query(self) -> None:
        client = self._client()
        live = asyncio.Queue()
        stale = asyncio.Queue()
        client._outbox = live
        with client._lock:
            client._connected = True
        state = _QueryState(query_id="c" * 32)
        with client._lock:
            client._queries[state.query_id] = state
        client._deliver_query(stale, state, "text")
        self.assertTrue(state.finished.is_set())
        self.assertIsNotNone(state.error)
        self.assertNotIn(state.query_id, client._queries)
        self.assertTrue(stale.empty())
        self.assertTrue(live.empty())
        with self.assertRaises(RelayNotConnected):
            next(_QueryStream(client, state))

    def test_live_outbox_delivers(self) -> None:
        client = self._client()
        live = asyncio.Queue()
        client._outbox = live
        with client._lock:
            client._connected = True
        state = _QueryState(query_id="d" * 32)
        with client._lock:
            client._queries[state.query_id] = state
        client._deliver_query(live, state, "text")
        self.assertEqual(live.get_nowait(), "text")
        self.assertFalse(state.finished.is_set())


class TestThreadDeathReporting(unittest.TestCase):
    """Finding 9: an unexpected exception in the run loop is captured
    into last_error, so status() shows why the thread died."""

    def test_run_loop_exception_lands_in_status(self) -> None:
        identity = NodeIdentity(key=secrets.token_hex(32))
        client = RelayClient(
            identity,
            url_provider=lambda: "ws://unused.invalid",
            mode_provider=lambda: "own_library",
            token_provider=lambda: "tok",
        )

        async def boom():
            raise RuntimeError("kaboom-9")

        client._run_loop = boom
        client.start()
        wait_until(lambda: not client.is_alive())
        self.assertIn("kaboom-9", client.status()["last_error"] or "")


class TestServiceTokenScopes(RelayClientCase):
    """Finding 8: the service's relay token check fails closed — a
    token whose scope list is empty or absent does NOT carry
    meta:read, and the diagnosis is raised locally."""

    def _service_with_stub(self, stub: StubPoolClient) -> NodeService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        identity = NodeIdentity(key=secrets.token_hex(32))
        store = Store(tmp.name)
        config = Config(state_dir=tmp.name, musicbrainz_min_interval_s=0.0, http_timeout_s=5)
        service = NodeService(
            config,
            store=store,
            identity=identity,
            pool_clients={"sin": stub},
            env={},
        )
        store.settings["pools"] = [
            {
                "id": "sin",
                "name": "SIN",
                "url": "https://pool.example",
                "api_key": "test-key",
                "role": "node",
                "enabled": True,
            }
        ]
        return service

    def test_empty_scopes_rejected(self) -> None:
        service = self._service_with_stub(StubPoolClient("tok", scopes=[]))
        with self.assertRaises(RelayError):
            service._relay_token()

    def test_absent_scopes_rejected(self) -> None:
        service = self._service_with_stub(StubPoolClient("tok", scopes=None))
        with self.assertRaises(RelayError):
            service._relay_token()

    def test_scopes_without_meta_read_rejected(self) -> None:
        service = self._service_with_stub(StubPoolClient("tok", scopes=["report:write"]))
        with self.assertRaises(RelayError):
            service._relay_token()

    def test_meta_read_accepted(self) -> None:
        service = self._service_with_stub(StubPoolClient("tok"))
        self.assertEqual(service._relay_token(), "tok")


if __name__ == "__main__":
    unittest.main()

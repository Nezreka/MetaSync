"""Relay integration tests: real WebSocket server, fake key store,
in-test Ed25519 identities, tokens minted by the conftest issuer that
mirrors src/sin/logic.ts issueAccessToken."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Self

import pytest
from conftest import FULL_SCOPES, FakeKeyStore, NodeIdentity, issue_token
from metasync_relay.config import RelayConfig
from metasync_relay.protocol import Query
from metasync_relay.server import Relay, _Connection, hello_signing_text, start_server
from metasync_relay.tokens import decode_base64url, encode_base64url
from websockets.asyncio.client import connect as ws_connect

NO_META_SCOPES = ["report:write", "index:read"]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for raw in self.ws:
                self.queue.put_nowait(json.loads(raw))
        except Exception:  # noqa: BLE001, S110 - a dead socket is the finally's news
            pass
        finally:
            self.closed.set()
            self.queue.put_nowait(None)

    async def send(self, obj) -> None:
        await self.ws.send(json.dumps(obj))

    async def next(self, timeout: float = 3.0):
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def expect_silence(self, timeout: float = 0.45) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await self.next(timeout)

    async def expect_closed(self, timeout: float = 3.0) -> None:
        await asyncio.wait_for(self.closed.wait(), timeout)

    async def close(self) -> None:
        self.task.cancel()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001, S110 - teardown ignores a dead socket
            pass


class Harness:
    def __init__(self, pool_private, pool_public, **config_overrides) -> None:
        self.pool_private = pool_private
        self.store = FakeKeyStore()
        self.relay = Relay(self.store, pool_public, RelayConfig(**config_overrides))
        self.port = _free_port()
        self.server = None
        self.clients: list[Client] = []

    async def __aenter__(self) -> Self:
        self.server = await start_server(self.relay, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *exc) -> None:
        for client in self.clients:
            await client.close()
        self.server.close()
        await self.server.wait_closed()

    def token(self, node: NodeIdentity, key_id: str = "key-1", scopes=FULL_SCOPES, **kw):
        return issue_token(self.pool_private, key_id, node.node_id, scopes, **kw)

    async def connect(
        self,
        node: NodeIdentity,
        key_id: str = "key-1",
        token: str | None = None,
        timestamp: int | None = None,
        signature: str | None = None,
        mode: str = "own_library",
    ) -> Client:
        ws = await ws_connect(f"ws://127.0.0.1:{self.port}")
        client = Client(ws)
        self.clients.append(client)
        challenge = await client.next()
        assert challenge["type"] == "challenge", challenge
        nonce = challenge["nonce"]
        ts = int(time.time()) if timestamp is None else timestamp
        tok = self.token(node, key_id) if token is None else token
        sig = (
            encode_base64url(
                node.sign(hello_signing_text(tok, ts, decode_base64url(nonce, 32)))
            )
            if signature is None
            else signature
        )
        await client.send(
            {
                "type": "hello",
                "version": 1,
                "node_key": encode_base64url(node.public_raw),
                "mode": mode,
                "token": tok,
                "timestamp": ts,
                "challenge": nonce,
                "signature": sig,
            }
        )
        return client

    async def connect_ok(self, node: NodeIdentity, key_id: str = "key-1", **kw) -> Client:
        self.store.bind(key_id, node)
        client = await self.connect(node, key_id=key_id, **kw)
        welcome = await client.next()
        assert welcome["type"] == "welcome", welcome
        client.welcome = welcome
        return client


def qid(n: int) -> str:
    return f"{n:032x}"


def exact_query(query_id: str) -> dict:
    return {
        "type": "query",
        "query_id": query_id,
        "kind": "track",
        "mode": "exact",
        "subjects": ["soul_example"],
    }


def answer(query_id: str, batch=None) -> dict:
    return {"type": "answer", "query_id": query_id, "batch": batch or {"claims": []}}


# -- the hello door ------------------------------------------------------


def test_hello_accepted_and_peer_count_grows(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            assert a.welcome["peers"] == 0
            b = await h.connect_ok(node_b, key_id="key-2")
            assert b.welcome["peers"] == 1
            assert set(h.relay.presence) == {node_a.node_id, node_b.node_id}

    asyncio.run(scenario())


def test_hello_rejected_when_token_node_mismatch(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)
            # Token minted for node_b, presented by node_a with node_a's key.
            foreign = h.token(node_b)
            client = await h.connect(node_a, token=foreign)
            refused = await client.next()
            assert refused == {"type": "refused", "reason": "auth"}
            await client.expect_closed()
            assert h.relay.presence == {}

    asyncio.run(scenario())


def test_hello_rejected_when_key_unbound(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            client = await h.connect(node_a)  # store never bound
            refused = await client.next()
            assert refused["reason"] == "auth"
            await client.expect_closed()

    asyncio.run(scenario())


def test_hello_rejected_when_bound_to_another_node(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_b)  # the key belongs to node_b's binding
            client = await h.connect(node_a)
            refused = await client.next()
            assert refused["reason"] == "auth"

    asyncio.run(scenario())


def test_hello_rejected_without_meta_read_scope(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)
            client = await h.connect(node_a, token=h.token(node_a, scopes=NO_META_SCOPES))
            refused = await client.next()
            assert refused["reason"] == "auth"

    asyncio.run(scenario())


def test_hello_rejected_with_bad_signature(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)
            bogus = encode_base64url(node_a.sign(b"some other text"))
            client = await h.connect(node_a, signature=bogus)
            refused = await client.next()
            assert refused["reason"] == "auth"

    asyncio.run(scenario())


def test_hello_rejected_with_stale_timestamp(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)
            client = await h.connect(node_a, timestamp=int(time.time()) - 3600)
            refused = await client.next()
            assert refused["reason"] == "auth"

    asyncio.run(scenario())


def test_hello_rejected_with_expired_token(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)
            stale = h.token(node_a, now=time.time() - 90000)
            client = await h.connect(node_a, token=stale)
            refused = await client.next()
            assert refused["reason"] == "auth"

    asyncio.run(scenario())


def test_message_before_hello_refused(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            ws = await ws_connect(f"ws://127.0.0.1:{h.port}")
            client = Client(ws)
            h.clients.append(client)
            challenge = await client.next()
            assert challenge["type"] == "challenge"
            await client.send(exact_query(qid(1)))
            refused = await client.next()
            assert refused["type"] == "refused"
            await client.expect_closed()

    asyncio.run(scenario())


def test_hello_replay_rejected_fresh_challenge_accepted(
    pool_private, pool_public, node_a
):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            h.store.bind("key-1", node_a)

            async def raw_client() -> tuple[Client, str]:
                ws = await ws_connect(f"ws://127.0.0.1:{h.port}")
                client = Client(ws)
                h.clients.append(client)
                challenge = await client.next()
                assert challenge["type"] == "challenge"
                return client, challenge["nonce"]

            def hello_frame(nonce: str) -> dict:
                ts = int(time.time())
                tok = h.token(node_a)
                return {
                    "type": "hello",
                    "version": 1,
                    "node_key": encode_base64url(node_a.public_raw),
                    "mode": "own_library",
                    "token": tok,
                    "timestamp": ts,
                    "challenge": nonce,
                    "signature": encode_base64url(
                        node_a.sign(
                            hello_signing_text(
                                tok, ts, decode_base64url(nonce, 32)
                            )
                        )
                    ),
                }

            first, nonce1 = await raw_client()
            captured = hello_frame(nonce1)
            await first.send(captured)
            assert (await first.next())["type"] == "welcome"
            await first.close()

            # The identical captured hello on a new connection: its
            # nonce belongs to the dead connection, so it is refused.
            second, nonce2 = await raw_client()
            assert nonce2 != nonce1
            await second.send(captured)
            refused = await second.next()
            assert refused == {"type": "refused", "reason": "auth"}
            await second.expect_closed()
            assert h.relay.presence == {}

            # A hello signed over the fresh challenge is accepted.
            third, nonce3 = await raw_client()
            await third.send(hello_frame(nonce3))
            assert (await third.next())["type"] == "welcome"

    asyncio.run(scenario())


# -- presence --------------------------------------------------------------


def test_presence_removed_on_disconnect(pool_private, pool_public, node_a, node_b, node_c):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await b.close()
            for _ in range(50):
                if node_b.node_id not in h.relay.presence:
                    break
                await asyncio.sleep(0.05)
            assert node_b.node_id not in h.relay.presence
            c = await h.connect_ok(node_c, key_id="key-3")
            assert c.welcome["peers"] == 1  # only node_a

    asyncio.run(scenario())


def test_reconnect_evicts_stale_connection(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            first = await h.connect_ok(node_a)
            second = await h.connect_ok(node_a)
            await first.expect_closed()
            entries = [n for n in h.relay.presence if n == node_a.node_id]
            assert len(entries) == 1
            assert second.welcome["type"] == "welcome"

    asyncio.run(scenario())


# -- routing ---------------------------------------------------------------


def test_query_fans_out_to_others_not_asker(pool_private, pool_public, node_a, node_b, node_c):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            c = await h.connect_ok(node_c, key_id="key-3")
            await a.send(exact_query(qid(7)))
            for peer in (b, c):
                got = await peer.next()
                assert got["type"] == "query" and got["query_id"] == qid(7)
            await a.expect_silence()

    asyncio.run(scenario())


def test_leech_node_gets_no_queries_and_cannot_delay_close(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness(pool_private, pool_public, window_seconds=30.0) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            leech = await h.connect_ok(node_c, key_id="key-3", mode="leech")
            await a.send(exact_query(qid(15)))
            got = await b.next()
            assert got["type"] == "query"
            await leech.expect_silence()
            oq = h.relay.open_queries[qid(15)]
            assert oq.expected == {node_b.node_id}
            # The only expected responder signalling done closes the
            # window early; the leech's silence must not hold it open.
            await b.send({"type": "done", "query_id": qid(15)})
            assert (await a.next()) == {"type": "done", "query_id": qid(15)}
            assert oq.all_responded_at is not None

    asyncio.run(scenario())


class _FakeWS:
    """A stand-in peer socket. With hang=True its send never returns,
    like a peer that stopped reading."""

    def __init__(self, hang: bool = False) -> None:
        self.sent: list[str] = []
        self._hang = hang

    async def send(self, payload) -> None:
        if self._hang:
            await asyncio.Event().wait()
        self.sent.append(payload)

    async def close(self, code=None) -> None:
        pass


def test_fanout_not_blocked_by_stuck_peer(
    pool_public, store, node_a, node_b, node_c
):
    async def scenario():
        relay = Relay(store, pool_public, RelayConfig(send_timeout_seconds=0.2))

        def add(node, ws) -> _Connection:
            conn = _Connection(ws=ws)
            conn.node_id = node.node_id
            conn.mode = "own_library"
            relay.presence[node.node_id] = conn
            return conn

        asker_ws = _FakeWS()
        asker = add(node_a, asker_ws)
        add(node_b, _FakeWS(hang=True))  # never reads, send hangs
        good_ws = _FakeWS()
        add(node_c, good_ws)
        query = Query(
            query_id=qid(81), kind="track", mode="exact", subjects=("soul_x",)
        )
        # Sequential sends could park on the stuck peer indefinitely;
        # the fan-out must complete on the per-send timeout instead.
        await asyncio.wait_for(relay._route_query(asker, query), timeout=5)
        assert len(good_ws.sent) == 1
        assert json.loads(good_ws.sent[0])["query_id"] == qid(81)
        assert asker_ws.sent == []

    asyncio.run(scenario())


def test_answer_routes_only_to_asker(pool_private, pool_public, node_a, node_b, node_c):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            c = await h.connect_ok(node_c, key_id="key-3")
            await a.send(exact_query(qid(8)))
            await b.next()
            await c.next()
            batch = {"claims": [{"subject": "soul_example"}], "sig": "kept-opaque"}
            await b.send(answer(qid(8), batch))
            got = await a.next()
            assert got["type"] == "answer" and got["batch"] == batch
            await c.expect_silence()
            await b.expect_silence()

    asyncio.run(scenario())


def test_done_forwarded_to_asker(pool_private, pool_public, node_a, node_b, node_c):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            c = await h.connect_ok(node_c, key_id="key-3")
            await a.send(exact_query(qid(9)))
            await b.next()
            await c.next()
            await b.send({"type": "done", "query_id": qid(9)})
            got = await a.next()
            assert got == {"type": "done", "query_id": qid(9)}
            await c.expect_silence()

    asyncio.run(scenario())


def test_answer_from_node_query_was_not_routed_to_rejected(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(16)))
            await b.next()
            # node_c connects after the fan-out: the query was never
            # routed to it, so its answer and done are uninvited.
            c = await h.connect_ok(node_c, key_id="key-3")
            await c.send(answer(qid(16)))
            await a.expect_silence(0.6)
            await c.send({"type": "done", "query_id": qid(16)})
            await a.expect_silence(0.6)
            assert h.relay.node_state[node_c.node_id].violations == 2
            # ...while the routed responder's answer still lands.
            await b.send(answer(qid(16)))
            assert (await a.next())["type"] == "answer"

    asyncio.run(scenario())


def test_asker_disconnect_orphans_query_answers_drop_silently(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness(
            pool_private, pool_public, window_seconds=30.0, grace_seconds=30.0
        ) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(17)))
            await b.next()
            await a.close()
            for _ in range(50):
                if node_a.node_id not in h.relay.presence:
                    break
                await asyncio.sleep(0.05)
            oq = h.relay.open_queries[qid(17)]
            assert oq.orphaned_at is not None
            # In-window traffic for the dead asker's query is dropped
            # silently: no violation for the responder.
            await b.send(answer(qid(17)))
            await b.send({"type": "done", "query_id": qid(17)})
            await asyncio.sleep(0.3)
            assert h.relay.node_state[node_b.node_id].violations == 0
            # The purge sweeper removes the tombstone on the same 60s
            # margin as expired queries (stand the deadline in for the
            # margin having passed, then let any dispatch drive the
            # sweep).
            oq.deadline = time.monotonic() - 3600
            await b.send({"type": "done", "query_id": qid(999)})
            await asyncio.sleep(0.2)
            assert qid(17) not in h.relay.open_queries

    asyncio.run(scenario())


def test_answer_for_unknown_query_dropped(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await b.send(answer(qid(404)))
            await a.expect_silence(0.6)

    asyncio.run(scenario())


def test_self_answer_dropped(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(10)))
            await b.next()
            await a.send(answer(qid(10)))  # asker answering its own query
            await b.expect_silence()
            await a.expect_silence()

    asyncio.run(scenario())


def test_late_answer_dropped(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(
            pool_private, pool_public, window_seconds=0.25, grace_seconds=0.1
        ) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(11)))
            await b.next()
            await asyncio.sleep(0.6)  # past window + grace
            await b.send(answer(qid(11)))
            await a.expect_silence(0.6)

    asyncio.run(scenario())


def test_grace_period_answer_still_lands(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(
            pool_private, pool_public, window_seconds=0.2, grace_seconds=2.0
        ) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(12)))
            await b.next()
            await asyncio.sleep(0.45)  # past the window, inside grace
            await b.send(answer(qid(12)))
            got = await a.next()
            assert got["type"] == "answer"

    asyncio.run(scenario())


# -- budgets ----------------------------------------------------------------


def test_answer_byte_cap_drops_overflow(pool_private, pool_public, node_a, node_b):
    batch = {"pad": "x" * 80}
    frame = json.dumps(answer(qid(13), batch))
    cap = len(frame.encode()) + 10  # room for exactly one such answer

    async def scenario():
        async with Harness(pool_private, pool_public, answer_cap_bytes=cap) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(13)))
            await b.next()
            await b.send(answer(qid(13), batch))
            got = await a.next()
            assert got["type"] == "answer"
            await b.send(answer(qid(13), batch))  # second would pass the cap
            await a.expect_silence(0.6)

    asyncio.run(scenario())


def test_query_budget_exceeded_drops(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public, queries_per_hour=2) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            for n in (21, 22, 23):
                await a.send(exact_query(qid(n)))
            assert (await b.next())["query_id"] == qid(21)
            assert (await b.next())["query_id"] == qid(22)
            await b.expect_silence(0.6)

    asyncio.run(scenario())


def test_answer_budget_exceeded_drops(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public, answers_per_hour=1) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(31)))
            await a.send(exact_query(qid(32)))
            await b.next()
            await b.next()
            await b.send(answer(qid(31)))
            assert (await a.next())["type"] == "answer"
            await b.send(answer(qid(32)))  # over the hourly answer budget
            await a.expect_silence(0.6)

    asyncio.run(scenario())


def test_persistent_abuse_closes_connection(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public, max_violations=2) as h:
            await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            for n in (41, 42, 43):  # three unknown-query answers: 3 > 2
                await b.send(answer(qid(n)))
            await b.expect_closed()

    asyncio.run(scenario())


def test_query_budget_survives_reconnect(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public, queries_per_hour=2) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            for n in (61, 62):
                await a.send(exact_query(qid(n)))
                assert (await b.next())["query_id"] == qid(n)
            # Reconnect (which evicts the stale socket): the hourly
            # budget belongs to the node, not the connection, so the
            # third query inside the hour is still over budget.
            a2 = await h.connect_ok(node_a)
            await a2.send(exact_query(qid(63)))
            await b.expect_silence(0.6)
            state = h.relay.node_state[node_a.node_id]
            assert state.violations == 1

    asyncio.run(scenario())


def test_dispatch_stops_after_closing_violation(pool_public, store, node_b):
    async def scenario():
        relay = Relay(store, pool_public, RelayConfig(max_violations=0))
        conn = _Connection(ws=_FakeWS())
        conn.node_id = node_b.node_id
        conn.mode = "own_library"
        relay.presence[node_b.node_id] = conn
        # One violation over the (zero) limit closes the connection;
        # dispatch must report the close so the handler loop stops
        # processing further messages on it.
        raw = json.dumps(answer(qid(91)))
        assert await relay._dispatch(conn, raw) is False
        assert relay.node_state[node_b.node_id].violations == 1

    asyncio.run(scenario())


def test_logs_carry_no_node_ids(pool_private, pool_public, node_a, node_b, caplog):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await b.send(answer(qid(95)))  # unknown query: a logged violation
            await asyncio.sleep(0.3)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert caplog.records, "expected connect/violation log records"
    for record in caplog.records:
        assert not hasattr(record, "node"), record.__dict__
        assert node_a.node_id[:12] not in record.getMessage()
        assert node_b.node_id[:12] not in record.getMessage()


def test_violation_count_survives_reconnect(pool_private, pool_public, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            b = await h.connect_ok(node_b, key_id="key-2")
            await b.send(answer(qid(71)))
            await b.send(answer(qid(72)))
            b2 = await h.connect_ok(node_b, key_id="key-2")
            await b2.send(answer(qid(73)))
            await b2.expect_silence(0.3)
            assert h.relay.node_state[node_b.node_id].violations == 3

    asyncio.run(scenario())

"""Relay integration tests for protocol v2 (DESIGN-METADATA.md
section 10, step 5): version negotiation and welcome contents,
identity/mode-bound hello signatures, cross-version answer
transforms, the sender stamp, byte budgets, violation decay,
late-answer-no-charge, the complete signal, in-band re-auth,
revocation carriage and push, aggregate counters, and node_state
pruning. Same harness style as test_server.py (v1)."""

from __future__ import annotations

import asyncio
import json
import time

from conftest import NodeIdentity
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from metasync_relay.revocations import RevocationFeed
from metasync_relay.server import (
    hello_v2_signing_text,
    reauth_signing_text,
)
from metasync_relay.tokens import decode_base64url, encode_base64url
from test_server import Harness, answer, exact_query, qid


class Harness2(Harness):
    """Harness plus a v2 hello. The relay identity the signature
    binds is the relay's CONFIGURED origin (gate M5: v2 refuses
    without one), set here to the canonical dialed origin — the
    harness models a correctly configured relay."""

    def __init__(self, pool_private, pool_public, **config_overrides) -> None:
        super().__init__(pool_private, pool_public, **config_overrides)
        if not self.relay.config.relay_origin:
            import dataclasses

            self.relay.config = dataclasses.replace(
                self.relay.config, relay_origin=self.identity
            )

    @property
    def identity(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def connect2(
        self,
        node: NodeIdentity,
        key_id: str = "key-1",
        token: str | None = None,
        mode: str = "own_library",
        version_min: int = 1,
        version_max: int = 2,
        sign_identity: str | None = None,
        sign_mode: str | None = None,
        capabilities: dict | None = None,
    ):
        from websockets.asyncio.client import connect as ws_connect

        ws = await ws_connect(f"ws://127.0.0.1:{self.port}")
        from test_server import Client

        client = Client(ws)
        self.clients.append(client)
        challenge = await client.next()
        assert challenge["type"] == "challenge", challenge
        nonce = challenge["nonce"]
        ts = int(time.time())
        tok = self.token(node, key_id) if token is None else token
        sig = encode_base64url(
            node.sign(
                hello_v2_signing_text(
                    relay_identity=(
                        sign_identity if sign_identity is not None else self.identity
                    ),
                    mode=sign_mode if sign_mode is not None else mode,
                    version_min=version_min,
                    version_max=version_max,
                    token=tok,
                    timestamp=ts,
                    challenge=decode_base64url(nonce, 32),
                )
            )
        )
        hello = {
            "type": "hello",
            "version_min": version_min,
            "version_max": version_max,
            "node_key": encode_base64url(node.public_raw),
            "mode": mode,
            "token": tok,
            "timestamp": ts,
            "challenge": nonce,
            "signature": sig,
        }
        if capabilities is not None:
            hello["capabilities"] = capabilities
        await client.send(hello)
        return client

    async def connect2_ok(self, node: NodeIdentity, **kw):
        client = await self.connect2(node, **kw)
        welcome = await client.next()
        assert welcome["type"] == "welcome", welcome
        client.welcome = welcome
        return client


def batch_of(node: NodeIdentity, marker: str = "m") -> dict:
    return {
        "batch": {
            "header": {"version": 1, "node": encode_base64url(node.public_raw)},
            "claims": [{"marker": marker}],
        },
        "sig": "c2ln",
    }


# -- negotiation, welcome, identity binding ----------------------------------


def test_v2_welcome_carries_version_and_identity(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect2_ok(node_a)
            welcome = client.welcome
            assert welcome["version"] == 2
            assert welcome["relay"] == h.identity
            # S3: the welcome states the relay's answer grace, so
            # the asker's post-complete grace can match the relay's
            # admission end.
            assert welcome["grace_seconds"] == h.relay.config.grace_seconds
            assert welcome["peers"] == 0
            # No revocation feed is configured, so there is no list
            # to carry: the field is present only when a verified
            # list exists (carriage is covered by the revocation
            # tests below); absence reads as the empty list.
            assert "revocations" not in welcome

    asyncio.run(scenario())


def test_v2_hello_for_another_relay_identity_is_refused(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect2(node_a, sign_identity="ws://127.0.0.1:9999")
            refused = await client.next()
            assert refused["type"] == "refused" and refused["reason"] == "auth"

    asyncio.run(scenario())


def test_v2_hello_with_wrong_mode_signature_is_refused(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect2(node_a, mode="own_library", sign_mode="full")
            refused = await client.next()
            assert refused["type"] == "refused" and refused["reason"] == "auth"

    asyncio.run(scenario())


def test_v2_hello_refused_without_configured_relay_origin(
    pool_private, pool_public, node_a
):
    """M5: a relay with no RELAY_ORIGIN refuses v2 hellos outright
    (its identity binding would otherwise be computed from the
    client's own Host headers and protect nothing) — while v1,
    which has no identity binding, still connects."""
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            import dataclasses

            h.relay.config = dataclasses.replace(
                h.relay.config, relay_origin=""
            )
            client = await h.connect2(node_a)
            refused = await client.next()
            assert refused["type"] == "refused" and refused["reason"] == "auth"
            assert node_a.node_id not in h.relay.presence
            v1 = await h.connect_ok(node_a)
            assert v1.welcome["type"] == "welcome"

    asyncio.run(scenario())


def test_v1_hello_gets_the_legacy_welcome_shape(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            # The v1 hello form still negotiates a v1 session on the
            # v2 relay, and its welcome is the legacy shape: no
            # version, identity, or revocation fields a v1 client
            # would not understand.
            client = await h.connect_ok(node_a)
            assert client.welcome["type"] == "welcome"
            assert "version" not in client.welcome
            assert "revocations" not in client.welcome

    asyncio.run(scenario())


# -- cross-version transforms, stamp, complete -------------------------------


def test_v2_asker_v1_answerer_batch_wrapped_stamped_completed(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")  # v1 answerer
            await a.send(exact_query(qid(41)))
            got = await b.next()
            assert got["type"] == "query" and "reply_key" not in got
            await b.send(answer(qid(41), batch=batch_of(node_b)))
            msg = await a.next()
            assert msg["type"] == "answer"
            assert msg["batches"] == [batch_of(node_b)]  # wrapped for v2
            assert msg["stamp"] == {"from": node_b.node_id, "query_id": qid(41)}
            await b.send({"type": "done", "query_id": qid(41)})
            complete = await a.next()
            assert complete == {"type": "complete", "query_id": qid(41)}

    asyncio.run(scenario())


def test_v1_asker_v2_answerer_packed_unpacked_done_forwarded(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)  # v1 asker
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(42)))
            got = await b.next()
            assert got["type"] == "query"
            b1, b2 = batch_of(node_b, "1"), batch_of(node_b, "2")
            await b.send(
                {"type": "answer", "query_id": qid(42), "batches": [b1, b2]}
            )
            first = await a.next()
            second = await a.next()
            assert first["batch"] == b1 and second["batch"] == b2
            assert first["stamp"]["from"] == node_b.node_id
            await b.send({"type": "done", "query_id": qid(42)})
            done = await a.next()
            assert done == {"type": "done", "query_id": qid(42)}

    asyncio.run(scenario())


def test_complete_sent_when_window_elapses(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(
            pool_private, pool_public, window_seconds=0.3, grace_seconds=0.3
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(43)))
            await b.next()
            # b never answers and never sends done: the window ending
            # must still complete the query for the asker.
            complete = await a.next(timeout=5)
            assert complete == {"type": "complete", "query_id": qid(43)}

    asyncio.run(scenario())


def test_duplicate_done_is_deduped(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)  # v1 asker sees forwarded dones
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(44)))
            await b.next()
            await b.send({"type": "done", "query_id": qid(44)})
            assert (await a.next())["type"] == "done"
            await b.send({"type": "done", "query_id": qid(44)})  # repeat
            await a.expect_silence(0.5)
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0

    asyncio.run(scenario())


# -- byte budgets, late answers, decay ----------------------------------------


def test_per_node_per_query_byte_cap_is_silent(pool_private, pool_public, node_a, node_b):
    big = batch_of(node_b)
    big["batch"]["claims"] = [{"marker": "x" * 400}]
    size = len(json.dumps({"type": "answer", "query_id": qid(45), "batch": big}).encode())

    async def scenario():
        async with Harness2(
            pool_private, pool_public, answer_node_query_cap_bytes=size + 10
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(45)))
            await b.next()
            await b.send({"type": "answer", "query_id": qid(45), "batch": big})
            assert (await a.next())["type"] == "answer"  # first fits exactly
            # An answer alone does NOT close the window (section 10):
            # no complete may arrive until b signals done.
            await a.expect_silence(0.5)
            await b.send({"type": "done", "query_id": qid(45)})
            complete = await a.next()  # b done: window closed early
            assert complete == {"type": "complete", "query_id": qid(45)}
            await b.send({"type": "answer", "query_id": qid(45), "batch": big})
            await a.expect_silence(0.5)  # second exceeds the per-query cap
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0  # silent: no strike
            # MF-2: the cap-dropped answer was not metered either —
            # only the one admitted answer is on the hourly books.
            assert state is not None and len(state.answer_bytes) == 1

    asyncio.run(scenario())


def test_hourly_byte_budget_strikes(pool_private, pool_public, node_a, node_b):
    frame = {"type": "answer", "query_id": qid(46), "batch": batch_of(node_b)}
    size = len(json.dumps(frame).encode())

    async def scenario():
        async with Harness2(
            pool_private, pool_public, answer_bytes_per_hour=size + 10
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(46)))
            await a.send(exact_query(qid(47)))
            await b.next()
            await b.next()
            await b.send(frame)
            assert (await a.next())["type"] == "answer"
            await b.send({"type": "done", "query_id": qid(46)})
            complete = await a.next()  # b done: window closed early
            assert complete == {"type": "complete", "query_id": qid(46)}
            await b.send({**frame, "query_id": qid(47)})  # over the hourly bytes
            await a.expect_silence(0.5)
            assert h.relay.node_state[node_b.node_id].violations == 1

    asyncio.run(scenario())


def test_late_answer_never_charged(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(
            pool_private, pool_public, window_seconds=0.2, grace_seconds=0.2
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(48)))
            await b.next()
            await asyncio.sleep(0.8)  # window + grace long gone
            complete = await a.next()  # the window ending completed it
            assert complete == {"type": "complete", "query_id": qid(48)}
            await b.send(answer(qid(48), batch=batch_of(node_b)))
            await a.expect_silence(0.4)
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0
            # and no bytes were metered against the answerer either
            assert state is None or len(state.answer_bytes) == 0

    asyncio.run(scenario())


def test_violations_decay_with_fake_clocks(pool_private, pool_public, node_a, node_b):
    from metasync_relay.config import RelayConfig
    from metasync_relay.server import Relay

    mono = [1000.0]
    wall = [1_700_000_000.0]
    relay = Relay(
        pool_public,
        RelayConfig(max_violations=10, violation_decay_seconds=60),
        wall_clock=lambda: wall[0],
        monotonic=lambda: mono[0],
    )

    async def scenario():
        conn = _fake_conn(relay, node_b)
        for _ in range(2):
            raw = json.dumps(answer(qid(49)))
            assert await relay._dispatch(conn, raw) is True
        assert relay.node_state[node_b.node_id].violations == 2
        mono[0] += 61  # both strikes age out
        assert relay.node_state[node_b.node_id].violations == 0

    asyncio.run(scenario())


def _fake_conn(relay, node):
    from metasync_relay.server import _Connection

    class _FakeWS:
        async def send(self, payload) -> None:
            pass

        async def close(self, code=None) -> None:
            pass

    conn = _Connection(ws=_FakeWS())
    conn.node_id = node.node_id
    conn.mode = "own_library"
    conn.version = 2
    relay.presence[node.node_id] = conn
    return conn


# -- stalled asker ------------------------------------------------------------


def test_stalled_asker_does_not_block_other_answers(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness2(
            pool_private, pool_public, send_timeout_seconds=0.3, window_seconds=30
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            c = await h.connect2_ok(node_c, key_id="key-3")
            # Stall a: stop pumping its socket by cancelling the reader.
            a.task.cancel()
            await a.send(exact_query(qid(50)))
            await b.next()
            await c.next()
            await b.send(answer(qid(50), batch=batch_of(node_b)))
            # c's query path is unaffected by a's stall: c can ask b.
            await c.send(exact_query(qid(51)))
            got = await b.next()
            assert got["query_id"] == qid(51)
            await b.send(answer(qid(51), batch=batch_of(node_b)))
            msg = await c.next(timeout=5)
            assert msg["type"] == "answer" and msg["stamp"]["from"] == node_b.node_id

    asyncio.run(scenario())


# -- re-auth -------------------------------------------------------------------


def test_reauth_keeps_session_alive_past_expiry(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(
            pool_private, pool_public, reauth_grace_seconds=2.0
        ) as h:
            client = await h.connect2_ok(
                node_a, token=h.token(node_a, ttl=2)
            )
            await asyncio.sleep(2.3)  # past expiry, inside the grace
            # Without a re-auth the sweeper closes the session at
            # expiry + grace; send one (fresh 24h token) inside the
            # grace and prove the session survives on it.
            fresh = h.token(node_a)
            ts = int(time.time())
            await client.send(
                {
                    "type": "reauth",
                    "token": fresh,
                    "timestamp": ts,
                    "signature": encode_base64url(
                        node_a.sign(
                            reauth_signing_text(
                                relay_identity=h.identity, token=fresh,
                                timestamp=ts)
                        )
                    ),
                }
            )
            ok = await client.next(timeout=3)
            assert ok["type"] == "reauth_ok" and ok["exp"] > int(time.time())
            await asyncio.sleep(1.0)
            assert node_a.node_id in h.relay.presence  # still alive

    asyncio.run(scenario())


async def _send_reauth(h, client, node, identity=None):
    """One well-formed re-auth on an open v2 connection."""
    fresh = h.token(node)
    ts = int(time.time())
    await client.send(
        {
            "type": "reauth",
            "token": fresh,
            "timestamp": ts,
            "signature": encode_base64url(
                node.sign(
                    reauth_signing_text(
                        relay_identity=identity or h.identity,
                        token=fresh,
                        timestamp=ts,
                    )
                )
            ),
        }
    )


def test_reauth_signed_for_another_relay_is_refused(
    pool_private, pool_public, node_a
):
    """S5: the re-auth signature binds the relay's identity, so a
    re-auth captured at relay B cannot be replayed at relay A."""
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect2_ok(node_a)
            await _send_reauth(h, client, node_a,
                               identity="wss://other-relay.example:443")
            # Refused: the refused frame, or the close that races it
            # (the relay enqueues the refusal and closes — either
            # signal ends the session), and the seat is gone.
            reply = await client.next(timeout=3)
            assert reply is None or reply["type"] == "refused", reply
            await client.expect_closed()
            for _ in range(50):
                if node_a.node_id not in h.relay.presence:
                    break
                await asyncio.sleep(0.1)
            assert node_a.node_id not in h.relay.presence

    asyncio.run(scenario())


def test_reauth_rate_limit_refuses_and_strikes(
    pool_private, pool_public, node_a
):
    """M4: re-auths past the per-connection ceiling are refused and
    struck — a healthy client re-auths ~once per token lifetime, so
    the ceiling only ever meets a spinning or hostile one."""
    async def scenario():
        async with Harness2(
            pool_private, pool_public, reauth_max_per_window=3
        ) as h:
            client = await h.connect2_ok(node_a)
            for _ in range(3):
                await _send_reauth(h, client, node_a)
                ok = await client.next(timeout=3)
                assert ok["type"] == "reauth_ok", ok
            await _send_reauth(h, client, node_a)
            reply = await client.next(timeout=3)
            assert reply["type"] == "refused", reply
            await asyncio.sleep(0.3)
            assert h.relay.node_state[node_a.node_id].violations >= 1

    asyncio.run(scenario())


def test_session_without_reauth_closes_after_expiry_grace(
    pool_private, pool_public, node_a
):
    async def scenario():
        async with Harness2(
            pool_private, pool_public, reauth_grace_seconds=0.3
        ) as h:
            await h.connect2_ok(node_a, token=h.token(node_a, ttl=1))
            for _ in range(60):
                if node_a.node_id not in h.relay.presence:
                    break
                await asyncio.sleep(0.1)
            assert node_a.node_id not in h.relay.presence

    asyncio.run(scenario())


def test_reauth_on_v1_connection_is_a_violation(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect_ok(node_a)  # v1 session
            fresh = h.token(node_a)
            ts = int(time.time())
            await client.send(
                {
                    "type": "reauth",
                    "token": fresh,
                    "timestamp": ts,
                    "signature": encode_base64url(
                        node_a.sign(
                            reauth_signing_text(
                                relay_identity=h.identity, token=fresh,
                                timestamp=ts)
                        )
                    ),
                }
            )
            await asyncio.sleep(0.4)
            assert h.relay.node_state[node_a.node_id].violations == 1

    asyncio.run(scenario())


# -- revocation -----------------------------------------------------------------

ISSUER_ID = "issuer-1"


def _signed_revocation_list(issuer_private, seq: int, keys: list[str]) -> dict:
    from metasync_relay.revocations import list_signing_text

    sig = encode_base64url(
        issuer_private.sign(list_signing_text(ISSUER_ID, seq, 1700000000, keys))
    )
    return {
        "issuer": ISSUER_ID,
        "seq": seq,
        "issued_at": 1700000000,
        "keys": list(keys),
        "sig": sig,
    }


def _feed(issuer_private, payloads: list[dict]) -> RevocationFeed:
    from metasync_relay.revocations import RevocationFeed as Feed

    queue = list(payloads)

    def fetch(url: str) -> bytes:
        return json.dumps(queue.pop(0)).encode()

    issuer_public: Ed25519PublicKey = issuer_private.public_key()
    return Feed(ISSUER_ID, issuer_public, "https://issuer.example/revoked.json", fetch=fetch)


def test_revocation_list_in_welcome_push_and_stale_ignored(
    pool_private, pool_public, node_a, node_b
):
    issuer_private = Ed25519PrivateKey.generate()

    async def scenario():
        payload_v1 = _signed_revocation_list(issuer_private, 1, [node_b.node_id])
        feed = _feed(issuer_private, [payload_v1])
        async with Harness2(pool_private, pool_public) as h:
            h.relay.revocations = feed
            await h.relay.refresh_revocations()
            a = await h.connect2_ok(node_a)
            assert a.welcome["revocations"]["seq"] == 1
            assert a.welcome["revocations"]["keys"] == [node_b.node_id]
            # A revoked node is refused at hello.
            refused_client = await h.connect2(node_b, key_id="key-2")
            refused = await refused_client.next()
            assert refused["type"] == "refused" and refused["reason"] == "auth"
            # A newer list pushes to connected v2 nodes.
            payload_v2 = _signed_revocation_list(issuer_private, 2, [])
            feed._fetch = lambda url: json.dumps(payload_v2).encode()  # type: ignore[method-assign]
            await h.relay.refresh_revocations()
            push = await a.next()
            assert push["type"] == "revocations" and push["seq"] == 2
            # An older sequence never replaces a newer one.
            feed._fetch = lambda url: json.dumps(payload_v1).encode()  # type: ignore[method-assign]
            changed = await h.relay.refresh_revocations()
            assert changed is False
            await a.expect_silence(0.4)

    asyncio.run(scenario())


def test_revocation_bad_signature_ignored(pool_private, pool_public, node_a):
    issuer_private = Ed25519PrivateKey.generate()
    other_private = Ed25519PrivateKey.generate()

    async def scenario():
        payload = _signed_revocation_list(other_private, 5, [node_a.node_id])
        feed = _feed(issuer_private, [payload])
        async with Harness2(pool_private, pool_public) as h:
            h.relay.revocations = feed
            changed = await h.relay.refresh_revocations()
            assert changed is False
            a = await h.connect2_ok(node_a)  # not revoked: list was dropped
            assert "revocations" not in a.welcome

    asyncio.run(scenario())


# -- counters, pruning, forward-compat -----------------------------------------


def test_counters_snapshot_after_round_trip(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(52)))
            await b.next()
            await b.send(answer(qid(52), batch=batch_of(node_b)))
            await a.next()
            snap = h.relay.counters_snapshot()
            assert snap["queries_routed"] >= 1
            assert snap["answers_forwarded"] >= 1
            assert snap["bytes_in"] > 0 and snap["bytes_out"] > 0
            assert snap["connections_active"] == 2
            assert snap["share_answered"] == 1.0
            assert snap["answers_per_query"] >= 1.0
            # Aggregate only: no per-node or content keys anywhere.
            assert not any("node" in key for key in snap)
            assert not any("claim" in key or "subject" in key for key in snap)

    asyncio.run(scenario())


def test_node_state_pruned_for_long_gone_nodes(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            # Give node_b a strike so it HAS state, then disconnect it.
            raw = json.dumps(answer(qid(53)))
            conn = h.relay.presence[node_b.node_id]
            await h.relay._dispatch(conn, raw)
            assert h.relay.node_state[node_b.node_id].violations == 1
            await b.close()
            for _ in range(60):
                if node_b.node_id not in h.relay.presence:
                    break
                await asyncio.sleep(0.05)
            # The strike keeps the state while it is live (decay is
            # time-based), and the sweep prunes only IDLE state — a
            # live strike is not idle. Force the strike stale and sweep.
            state = h.relay.node_state[node_b.node_id]
            state._strikes.clear()  # as if the strike had fully decayed
            h.relay._sweep_once()
            assert node_b.node_id not in h.relay.node_state
            await a.close()

    asyncio.run(scenario())


def test_unknown_message_type_from_v2_is_ignored(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await b.send({"type": "future_thing", "payload": {"x": 1}})
            await asyncio.sleep(0.4)
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0
            # and the connection still works afterwards
            await a.send(exact_query(qid(54)))
            assert (await b.next())["type"] == "query"

    asyncio.run(scenario())


def test_v2_unknown_fields_route_and_behave_identically(
    pool_private, pool_public, node_a, node_b
):
    # Forward compatibility end to end (section 10): extra fields on
    # a query and on an answer are ignored — no violation, the
    # forwarded query is the canonical shape without them, and the
    # asker receives the answer stamped by the relay as usual.
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            query = exact_query(qid(55))
            query["trace_id"] = "from-a-future-client"
            query["priority"] = 3
            await a.send(query)
            got = await b.next()
            assert got["type"] == "query"
            assert "trace_id" not in got and "priority" not in got
            ans = answer(qid(55), batch=batch_of(node_b))
            ans["future_field"] = {"nested": [1, 2, 3]}
            await b.send(ans)
            msg = await a.next()
            assert msg["type"] == "answer"
            assert msg["batches"] == [batch_of(node_b)]
            assert msg["stamp"] == {"from": node_b.node_id, "query_id": qid(55)}
            assert "future_field" not in msg
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0

    asyncio.run(scenario())


# -- hostile-review folds: window/metering/queue accounting -------------------


def test_answer_alone_never_completes_and_done_in_grace_never_extends_admission(
    pool_public, node_a, node_b, node_c
):
    """MF-1: complete fires only on all-done or the deadline; admission
    end is complete_time + grace, fixed — a done landing inside the
    grace period must not re-base it and admit later answers."""
    from metasync_relay.config import RelayConfig
    from metasync_relay.server import Relay

    mono = [1000.0]
    wall = [1_700_000_000.0]
    relay = Relay(
        pool_public,
        RelayConfig(window_seconds=6.0, grace_seconds=10.0),
        wall_clock=lambda: wall[0],
        monotonic=lambda: mono[0],
    )

    async def scenario():
        asker = _fake_conn(relay, node_a)
        peer_b = _fake_conn(relay, node_b)
        peer_c = _fake_conn(relay, node_c)
        for conn in (asker, peer_b, peer_c):
            conn.token_exp = wall[0] + 100_000
        raw_query = json.dumps(exact_query(qid(60)))
        assert await relay._dispatch(asker, raw_query) is True
        oq = relay.open_queries[qid(60)]
        assert set(oq.expected) == {node_b.node_id, node_c.node_id}
        # t=1001: an answer from b is not a done — the window stays open.
        mono[0] = 1001.0
        raw_answer_b = json.dumps(answer(qid(60), batch=batch_of(node_b)))
        assert await relay._dispatch(peer_b, raw_answer_b) is True
        assert oq.all_responded_at is None and not oq.complete_sent
        # t=1002: b's done still leaves c outstanding — no completion.
        mono[0] = 1002.0
        raw_done = json.dumps({"type": "done", "query_id": qid(60)})
        assert await relay._dispatch(peer_b, raw_done) is True
        assert oq.all_responded_at is None and not oq.complete_sent
        # t=1006 (the deadline): the sweeper completes by timeout, and
        # the completion time is the deadline — fixed from here on.
        mono[0] = 1006.0
        relay._sweep_once()
        assert oq.complete_sent and oq.complete_time == 1006.0
        assert oq.admission_end() == 1016.0
        # t=1010, inside grace: c's done is recorded but must NOT
        # re-base the admission end to 1010 + grace.
        mono[0] = 1010.0
        assert await relay._dispatch(peer_c, raw_done) is True
        assert oq.admission_end() == 1016.0
        # t=1017: past the fixed close + grace — c's answer is late:
        # dropped silently, no strike, no bytes metered.
        mono[0] = 1017.0
        raw_answer_c = json.dumps(answer(qid(60), batch=batch_of(node_c)))
        assert await relay._dispatch(peer_c, raw_answer_c) is True
        state_c = relay.node_state.get(node_c.node_id)
        # Dropped before any metering: c may have no state entry at all.
        assert state_c is None or (
            state_c.violations == 0 and state_c.answer_bytes_total() == 0
        )

    asyncio.run(scenario())


def test_total_cap_dropped_answers_are_not_metered(
    pool_private, pool_public, node_a, node_b
):
    """MF-2: bytes dropped by the query's total cap are not charged to
    the answerer's hourly budget (section 10: hitting the total cap
    is a silent drop, not a charge against whoever arrived last)."""
    frame = {"type": "answer", "query_id": qid(61), "batch": batch_of(node_b)}
    size = len(json.dumps(frame).encode())

    async def scenario():
        async with Harness2(pool_private, pool_public, answer_cap_bytes=size + 10) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(61)))
            await b.next()
            await b.send(frame)
            assert (await a.next())["type"] == "answer"  # first fills the cap
            await b.send(frame)  # second hits the total cap: silent drop
            await a.expect_silence(0.5)
            state = h.relay.node_state[node_b.node_id]
            assert state.violations == 0
            # Only the admitted answer is on the hourly books.
            assert len(state.answer_bytes) == 1
            assert state.answer_bytes_total() == size

    asyncio.run(scenario())


def test_answer_bytes_total_is_a_running_total(pool_public):
    """MF-3: the hourly byte total is maintained incrementally and
    stays exact across pruning (never re-summed per answer)."""
    from metasync_relay.server import _NodeState

    now = [0.0]
    state = _NodeState(_now=lambda: now[0])
    state.record_answer_bytes(0.0, 100)
    state.record_answer_bytes(10.0, 50)
    assert state.answer_bytes_total() == 150
    assert state._answer_bytes_total == sum(s for _t, s in state.answer_bytes)
    # Rolling past the hour prunes the old entries AND their bytes.
    state.record_answer_bytes(3700.0, 25)
    assert state.answer_bytes_total() == 25
    assert state._answer_bytes_total == sum(s for _t, s in state.answer_bytes)


def test_send_queue_is_bounded_in_bytes(pool_public, node_a):
    """SF-1: the send queue sheds load at a byte bound, not just a
    message count — 128 max-size frames must not buffer behind a
    slow asker."""
    from metasync_relay.config import RelayConfig
    from metasync_relay.server import Relay, _Connection

    async def scenario():
        relay = Relay(
            pool_public, RelayConfig(send_queue_messages=1000, send_queue_bytes=100)
        )
        conn = _Connection(ws=None)
        conn.outbox = asyncio.Queue(maxsize=1000)
        assert relay._enqueue(conn, "x" * 60) is True
        assert relay._enqueue(conn, "x" * 60) is False  # 120 > 100 bytes
        assert relay.counters.send_drops == 1
        assert conn.outbox_bytes == 60

    asyncio.run(scenario())


def test_relay_identity_uses_forwarded_proto(pool_public):
    """SF-2: behind TLS termination the identity's scheme is the
    forwarded proto the node dialed (wss), not a hardcoded ws."""
    from metasync_relay.config import RelayConfig
    from metasync_relay.server import Relay, _Connection

    class _Headers(dict):
        def get(self, name, default=""):
            return super().get(name, default)

    class _Request:
        def __init__(self, headers):
            self.headers = _Headers(headers)

    class _WS:
        def __init__(self, headers):
            self.request = _Request(headers)

    relay = Relay(pool_public, RelayConfig())
    tls = _Connection(ws=_WS({"Host": "relay.example", "X-Forwarded-Proto": "wss"}))
    assert relay._relay_identity(tls) == "wss://relay.example:443"
    plain = _Connection(ws=_WS({"Host": "relay.example:8791"}))
    assert relay._relay_identity(plain) == "ws://relay.example:8791"


def test_sealed_query_does_not_fan_out_to_v1_peers(
    pool_private, pool_public, node_a, node_b, node_c
):
    """SF-3: a query carrying a reply_key goes to v2 peers only — a
    v1 peer cannot seal, and its plaintext answers must not silently
    downgrade the query's confidentiality."""
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")  # v1 peer
            c = await h.connect2_ok(node_c, key_id="key-3")  # v2 peer
            query = exact_query(qid(62))
            query["reply_key"] = encode_base64url(b"\x07" * 32)
            query["reply_encoding"] = "none"
            await a.send(query)
            got = await c.next()
            assert got["type"] == "query" and "reply_key" in got
            await b.expect_silence(0.5)  # v1 peer is not a recipient
            oq = h.relay.open_queries[qid(62)]
            assert set(oq.expected) == {node_c.node_id}

    asyncio.run(scenario())


def test_plaintext_answer_to_sealed_query_is_not_forwarded(
    pool_private, pool_public, node_a, node_c
):
    """S1: the fan-out filter keeps v1 peers away from sealed
    queries, and this closes the other side: a v2 peer that answers
    a sealed query IN THE CLEAR has its answer dropped at the
    relay, never forwarded — and not struck, since a downgrade is
    a drop, not an abuse signal. Gate re-pass S-b: the drop
    happens BEFORE admission metering, so the answer is not
    charged to the answerer's byte budget, does not mark it
    responded, and does not count the query as answered."""
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            c = await h.connect2_ok(node_c, key_id="key-3")
            query = exact_query(qid(63))
            query["reply_key"] = encode_base64url(b"\x07" * 32)
            query["reply_encoding"] = "none"
            await a.send(query)
            got = await c.next()
            assert got["type"] == "query" and "reply_key" in got
            oq = h.relay.open_queries[qid(63)]
            await c.send(answer(qid(63), batch_of(node_c)))
            await a.expect_silence(0.5)
            snap = h.relay.counters.snapshot(connections_active=2)
            assert snap["answers_forwarded"] == 0
            assert snap["answers_dropped"] == 1
            assert snap["queries_answered"] == 0
            # The answerer was never even metered: metering is
            # what creates a node_state entry, and it never ran.
            state = h.relay.node_state.get(node_c.node_id)
            assert state is None or (
                state.violations == 0 and state.answer_bytes_total() == 0)
            assert node_c.node_id not in oq.responded
            assert oq.bytes_used == 0

    asyncio.run(scenario())


def test_answer_after_purge_is_a_silent_drop_not_a_strike(
    pool_private, pool_public, node_a, node_b
):
    """S4: an answer for a query the relay routed but has since
    PURGED is late, not uninvited — dropped silently, no strike.
    (An answer for a query the relay never routed still strikes:
    the tombstone is what separates the two.)"""
    async def scenario():
        async with Harness2(
            pool_private, pool_public, window_seconds=0.2, grace_seconds=0.2
        ) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            # Never routed at all: struck, as before.
            await b.send(answer(qid(71), batch=batch_of(node_b)))
            await asyncio.sleep(0.3)
            assert h.relay.node_state[node_b.node_id].violations == 1
            # Routed, then purged: the late answer is silent.
            await a.send(exact_query(qid(72)))
            await b.next()
            oq = h.relay.open_queries[qid(72)]
            oq.deadline = h.relay._mono() - 120.0
            oq.complete_sent = True
            oq.complete_time = oq.deadline
            h.relay._sweep_once()
            assert qid(72) not in h.relay.open_queries
            await b.send(answer(qid(72), batch=batch_of(node_b)))
            await asyncio.sleep(0.3)
            assert h.relay.node_state[node_b.node_id].violations == 1

    asyncio.run(scenario())


def test_reconnected_node_answers_without_strike(
    pool_private, pool_public, node_a, node_b
):
    """SF-4: a node that reconnects mid-window and answers on its new
    connection is expected, not uninvited: the answer routes and no
    strike lands."""
    async def scenario():
        async with Harness2(pool_private, pool_public, window_seconds=30.0) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(63)))
            await b.next()  # b was routed the query on its first conn
            await b.close()
            b2 = await h.connect2_ok(node_b, key_id="key-2")
            await b2.send(answer(qid(63), batch=batch_of(node_b)))
            msg = await a.next(timeout=5)
            assert msg["type"] == "answer"
            assert msg["stamp"]["from"] == node_b.node_id
            state = h.relay.node_state.get(node_b.node_id)
            assert state is None or state.violations == 0

    asyncio.run(scenario())


def test_counters_count_only_what_actually_moved(
    pool_public, node_a, node_b
):
    """SF-5: an answer the asker's full queue sheds counts as dropped,
    never as forwarded."""
    from metasync_relay.config import RelayConfig
    from metasync_relay.server import Relay

    mono = [2000.0]
    wall = [1_700_000_000.0]
    relay = Relay(
        pool_public,
        RelayConfig(window_seconds=60.0, grace_seconds=10.0),
        wall_clock=lambda: wall[0],
        monotonic=lambda: mono[0],
    )

    async def scenario():
        asker = _fake_conn(relay, node_a)
        peer = _fake_conn(relay, node_b)
        # The asker's queue is byte-full before the answer arrives.
        asker.outbox = asyncio.Queue(maxsize=128)
        asker.outbox_bytes = relay.config.send_queue_bytes
        raw_query = json.dumps(exact_query(qid(64)))
        assert await relay._dispatch(asker, raw_query) is True
        raw_answer = json.dumps(answer(qid(64), batch=batch_of(node_b)))
        assert await relay._dispatch(peer, raw_answer) is True
        assert relay.counters.answers_forwarded == 0
        assert relay.counters.answers_dropped == 1
        assert relay.counters.queries_answered == 1  # it WAS answered

    asyncio.run(scenario())


def test_startup_warns_when_revocation_enforcement_is_off(
    caplog, pool_private, pool_public
):
    """SF-6: a relay started with no issuer/revocations feed says so,
    loudly — the exchange being the only gate must not be silent."""
    import logging

    async def scenario():
        async with Harness(pool_private, pool_public) as _h:
            pass

    with caplog.at_level(logging.WARNING, logger="metasync_relay"):
        asyncio.run(scenario())
    assert any(
        "revocation_enforcement_off" in record.message
        for record in caplog.records
    )


def test_reauth_refusal_goes_through_the_send_queue(pool_public, node_a):
    """SF-7: a failed re-auth is refused via the connection's send
    queue — the sender task owns the socket after hello, and no
    second writer writes directly."""
    from metasync_relay.config import RelayConfig
    from metasync_relay.protocol import Reauth
    from metasync_relay.server import Relay, _Connection

    class _RecordingWS:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, payload) -> None:
            self.sent.append(payload)

        async def close(self, code=None) -> None:
            pass

    async def scenario():
        relay = Relay(pool_public, RelayConfig())
        ws = _RecordingWS()
        conn = _Connection(ws=ws)
        conn.node_id = node_a.node_id
        conn.node_key = node_a.public_raw
        conn.version = 2
        conn.outbox = asyncio.Queue(maxsize=128)  # queue, but no sender
        reauth = Reauth(token="not-a-token", timestamp=1, signature=b"\x00" * 64)
        assert await relay._route_reauth(conn, reauth) is False
        assert ws.sent == []  # nothing was written directly
        queued = conn.outbox.get_nowait()
        assert json.loads(queued)["type"] == "refused"

    asyncio.run(scenario())


def test_duplicate_query_is_refused_before_budget_charge(
    pool_private, pool_public, node_a, node_b
):
    """Minor: the honest retry (same id re-asked) is refused as a
    duplicate without consuming query budget."""
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(65)))
            await b.next()
            await a.send(exact_query(qid(65)))  # the retry
            refused = await a.next()
            assert refused["type"] == "query_refused"
            assert refused["reason"] == "duplicate_query"
            state = h.relay.node_state[node_a.node_id]
            assert len(state.query_times) == 1  # only the first ask charged
            assert state.violations == 0

    asyncio.run(scenario())

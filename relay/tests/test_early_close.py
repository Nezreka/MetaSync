"""Early close (post-v2, capability-negotiated): the asker ends its
own query before the window runs out; the relay frees the fan-out
state at once and late answers take the purged-query path — dropped
silently, never charged. Same harness style as test_server_v2.py.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from metasync_relay.protocol import Close, ProtocolError, parse_message
from test_server import Harness, answer, exact_query, qid
from test_server_v2 import Harness2, batch_of


def close_msg(query_id: str) -> dict:
    return {"type": "close", "query_id": query_id}


# -- protocol shape ----------------------------------------------------------


def test_close_message_parses():
    msg = parse_message(
        json.dumps({"type": "close", "query_id": qid(7)}))
    assert msg == Close(query_id=qid(7))


def test_close_message_rejects_a_malformed_query_id():
    with pytest.raises(ProtocolError):
        parse_message(json.dumps({"type": "close", "query_id": "nope"}))
    with pytest.raises(ProtocolError):
        parse_message(json.dumps({"type": "close"}))


# -- the capability ----------------------------------------------------------


def test_v2_welcome_advertises_early_close(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            client = await h.connect2_ok(node_a)
            caps = client.welcome.get("capabilities") or {}
            assert caps.get("early_close") is True

    asyncio.run(scenario())


def test_v1_welcome_has_no_early_close_capability(pool_private, pool_public, node_a):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            client = await h.connect_ok(node_a)
            assert "capabilities" not in client.welcome

    asyncio.run(scenario())


# -- honored closes ------------------------------------------------------------


def test_close_frees_the_query_and_late_answers_drop_uncharged(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            c = await h.connect2_ok(node_c, key_id="key-3")
            await asker.send(exact_query(qid(41)))
            assert (await b.next())["type"] == "query"
            assert (await c.next())["type"] == "query"
            # One answer lands and is forwarded normally.
            await b.send(answer(qid(41), batch_of(node_b)))
            got = await asker.next()
            assert got["type"] == "answer" and got["query_id"] == qid(41)
            assert h.relay.counters.answers_forwarded == 1

            await asker.send(close_msg(qid(41)))
            # The close is honored silently — no ack, no complete —
            # and the fan-out state is gone at once.
            await asker.expect_silence(0.5)
            assert qid(41) not in h.relay.open_queries
            assert h.relay.counters.queries_closed_early == 1

            # Answers still in flight when the close landed (and a
            # done) now hit the purged path: dropped silently,
            # never metered, never a strike — the same treatment a
            # window-expired query's late answers get.
            dropped_before = h.relay.counters.answers_dropped
            await c.send(answer(qid(41), batch_of(node_c)))
            await c.send({"type": "done", "query_id": qid(41)})
            await asker.expect_silence(0.5)
            assert h.relay.counters.answers_forwarded == 1
            assert h.relay.counters.answers_dropped == dropped_before
            state = h.relay.node_state.get(node_c.node_id)
            assert state is None or (
                state.violations == 0 and state.answer_bytes_total() == 0
            )

    asyncio.run(scenario())


def test_close_after_complete_still_frees_the_grace_tail(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            c = await h.connect2_ok(node_c, key_id="key-3")
            await asker.send(exact_query(qid(42)))
            assert (await b.next())["type"] == "query"
            assert (await c.next())["type"] == "query"
            await b.send(answer(qid(42), batch_of(node_b)))
            assert (await asker.next())["type"] == "answer"
            await b.send({"type": "done", "query_id": qid(42)})
            await c.send({"type": "done", "query_id": qid(42)})
            # Every expected answerer reported: the relay completes
            # the query, which stays open through its grace.
            assert (await asker.next())["type"] == "complete"
            assert qid(42) in h.relay.open_queries
            # The asker has enough even for the grace tail: closing
            # now frees the state immediately instead of at
            # complete + grace.
            await asker.send(close_msg(qid(42)))
            await asker.expect_silence(0.5)
            assert qid(42) not in h.relay.open_queries
            assert h.relay.counters.queries_closed_early == 1
            await c.send(answer(qid(42), batch_of(node_c)))
            await asker.expect_silence(0.5)
            assert h.relay.counters.answers_forwarded == 1

    asyncio.run(scenario())


# -- ignored closes ----------------------------------------------------------


def test_foreign_close_is_ignored_and_the_query_lives_on(
    pool_private, pool_public, node_a, node_b, node_c
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            c = await h.connect2_ok(node_c, key_id="key-3")
            await asker.send(exact_query(qid(43)))
            assert (await b.next())["type"] == "query"
            assert (await c.next())["type"] == "query"
            # node_b does not own this query: its close must change
            # nothing — and say nothing, to anyone.
            await b.send(close_msg(qid(43)))
            await asker.expect_silence(0.5)
            await b.expect_silence(0.3)
            assert qid(43) in h.relay.open_queries
            assert h.relay.counters.queries_closed_early == 0
            # The query is unharmed: a later answer still routes.
            await c.send(answer(qid(43), batch_of(node_c)))
            got = await asker.next()
            assert got["type"] == "answer" and got["query_id"] == qid(43)

    asyncio.run(scenario())


def test_v1_close_for_its_own_query_is_ignored(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            asker = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(46)))
            assert (await b.next())["type"] == "query"
            # Ownership is not enough on a v1 connection: close is
            # a post-v2 message, gated to conn.version >= 2, so v1
            # traffic keeps exactly its pre-close treatment.
            await asker.send(close_msg(qid(46)))
            await asker.expect_silence(0.5)
            await b.expect_silence(0.3)
            assert qid(46) in h.relay.open_queries
            assert qid(46) not in h.relay._purged_queries
            assert h.relay.counters.queries_closed_early == 0
            await b.send(answer(qid(46), batch_of(node_b)))
            got = await asker.next()
            assert got["type"] == "answer" and got["query_id"] == qid(46)

    asyncio.run(scenario())


def test_unknown_close_is_ignored_silently(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            a = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await a.send(close_msg(qid(999)))
            await b.send(close_msg(qid(998)))
            await a.expect_silence(0.5)
            await b.expect_silence(0.3)
            assert h.relay.counters.queries_closed_early == 0
            assert h.relay.open_queries == {}

    asyncio.run(scenario())


def test_double_close_counts_once(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(44)))
            assert (await b.next())["type"] == "query"
            await asker.send(close_msg(qid(44)))
            await asker.send(close_msg(qid(44)))
            await asker.expect_silence(0.5)
            assert h.relay.counters.queries_closed_early == 1
            assert qid(44) not in h.relay.open_queries

    asyncio.run(scenario())


# -- tombstoned ids stay spent (fold) ------------------------------------------


def test_closed_query_id_cannot_be_re_registered(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(50)))
            assert (await b.next())["type"] == "query"
            await asker.send(close_msg(qid(50)))
            await asker.expect_silence(0.5)
            assert qid(50) in h.relay._purged_queries
            # Re-registering the tombstoned id is refused with the
            # duplicate shape — a notice, never routed again.
            await asker.send(exact_query(qid(50)))
            refused = await asker.next()
            assert refused["type"] == "query_refused"
            assert refused["reason"] == "duplicate_query"
            assert refused["query_id"] == qid(50)
            assert qid(50) not in h.relay.open_queries
            await b.expect_silence(0.3)
            # The original query's late answers stay dropped.
            await b.send(answer(qid(50), batch_of(node_b)))
            await asker.expect_silence(0.5)
            assert h.relay.counters.answers_forwarded == 0

    asyncio.run(scenario())


def test_nonowner_reregistering_a_closed_id_gets_the_open_treatment(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(52)))
            assert (await b.next())["type"] == "query"
            await asker.send(close_msg(qid(52)))
            await asker.expect_silence(0.5)
            assert qid(52) in h.relay._purged_queries
            # node_b was a fan-out recipient for the id. If its
            # re-registration drew the duplicate_query notice, it
            # would learn the asker had closed; an id still OPEN
            # would instead draw a query_id_in_use strike. The
            # tombstone must produce exactly the open treatment:
            # no notice to anyone, one strike, nothing routed.
            # (The owner direction — the asker re-registering its
            # own closed id and getting duplicate_query — is pinned
            # by test_closed_query_id_cannot_be_re_registered.)
            await b.send(exact_query(qid(52)))
            await b.expect_silence(0.5)
            await asker.expect_silence(0.3)
            assert qid(52) not in h.relay.open_queries
            assert h.relay.counters.queries_closed_early == 1
            state = h.relay.node_state.get(node_b.node_id)
            assert state is not None and state.violations == 1

    asyncio.run(scenario())


def test_expired_query_id_cannot_be_re_registered(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(51)))
            assert (await b.next())["type"] == "query"
            # Stand the deadline in for the window + purge margin
            # having passed (the test_server.py pattern), then
            # sweep: expiry moves the query to the tombstones.
            oq = h.relay.open_queries[qid(51)]
            oq.deadline = time.monotonic() - 3600
            h.relay._sweep_once()
            assert qid(51) not in h.relay.open_queries
            assert qid(51) in h.relay._purged_queries
            assert (await asker.next())["type"] == "complete"
            # The expired id is refused on re-registration too —
            # the pre-existing window-expiry path gets the same
            # refusal the close path does.
            await asker.send(exact_query(qid(51)))
            refused = await asker.next()
            assert refused["type"] == "query_refused"
            assert refused["reason"] == "duplicate_query"
            assert qid(51) not in h.relay.open_queries
            await b.expect_silence(0.3)
            await b.send(answer(qid(51), batch_of(node_b)))
            await asker.expect_silence(0.5)
            assert h.relay.counters.answers_forwarded == 0

    asyncio.run(scenario())


# -- peers that never close behave exactly as before ---------------------------


def test_no_close_means_the_window_and_complete_as_today(
    pool_private, pool_public, node_a, node_b
):
    async def scenario():
        async with Harness2(pool_private, pool_public) as h:
            asker = await h.connect2_ok(node_a)
            b = await h.connect2_ok(node_b, key_id="key-2")
            await asker.send(exact_query(qid(45)))
            assert (await b.next())["type"] == "query"
            await b.send(answer(qid(45), batch_of(node_b)))
            assert (await asker.next())["type"] == "answer"
            await b.send({"type": "done", "query_id": qid(45)})
            assert (await asker.next())["type"] == "complete"
            assert h.relay.counters.queries_closed_early == 0
            snap = h.relay.counters.snapshot(connections_active=2)
            assert snap["queries_closed_early"] == 0

    asyncio.run(scenario())

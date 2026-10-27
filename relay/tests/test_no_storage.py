"""The no-storage rule (DESIGN-METADATA.md sections 10 and 12, Q1/Q4),
proven two ways: behaviorally, by driving a full query round over the
real server and auditing every external touch the relay made; and
mechanically, by scanning the package source for write paths."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from test_server import Harness, answer, exact_query, qid

PACKAGE = Path(__file__).resolve().parents[1] / "metasync_relay"


def test_relay_only_ever_peeks_and_state_empties(pool_private, pool_public, node_a, node_b):
    async def scenario():
        async with Harness(pool_private, pool_public) as h:
            a = await h.connect_ok(node_a)
            b = await h.connect_ok(node_b, key_id="key-2")
            await a.send(exact_query(qid(51)))
            await b.next()
            await b.send(answer(qid(51)))
            await a.next()
            await a.close()
            await b.close()
            for _ in range(50):
                if not h.relay.presence:
                    break
                await asyncio.sleep(0.05)
            # Presence was the only state, and it is gone with the
            # connections: a restart would know exactly this much.
            assert h.relay.presence == {}
            assert h.relay.open_queries == {}
            # Every external call the relay made, all session long, was
            # the single read-only guest-list peek.
            assert h.store.calls, "expected at least the connect-time peeks"
            assert {call[0] for call in h.store.calls} == {"is_bound_and_valid"}

    asyncio.run(scenario())


def test_package_source_has_no_write_paths():
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("*.py"))
    }
    # SQL lives only in keys.py, and it is exactly one read.
    for name, text in sources.items():
        if name == "keys.py":
            continue
        assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE)\b", text), name
        assert "psycopg" not in text, name
    # The one SQL statement in the package (imported, not scraped, so
    # prose in comments cannot confuse the check): a single read that
    # never names the account column.
    from metasync_relay.keys import _SELECT_BOUND_KEY

    assert _SELECT_BOUND_KEY.strip().upper().startswith("SELECT")
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE"):
        assert not re.search(rf"\b{verb}\b", _SELECT_BOUND_KEY.upper()), verb
    assert "user_id" not in _SELECT_BOUND_KEY
    # No file writes anywhere in the package: the only open() shaped
    # call allowed would be none at all in v1.
    for name, text in sources.items():
        assert "sqlite" not in text.lower(), name
        assert not re.search(r"\bopen\s*\(", text), name

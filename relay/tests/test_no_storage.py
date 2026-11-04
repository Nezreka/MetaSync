"""The no-storage / no-database rule (DESIGN-METADATA.md sections 10
and 12, Q1/Q4), proven two ways: behaviorally, by driving a full query
round over the real server and auditing the relay's state afterwards;
and mechanically, by scanning the package source for write paths and
database access.

Protocol v2 removed the relay's one database touch (the connect-time
guest-list peek): the guest-list check lives at the token exchange,
tokens are verified offline, and this package must not grow a new
database path — the scan below fails if one appears.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from test_server import Harness, answer, exact_query, qid

PACKAGE = Path(__file__).resolve().parents[1] / "metasync_relay"


def test_relay_state_empties_with_the_connections(pool_private, pool_public, node_a, node_b):
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
            # Presence was the only session state, and it is gone with
            # the connections: a restart would know exactly this much.
            assert h.relay.presence == {}
            assert h.relay.open_queries == {}

    asyncio.run(scenario())


def test_package_source_has_no_write_or_database_paths():
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("*.py"))
    }
    # No SQL, no database driver, no database URL anywhere: the relay
    # verifies tokens offline and holds no path to the portal's data.
    for name, text in sources.items():
        assert not re.search(
            r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE)\b", text
        ), name
        assert "psycopg" not in text, name
        assert "DATABASE_URL" not in text, name
        assert "sqlite" not in text.lower(), name
    # No file writes anywhere in the package: the only open() shaped
    # call allowed would be none at all.
    for name, text in sources.items():
        assert not re.search(r"\bopen\s*\(", text), name
    # And nothing in the package may grow a key-store / database
    # requirement again: the v1 names stay gone.
    for name, text in sources.items():
        assert "PostgresKeyStore" not in text, name
        assert "require_database_url" not in text, name
        assert "is_bound_and_valid" not in text, name

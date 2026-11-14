"""Regression tests for the November 14, 2026 review fixes (relay side)."""
from __future__ import annotations

import asyncio
import logging

from metasync_relay.__main__ import ExtraFormatter
from metasync_relay.config import RelayConfig
from metasync_relay.server import Relay


def test_first_revocation_fetch_does_not_wait_for_uptime(pool_public):
    """The monotonic clock is seconds since boot. On a fresh machine it
    reads a few seconds, and the first fetch must still happen at once,
    not after a whole refresh interval with no list held."""
    mono = [3.0]
    relay = Relay(
        pool_public,
        RelayConfig(revocations_interval_seconds=300.0),
        monotonic=lambda: mono[0],
    )
    calls = []

    class Feed:
        url = "https://issuer.example/v1/meta/revocations"
        current = None

    async def fake_refresh():
        calls.append(mono[0])
        return False

    relay.revocations = Feed()
    relay.refresh_revocations = fake_refresh

    async def scenario():
        await relay._refresh_revocations_if_due()
        assert calls == [3.0]
        mono[0] = 100.0  # inside the interval: no second fetch
        await relay._refresh_revocations_if_due()
        assert calls == [3.0]
        mono[0] = 303.5
        await relay._refresh_revocations_if_due()
        assert calls == [3.0, 303.5]

    asyncio.run(scenario())


def test_log_lines_carry_their_extra_fields():
    formatter = ExtraFormatter("%(levelname)s %(name)s %(message)s")
    record = logging.LogRecord("metasync_relay", logging.INFO, __file__, 1, "counters", (), None)
    record.queries_routed = 7
    record.connections_active = 2
    assert formatter.format(record) == (
        "INFO metasync_relay counters connections_active=2 queries_routed=7"
    )
    plain = logging.LogRecord("metasync_relay", logging.INFO, __file__, 1, "node_connected", (), None)
    assert formatter.format(plain) == "INFO metasync_relay node_connected"

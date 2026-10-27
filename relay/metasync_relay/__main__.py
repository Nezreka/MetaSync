"""Relay entry point: python -m metasync_relay

Configuration comes from the environment only (see config.py). Startup
fails closed: without RELAY_POOL_PUBLIC_KEY or DATABASE_URL the relay
refuses to run, because a switchboard that cannot check the guest list
must not open the room.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import (
    ConfigError,
    RelayConfig,
    require_database_url,
    require_pool_public_key,
)
from .keys import PostgresKeyStore
from .server import Relay, serve_forever


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = RelayConfig.from_env()
        pool_public_key = require_pool_public_key()
        database_url = require_database_url()
    except ConfigError as exc:
        print(f"metasync_relay: refusing to start: {exc}", file=sys.stderr)
        return 2
    relay = Relay(PostgresKeyStore(database_url), pool_public_key, config)
    try:
        asyncio.run(serve_forever(relay, config.host, config.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

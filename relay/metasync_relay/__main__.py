"""Relay entry point: python -m metasync_relay

Configuration comes from the environment only (see config.py). Startup
fails closed: without RELAY_POOL_PUBLIC_KEY the relay refuses to run,
because a switchboard that cannot verify tokens must not open the
room. There is no database to configure — protocol v2 removed the
relay's only database touch (the connect-time guest-list peek); the
binding check lives at the token exchange now.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import (
    ConfigError,
    RelayConfig,
    issuer_public_key,
    require_pool_public_key,
)
from .server import Relay, serve_forever


_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class ExtraFormatter(logging.Formatter):
    """The plain line plus every ``extra=`` field as ``key=value``.

    The relay logs numbers and error classes through ``extra`` (the
    counters line, violations, refusals). The stock format string prints
    none of them, which left the line the runbook reads as the bare
    word "counters". Only what the code passes in ``extra`` is added:
    counts and class names, never a subject, node id or address.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RECORD_FIELDS
        }
        if not extras:
            return line
        fields = " ".join(f"{key}={extras[key]}" for key in sorted(extras))
        return f"{line} {fields}"


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        ExtraFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> int:
    configure_logging()
    try:
        config = RelayConfig.from_env()
        pool_public_key = require_pool_public_key()
        issuer_key = issuer_public_key(pool_public_key=pool_public_key)
    except ConfigError as exc:
        print(f"metasync_relay: refusing to start: {exc}", file=sys.stderr)
        return 2
    relay = Relay(pool_public_key, config, issuer_public_key=issuer_key)
    try:
        asyncio.run(serve_forever(relay, config.host, config.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

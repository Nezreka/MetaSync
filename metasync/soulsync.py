"""Client for SoulSync's ``/api/v1/metasync/export`` endpoint.

Read-only, stdlib-only. Walks the library with keyset pagination and yields raw
rows; turning rows into claims is ``importer.py``'s job.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Optional

ENTITIES = ("artist", "album", "track")


class SoulSyncError(RuntimeError):
    """The SoulSync API refused or failed a request."""


class _RateLimited(Exception):
    """Internal: a 429, retried with backoff rather than surfaced."""

    def __init__(self, retry_after: Optional[float] = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class _Unreachable(Exception):
    """Internal: SoulSync did not answer. Usually it is restarting."""


class SoulSyncClient:
    # The /api/v1 blueprint is rate-limited to 60 requests/minute. A full walk
    # of a large library is several hundred requests, so pacing is not optional.
    MAX_RETRIES = 5
    # A walk takes minutes and SoulSync restarts — on upgrade, on a config
    # change, whenever a developer reloads it. Riding that out beats losing the
    # walk, and the cursor is checkpointed anyway, so the cost of being wrong
    # is small. Roughly four minutes of patience.
    MAX_CONNECT_RETRIES = 10
    MAX_BACKOFF = 30.0

    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET, retrying through rate limits and through SoulSync restarts.

        Honours Retry-After on a 429. A refused connection is treated as
        transient — SoulSync restarting — and retried with backoff, because a
        walk of a large library takes long enough that a restart during one is
        ordinary rather than exceptional.
        """
        rate_limited = connect_failures = 0
        delay = 2.0
        while True:
            try:
                return self._get_once(path, params)
            except _RateLimited as limited:
                rate_limited += 1
                if rate_limited >= self.MAX_RETRIES:
                    raise SoulSyncError(
                        f"rate limited by SoulSync {rate_limited} times in a row; "
                        "lower METASYNC_PAGE_SIZE or slow the walk"
                    ) from limited
                time.sleep(limited.retry_after or delay)
            except _Unreachable as unreachable:
                connect_failures += 1
                if connect_failures >= self.MAX_CONNECT_RETRIES:
                    raise SoulSyncError(
                        f"SoulSync at {self.base_url} did not answer after "
                        f"{connect_failures} attempts: {unreachable}. Is it running?"
                    ) from unreachable
                time.sleep(delay)
            delay = min(delay * 2, self.MAX_BACKOFF)

    def _get_once(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
        url = f"{self.base_url}{path}?{query}"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 429:
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = float(header) if header else None
                except (TypeError, ValueError):
                    retry_after = None
                raise _RateLimited(retry_after) from exc
            # Never echo the URL back — it would put the key in a log if the
            # caller ever passed it as a query param.
            raise SoulSyncError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise _Unreachable(str(exc.reason)) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise _Unreachable(str(exc)) from exc

        if not body.get("success"):
            error = body.get("error") or {}
            raise SoulSyncError(f"{error.get('code', 'ERROR')}: {error.get('message', 'unknown')}")
        return body.get("data") or {}

    def export_page(self, entity: str, cursor: str = "", since: str = "",
                    limit: int = 500) -> Dict[str, Any]:
        return self._get("/api/v1/metasync/export", {
            "entity": entity, "cursor": cursor, "since": since, "limit": limit,
        })

    def export(self, entity: str, cursor: str = "", since: str = "",
               limit: int = 500, pause: float = 1.1) -> Iterator[Dict[str, Any]]:
        """Yield ``(row, cursor_after_this_page)`` pages until the walk ends.

        Yields whole pages rather than rows so the caller can checkpoint its
        cursor per page and resume an interrupted sync without re-walking.
        The blueprint is rate-limited to 60 requests/minute, so ``pause``
        defaults to just over a second — enough to stay under the ceiling
        without relying on the 429 backoff to carry a whole walk.
        """
        if entity not in ENTITIES:
            raise ValueError(f"unknown entity {entity!r}")
        while True:
            page = self.export_page(entity, cursor=cursor, since=since, limit=limit)
            rows = page.get("items") or []
            next_cursor = page.get("next_cursor") or ""
            yield {"rows": rows, "cursor": next_cursor}
            if not page.get("has_more") or not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor
            if pause:
                time.sleep(pause)

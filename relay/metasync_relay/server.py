"""The relay server (DESIGN-METADATA.md section 10).

A dumb switchboard, by specification:
- presence is an in-memory dict and nothing else; a restart empties it;
- queries fan out to the other connected nodes; answers route only to
  the connection that asked, only for query IDs the relay actually
  routed, only inside the answer window plus a grace period;
- per-node budgets (queries/hour, answers/hour, answer bytes per query)
  bound scraping and flooding; unsolicited, late, or over-cap answers
  are dropped and charged to the sender;
- nothing about query or answer content is ever logged: log lines carry
  connection counts and error classes only (section 10).

State note: the only mutable state is presence, open queries, and
per-node budget/violation counters, all in this process's memory.
The counters are keyed by node id at the relay level, not by
connection: reconnecting must not launder an hourly budget or an
abuse record. The one external touch is the key store's read-only
peek at connect time.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import serve as _ws_serve
from websockets.exceptions import ConnectionClosed

from .config import RelayConfig
from .keys import KeyStore
from .protocol import (
    MAX_MESSAGE_BYTES,
    Answer,
    Bye,
    Done,
    Hello,
    ProtocolError,
    Query,
    parse_message,
    query_to_json,
    relay_message_to_json,
)
from .tokens import encode_base64url, node_id_for_public_key, verify_access_token

log = logging.getLogger("metasync_relay")

REQUIRED_SCOPE = "meta:read"
HELLO_SIGNATURE_CONTEXT = "metasync-relay-hello"
# Mirror of NODE_TIMESTAMP_WINDOW_SECONDS in src/sin/logic.ts (section 3A.2).
NODE_TIMESTAMP_WINDOW_SECONDS = 5 * 60
HELLO_TIMEOUT_SECONDS = 30


def hello_signing_text(token: str, timestamp: int, challenge: bytes) -> bytes:
    """The canonical string a node signs in hello: it binds the access
    token, the presented node key (whose id the relay derives and
    compares), a fresh timestamp, and the per-connection challenge
    nonce the relay issued on this connection, for this relay
    protocol only. Binding the nonce is what makes a captured hello
    worthless on any other connection."""
    return (
        f"{HELLO_SIGNATURE_CONTEXT}\n{token}\n{timestamp}\n"
        f"{encode_base64url(challenge)}"
    ).encode()


@dataclass
class _Connection:
    ws: object
    node_id: str = ""
    node_key: bytes = b""
    key_id: str = ""
    mode: str = ""
    challenge: bytes | None = None  # this connection's hello nonce, single use
    replaced: bool = False


@dataclass
class _NodeState:
    """Per-node budgets and abuse record. Lives on the relay, keyed by
    node id, so it survives the node's reconnects; it dies only with
    the process, like everything else here."""

    violations: int = 0
    query_times: deque = field(default_factory=deque)
    answer_times: deque = field(default_factory=deque)


@dataclass
class _OpenQuery:
    query_id: str
    asker: _Connection
    deadline: float  # end of the answer window (monotonic)
    grace_seconds: float
    expected: set[str]  # node ids the query was forwarded to
    responded: set[str] = field(default_factory=set)
    bytes_used: int = 0
    all_responded_at: float | None = None
    orphaned_at: float | None = None  # set when the asker's connection dies

    def admission_end(self) -> float:
        """Answers are admitted until the window closes plus grace. The
        window closes early when every expected node has answered or
        signalled done (section 10)."""
        if self.all_responded_at is not None:
            return self.all_responded_at + self.grace_seconds
        return self.deadline + self.grace_seconds

    def note_response(self, node_id: str, now: float) -> None:
        self.responded.add(node_id)
        if (
            self.all_responded_at is None
            and self.expected
            and self.expected <= self.responded
        ):
            self.all_responded_at = now


class Relay:
    def __init__(
        self,
        key_store: KeyStore,
        pool_public_key: Ed25519PublicKey,
        config: RelayConfig | None = None,
        wall_clock=time.time,
        monotonic=time.monotonic,
    ) -> None:
        self.key_store = key_store
        self.pool_public_key = pool_public_key
        self.config = config or RelayConfig()
        self._wall = wall_clock
        self._mono = monotonic
        self.presence: dict[str, _Connection] = {}
        self.open_queries: dict[str, _OpenQuery] = {}
        self.node_state: dict[str, _NodeState] = {}

    def _state_for(self, node_id: str) -> _NodeState:
        return self.node_state.setdefault(node_id, _NodeState())

    # -- connection lifecycle ------------------------------------------

    async def handle_connection(self, ws) -> None:
        conn = _Connection(ws=ws)
        try:
            if not await self._read_hello(conn):
                return
            async for raw in ws:
                if conn.replaced:
                    break
                if not await self._dispatch(conn, raw):
                    break
        except ConnectionClosed:
            pass
        finally:
            self._evict(conn)

    async def _read_hello(self, conn: _Connection) -> bool:
        # Issue this connection's challenge before anything else: a
        # fresh 32-byte nonce, valid for this one hello attempt only.
        conn.challenge = secrets.token_bytes(32)
        sent = await self._send(
            conn,
            relay_message_to_json(
                {"type": "challenge", "nonce": encode_base64url(conn.challenge)}
            ),
        )
        if not sent:
            return False
        try:
            raw = await asyncio.wait_for(
                conn.ws.recv(), timeout=HELLO_TIMEOUT_SECONDS
            )
        except (TimeoutError, ConnectionClosed):
            return False
        try:
            msg = parse_message(raw)
        except ProtocolError:
            await self._refuse(conn, "malformed")
            return False
        if not isinstance(msg, Hello):
            await self._refuse(conn, "expected_hello")
            return False
        if not self._authenticate(conn, msg):
            # One reason class for every auth failure: no oracle about
            # which check (token, scope, binding, signature) failed.
            await self._refuse(conn, "auth")
            return False
        old = self.presence.get(conn.node_id)
        if old is not None and old is not conn:
            # A reconnecting node evicts its own stale connection; it
            # must never lock itself out behind a lingering socket.
            old.replaced = True
            await self._close(old)
        self.presence[conn.node_id] = conn
        log.info("node_connected", extra={"peers": len(self.presence)})
        await self._send(
            conn,
            relay_message_to_json(
                {"type": "welcome", "peers": self._peer_count(excluding=conn.node_id)}
            ),
        )
        return True

    def _authenticate(self, conn: _Connection, hello: Hello) -> bool:
        # The challenge is consumed by the attempt, pass or fail: a
        # hello without this connection's live nonce (or replaying
        # one already spent) is not authentic, whatever else checks.
        challenge, conn.challenge = conn.challenge, None
        if challenge is None or hello.challenge != challenge:
            return False
        claims = verify_access_token(
            hello.token, self.pool_public_key, now=self._wall()
        )
        if claims is None:
            return False
        if REQUIRED_SCOPE not in claims.scopes:
            return False
        node_id = node_id_for_public_key(hello.node_key)
        if claims.node_id != node_id:
            return False
        if abs(self._wall() - hello.timestamp) > NODE_TIMESTAMP_WINDOW_SECONDS:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(hello.node_key).verify(
                hello.signature,
                hello_signing_text(hello.token, hello.timestamp, hello.challenge),
            )
        except InvalidSignature:
            return False
        except Exception:  # noqa: BLE001 - any crypto failure rejects the hello
            return False
        if not self.key_store.is_bound_and_valid(
            claims.key_id, claims.node_id, hello.node_key
        ):
            return False
        conn.node_id = node_id
        conn.node_key = hello.node_key
        conn.key_id = claims.key_id
        conn.mode = hello.mode
        return True

    def _evict(self, conn: _Connection) -> None:
        if conn.node_id and self.presence.get(conn.node_id) is conn:
            del self.presence[conn.node_id]
            log.info("node_disconnected", extra={"peers": len(self.presence)})
        if conn.node_id:
            now = self._mono()
            for query_id, oq in list(self.open_queries.items()):
                if oq.asker is conn:
                    # Tombstone, not deletion: responders still in
                    # the window may answer a query whose asker just
                    # died; those answers drop silently (see the
                    # routes) instead of being charged as junk.
                    if oq.orphaned_at is None:
                        oq.orphaned_at = now
                else:
                    oq.expected.discard(conn.node_id)
                    if (
                        oq.all_responded_at is None
                        and oq.expected <= oq.responded
                    ):
                        oq.all_responded_at = now
                if oq.orphaned_at is not None and not oq.expected:
                    # No responder left who could still answer: the
                    # tombstone has nothing left to absorb. Any other
                    # orphan is left for the purge sweeper's margin.
                    del self.open_queries[query_id]

    # -- message routing ------------------------------------------------

    async def _dispatch(self, conn: _Connection, raw: str | bytes) -> bool:
        """Returns False when the connection should close."""
        try:
            msg = parse_message(raw)
        except ProtocolError as exc:
            return await self._violate(conn, exc.reason)
        self._purge_expired_queries()
        if isinstance(msg, Hello):
            return await self._violate(conn, "unexpected_hello")
        if isinstance(msg, Bye):
            return False
        if isinstance(msg, Query):
            return await self._route_query(conn, msg)
        if isinstance(msg, Answer):
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            return await self._route_answer(conn, msg, size)
        if isinstance(msg, Done):
            return await self._route_done(conn, msg)
        return True

    async def _route_query(self, conn: _Connection, query: Query) -> bool:
        """Returns False when a violation closed the connection: the
        caller must stop processing further messages on it."""
        if not self._charge(
            self._state_for(conn.node_id).query_times, self.config.queries_per_hour
        ):
            return await self._violate(conn, "query_budget")
        existing = self.open_queries.get(query.query_id)
        if existing is not None:
            reason = (
                "duplicate_query" if existing.asker is conn else "query_id_in_use"
            )
            return await self._violate(conn, reason)
        now = self._mono()
        # Leech-mode nodes (protocol NODE_MODES) never answer: they are
        # excluded from the fan-out and from the expected set, so they
        # cannot delay the all-responded early close either.
        expected = {
            nid
            for nid, c in self.presence.items()
            if c is not conn and c.mode != "leech"
        }
        oq = _OpenQuery(
            query_id=query.query_id,
            asker=conn,
            deadline=now + self.config.window_seconds,
            grace_seconds=self.config.grace_seconds,
            expected=expected,
        )
        if not expected:
            oq.all_responded_at = now
        self.open_queries[query.query_id] = oq
        payload = query_to_json(query)
        # Concurrent sends: one peer that stops reading must not
        # head-of-line block the asker's handler loop (or the other
        # recipients) behind its socket buffer.
        sends = [
            self._send_with_timeout(peer, payload)
            for node_id in expected
            if (peer := self.presence.get(node_id)) is not None
        ]
        if sends:
            await asyncio.gather(*sends, return_exceptions=True)
        return True

    async def _send_with_timeout(self, conn: _Connection, payload: str) -> bool:
        try:
            return await asyncio.wait_for(
                self._send(conn, payload),
                timeout=self.config.send_timeout_seconds,
            )
        except TimeoutError:
            return False

    async def _route_answer(
        self, conn: _Connection, answer: Answer, size: int
    ) -> bool:
        """Returns False when a violation closed the connection."""
        # Every answer is charged to the sender's hourly budget first,
        # admitted or not (section 10: junk is charged, not free).
        if not self._charge(
            self._state_for(conn.node_id).answer_times, self.config.answers_per_hour
        ):
            return await self._violate(conn, "answer_budget")
        oq = self.open_queries.get(answer.query_id)
        if oq is None:
            return await self._violate(conn, "unknown_query")
        if oq.orphaned_at is not None:
            return True  # asker is gone: silent drop, never a violation
        if oq.asker is conn:
            return await self._violate(conn, "self_answer")
        if conn.node_id not in oq.expected:
            # The query was never routed to this node (it connected
            # after the fan-out, or was not a recipient): its answer is
            # uninvited, however well-formed.
            return await self._violate(conn, "unrouted_answer")
        if self._mono() > oq.admission_end():
            return await self._violate(conn, "late_answer")
        if oq.bytes_used + size > self.config.answer_cap_bytes:
            return await self._violate(conn, "answer_cap")
        oq.bytes_used += size
        oq.note_response(conn.node_id, self._mono())
        # The batch passes through as a parsed JSON object, untouched in
        # content: the asker verifies its signature over the section 6
        # canonical form, not over transport bytes, so re-serialization
        # here cannot invalidate it.
        await self._send(
            oq.asker,
            relay_message_to_json(
                {"type": "answer", "query_id": answer.query_id, "batch": answer.batch}
            ),
        )
        return True

    async def _route_done(self, conn: _Connection, done: Done) -> bool:
        """Returns False when a violation closed the connection."""
        oq = self.open_queries.get(done.query_id)
        if oq is None:
            return True  # liveness-adjacent: dropped silently, never charged
        if oq.orphaned_at is not None:
            return True  # asker is gone: silent drop, like answers
        if conn.node_id not in oq.expected:
            # Same admission rule as answers: a done from a node the
            # query was never routed to (the asker included) is
            # uninvited and charged.
            return await self._violate(conn, "unrouted_done")
        if self._mono() > oq.admission_end():
            return True
        oq.note_response(conn.node_id, self._mono())
        await self._send(
            oq.asker,
            relay_message_to_json({"type": "done", "query_id": done.query_id}),
        )
        return True

    # -- budgets and abuse ----------------------------------------------

    def _charge(self, times: deque, limit: int) -> bool:
        now = self._mono()
        cutoff = now - 3600
        while times and times[0] <= cutoff:
            times.popleft()
        times.append(now)
        return len(times) <= limit

    async def _violate(self, conn: _Connection, reason: str) -> bool:
        """Charge a protocol/budget violation. Returns False when the
        connection has crossed the abuse threshold and should close."""
        state = self._state_for(conn.node_id)
        state.violations += 1
        log.warning(
            "violation",
            extra={"error_class": reason, "count": state.violations},
        )
        if state.violations > self.config.max_violations:
            await self._close(conn)
            return False
        return True

    # -- helpers ----------------------------------------------------------

    def _peer_count(self, excluding: str) -> int:
        """Coarse presence hint (section 10): exact under 100 peers,
        rounded to the nearest 10 above that."""
        n = sum(1 for nid in self.presence if nid != excluding)
        if n < 100:
            return n
        return round(n / 10.0) * 10

    def _purge_expired_queries(self) -> None:
        # Closed queries linger briefly so a late answer is classified
        # late (and charged) instead of unknown; admission itself is
        # still governed by admission_end(). Orphaned queries ride the
        # same 60-second margin: until the sweep, their tombstone is
        # what makes a dead asker's in-window answers drop silently.
        now = self._mono()
        for query_id, oq in list(self.open_queries.items()):
            if now > oq.admission_end() + 60:
                del self.open_queries[query_id]

    async def _send(self, conn: _Connection, payload: str) -> bool:
        try:
            await conn.ws.send(payload)
            return True
        except ConnectionClosed:
            self._evict(conn)
            return False

    async def _refuse(self, conn: _Connection, reason: str) -> None:
        log.info("connection_refused", extra={"error_class": reason})
        try:
            await conn.ws.send(
                relay_message_to_json({"type": "refused", "reason": reason})
            )
        except ConnectionClosed:
            pass
        await self._close(conn)

    async def _close(self, conn: _Connection) -> None:
        try:
            await conn.ws.close(code=1008)
        except ConnectionClosed:
            pass


async def start_server(relay: Relay, host: str, port: int):
    """Start the websockets server. max_size enforces the section 10
    per-message cap at the transport; ping/pong is the heartbeat, and
    dead connections fall out of presence when their handler exits."""
    return await _ws_serve(
        relay.handle_connection,
        host,
        port,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    )


async def serve_forever(relay: Relay, host: str, port: int) -> None:
    async with await start_server(relay, host, port):
        log.info("relay_listening", extra={"host": host, "port": port})
        await asyncio.Future()

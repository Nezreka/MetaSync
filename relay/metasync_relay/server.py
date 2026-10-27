"""The relay server (DESIGN-METADATA.md section 10).

A dumb switchboard, by specification:
- presence is an in-memory dict and nothing else; a restart empties it;
- queries fan out to the other connected nodes; answers route only to
  the connection that asked, only for query IDs the relay actually
  routed, only inside the answer window plus a grace period;
- budgets are byte-based (section 10): answer bytes per node per query
  and per hour, plus a query count per hour. Late answers are dropped,
  never charged; cap hits are silent drops; violations decay;
- every send to a client goes through that connection's bounded queue
  with a per-send timeout, so one stalled asker can never freeze an
  answerer or the relay loop;
- the relay has NO database access (protocol v2): the guest-list check
  lives at the token exchange, tokens are verified offline against the
  issuer key, and binding is checked locally (the token's node_id must
  be the SHA-256 of the presented node key);
- aggregate counters only: queries routed, answers forwarded, bytes,
  connections. Nothing about query or answer content is ever logged:
  log lines carry counts and error classes only (sections 10/12).

State note: the only mutable state is presence, open queries, per-node
budget/violation counters, the held revocation list, and aggregate
counters, all in this process's memory. Counters are keyed by node id
at the relay level, not by connection: reconnecting must not launder
an hourly budget. Strikes decay and idle per-node state is pruned, so
the relay does not remember every node that ever connected.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import serve as _ws_serve
from websockets.exceptions import ConnectionClosed

from . import PROTOCOL_VERSION_MAX
from .config import RelayConfig
from .protocol import (
    MAX_FRAME_BYTES,
    Answer,
    Bye,
    Done,
    Hello,
    ProtocolError,
    Query,
    Reauth,
    answer_to_json,
    parse_message,
    query_to_json,
    relay_message_to_json,
)
from .revocations import RevocationFeed
from .tokens import encode_base64url, node_id_for_public_key, verify_access_token

log = logging.getLogger("metasync_relay")

REQUIRED_SCOPE = "meta:read"
HELLO_SIGNATURE_CONTEXT = "metasync-relay-hello"
HELLO_V2_SIGNATURE_CONTEXT = "metasync-relay-hello-v2"
REAUTH_SIGNATURE_CONTEXT = "metasync-relay-reauth"
# Mirror of NODE_TIMESTAMP_WINDOW_SECONDS in src/sin/logic.ts (section 3A.2).
NODE_TIMESTAMP_WINDOW_SECONDS = 5 * 60
HELLO_TIMEOUT_SECONDS = 30
_SWEEP_INTERVAL_SECONDS = 0.25
_PURGE_MARGIN_SECONDS = 60.0


def canonical_relay_identity(origin: str) -> str:
    """The canonical relay identity (section 10): scheme, host, port —
    lowercased, port always explicit, no path. Both sides compute the
    identical string from the dialed origin; the node package carries
    a mirror of this function."""
    parts = urlsplit(origin.strip())
    scheme = (parts.scheme or "ws").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "wss" else 80)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def hello_signing_text(token: str, timestamp: int, challenge: bytes) -> bytes:
    """The v1 canonical hello string (kept for v1 connections): binds
    the access token, a fresh timestamp, and the per-connection
    challenge nonce."""
    return (
        f"{HELLO_SIGNATURE_CONTEXT}\n{token}\n{timestamp}\n"
        f"{encode_base64url(challenge)}"
    ).encode()


def hello_v2_signing_text(
    *,
    relay_identity: str,
    mode: str,
    version_min: int,
    version_max: int,
    token: str,
    timestamp: int,
    challenge: bytes,
) -> bytes:
    """The v2 canonical hello string: additionally binds the relay's
    identity and the node's declared mode (section 10), so a captured
    hello cannot be replayed to a different relay or under a
    different mode."""
    return (
        f"{HELLO_V2_SIGNATURE_CONTEXT}\n{relay_identity}\n{mode}\n"
        f"{version_min}\n{version_max}\n{token}\n{timestamp}\n"
        f"{encode_base64url(challenge)}"
    ).encode()


def reauth_signing_text(
    *, relay_identity: str, token: str, timestamp: int
) -> bytes:
    """The canonical in-band re-authentication string (section 10):
    the fresh token and a fresh timestamp, signed by the connection's
    already-authenticated node key — and bound to THIS relay's
    identity (gate S5), so a re-auth captured at one relay cannot be
    replayed at another to keep a hijacked session alive."""
    return (
        f"{REAUTH_SIGNATURE_CONTEXT}\n{relay_identity}\n{token}\n{timestamp}"
    ).encode()


@dataclass
class RelayCounters:
    """Aggregate counters (section 10: counts, never content)."""

    queries_routed: int = 0
    queries_answered: int = 0
    answers_forwarded: int = 0
    answers_dropped: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    connections_total: int = 0
    send_drops: int = 0

    def snapshot(self, *, connections_active: int) -> dict:
        return {
            "queries_routed": self.queries_routed,
            "queries_answered": self.queries_answered,
            "share_answered": (
                round(self.queries_answered / self.queries_routed, 4)
                if self.queries_routed
                else 0.0
            ),
            "answers_forwarded": self.answers_forwarded,
            "answers_per_query": (
                round(self.answers_forwarded / self.queries_routed, 4)
                if self.queries_routed
                else 0.0
            ),
            "answers_dropped": self.answers_dropped,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "connections_total": self.connections_total,
            "connections_active": connections_active,
            "send_drops": self.send_drops,
        }


@dataclass
class _Connection:
    ws: object
    node_id: str = ""
    node_key: bytes = b""
    key_id: str = ""
    mode: str = ""
    version: int = 0  # negotiated protocol version, 0 until hello lands
    token_exp: float = 0.0  # wall-clock expiry of the current token
    challenge: bytes | None = None  # this connection's hello nonce, single use
    replaced: bool = False
    evicted: bool = False
    reauth_times: deque = field(default_factory=deque)  # monotonic attempt times
    outbox: asyncio.Queue | None = None
    outbox_bytes: int = 0  # bytes currently buffered in outbox (SF-1)
    sender_task: asyncio.Task | None = None


@dataclass
class _NodeState:
    """Per-node budgets and abuse record, keyed by node id so it
    survives reconnects. Strikes are timestamps and decay: a strike
    older than the decay window no longer counts, so honest nodes do
    not drift toward disconnection over a long-lived relay's life,
    and idle state is pruned by the sweeper once nothing is left."""

    query_times: deque = field(default_factory=deque)
    answer_bytes: deque = field(default_factory=deque)  # (monotonic, bytes)
    _answer_bytes_total: int = 0  # running sum of answer_bytes (MF-3)
    _strikes: deque = field(default_factory=deque)  # monotonic timestamps
    _decay_seconds: float = 3600.0
    _now: object = time.monotonic

    def _prune_strikes(self) -> None:
        cutoff = self._now() - self._decay_seconds  # type: ignore[operator]
        while self._strikes and self._strikes[0] <= cutoff:
            self._strikes.popleft()

    @property
    def violations(self) -> int:
        """Live (undecayed) strike count."""
        self._prune_strikes()
        return len(self._strikes)

    def add_strike(self) -> int:
        self._prune_strikes()
        self._strikes.append(self._now())  # type: ignore[operator]
        return len(self._strikes)

    def prune_budgets(self, now: float) -> None:
        cutoff = now - 3600
        while self.query_times and self.query_times[0] <= cutoff:
            self.query_times.popleft()
        while self.answer_bytes and self.answer_bytes[0][0] <= cutoff:
            self._answer_bytes_total -= self.answer_bytes.popleft()[1]

    def record_answer_bytes(self, now: float, size: int) -> None:
        """Meter admitted answer bytes. O(1): the rolling total is
        maintained incrementally, never re-summed per answer."""
        self.prune_budgets(now)
        self.answer_bytes.append((now, size))
        self._answer_bytes_total += size

    def answer_bytes_total(self) -> int:
        return self._answer_bytes_total

    def is_idle(self, now: float) -> bool:
        self.prune_budgets(now)
        return (
            not self.query_times
            and not self.answer_bytes
            and self.violations == 0
        )


@dataclass
class _OpenQuery:
    query_id: str
    asker: _Connection
    deadline: float  # end of the answer window (monotonic)
    grace_seconds: float
    expected: dict  # node id -> the connection the query was routed to
    # Every node id the query was ever routed to. Unlike `expected`
    # (whose dead connections are evicted), this never shrinks: it is
    # what lets a node that reconnects mid-window reclaim its seat
    # (SF-4) instead of being struck as uninvited.
    expected_nodes: set = field(default_factory=set)
    responded: set = field(default_factory=set)
    done_from: set = field(default_factory=set)
    per_node_bytes: dict = field(default_factory=dict)
    bytes_used: int = 0
    answered: bool = False
    # The query carried a reply_key: its answers are sealed or they
    # are not forwarded at all (gate S1).
    sealed_query: bool = False
    complete_sent: bool = False
    # Set once, when every expected node has signalled done (answers
    # alone never set it: section 10 — a node's first answer does NOT
    # close its stream, only its done or the window does).
    all_responded_at: float | None = None
    # Set once, when the window is decided over: the all-done time, or
    # the deadline when the sweeper completes by timeout. Admission end
    # is derived from this and never re-based by later traffic.
    complete_time: float | None = None
    orphaned_at: float | None = None  # set when the asker's connection dies

    def admission_end(self) -> float:
        """Answers are admitted until close plus grace — the one
        definition (section 10). Close is fixed once decided: later
        answers or dones never move it."""
        base = self.complete_time if self.complete_time is not None else self.deadline
        return base + self.grace_seconds

    def note_done(self, node_id: str, now: float) -> None:
        """Record a done. When every expected node has signalled done,
        the window is complete as of now (fixed, once)."""
        if (
            self.all_responded_at is None
            and self.expected
            and set(self.expected) <= self.done_from
        ):
            self.all_responded_at = now


class Relay:
    def __init__(
        self,
        pool_public_key: Ed25519PublicKey,
        config: RelayConfig | None = None,
        *,
        issuer_public_key: Ed25519PublicKey | None = None,
        revocation_feed: RevocationFeed | None = None,
        wall_clock=time.time,
        monotonic=time.monotonic,
    ) -> None:
        self.pool_public_key = pool_public_key
        self.config = config or RelayConfig()
        self._wall = wall_clock
        self._mono = monotonic
        self.presence: dict[str, _Connection] = {}
        self.open_queries: dict[str, _OpenQuery] = {}
        # Tombstones of queries this relay DID route and has since
        # purged (query id -> monotonic purge time), pruned after an
        # hour. An answer that arrives after the purge is late, not
        # uninvited (gate S4): without the tombstone it would be
        # struck as unknown_query, and honest slowness is not an
        # abuse signal (section 10).
        self._purged_queries: dict[str, float] = {}
        self.node_state: dict[str, _NodeState] = {}
        self.counters = RelayCounters()
        if revocation_feed is not None:
            self.revocations = revocation_feed
        elif self.config.issuer_id or self.config.revocations_url:
            self.revocations = RevocationFeed(
                self.config.issuer_id,
                issuer_public_key or pool_public_key,
                self.config.revocations_url,
            )
        else:
            self.revocations = None
        self._sweeper_task: asyncio.Task | None = None
        self._last_revocations_refresh = 0.0
        self._last_counters_log = 0.0

    def _state_for(self, node_id: str) -> _NodeState:
        state = self.node_state.get(node_id)
        if state is None:
            state = _NodeState(
                _decay_seconds=self.config.violation_decay_seconds,
                _now=self._mono,
            )
            self.node_state[node_id] = state
        return state

    def counters_snapshot(self) -> dict:
        return self.counters.snapshot(connections_active=len(self.presence))

    # -- connection lifecycle ------------------------------------------

    async def handle_connection(self, ws) -> None:
        conn = _Connection(ws=ws)
        try:
            if not await self._read_hello(conn):
                return
            async for raw in ws:
                if conn.replaced:
                    break
                self.counters.bytes_in += (
                    len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                )
                if not await self._dispatch(conn, raw):
                    break
        except ConnectionClosed:
            pass
        finally:
            if conn.sender_task is not None:
                conn.sender_task.cancel()
            self._evict(conn)

    async def _read_hello(self, conn: _Connection) -> bool:
        # Issue this connection's challenge before anything else: a
        # fresh 32-byte nonce, valid for this one hello attempt only.
        conn.challenge = secrets.token_bytes(32)
        sent = await self._send_direct(
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
        # Take the presence slot BEFORE closing any stale connection
        # for this key, and never await that close inline: a half-open
        # old socket must not delay this welcome, and the ghost's own
        # eviction must find the slot (and the query expectations)
        # already pointing at the live connection.
        old = self.presence.get(conn.node_id)
        self.presence[conn.node_id] = conn
        if old is not None and old is not conn:
            old.replaced = True
            asyncio.ensure_future(self._close(old))
        self.counters.connections_total += 1
        log.info("node_connected", extra={"peers": len(self.presence)})
        self._start_sender(conn)
        welcome: dict[str, object] = {
            "type": "welcome",
            "peers": self._peer_count(excluding=conn.node_id),
        }
        if conn.version >= 2:
            welcome["version"] = conn.version
            welcome["relay"] = self._relay_identity(conn)
            welcome["capabilities"] = {
                "sealing": ["hpke"],
                "encodings": ["zstd-dict1", "none"],
            }
            # The asker's post-complete grace must match this
            # relay's admission end (gate S3): without the number,
            # the asker closed its stream ~1s after `complete`
            # while the relay kept admitting (and metering) answers
            # for the full grace — charged bytes that could never
            # land in any cache.
            welcome["grace_seconds"] = self.config.grace_seconds
            if self.revocations is not None and self.revocations.current is not None:
                welcome["revocations"] = self.revocations.current.to_message()
        self._enqueue(conn, relay_message_to_json(welcome))
        return True

    def _relay_identity(self, conn: _Connection) -> str:
        """This relay's canonical identity for the hello binding:
        the configured origin (RELAY_ORIGIN). Since gate M5, v2
        authentication REQUIRES the configured origin, so on every
        authenticated v2 path this returns exactly that; the
        header-derived fallback below survives only for direct
        callers (and its unit test) and is never what a v2
        signature is checked against — a client-supplied Host
        header is not an identity. (The derivation, kept for the
        record: the scheme comes from the connection — the
        forwarded proto when a proxy terminated TLS, else the
        plain ws this server speaks.)"""
        if self.config.relay_origin:
            return canonical_relay_identity(self.config.relay_origin)
        host = ""
        scheme = "ws"
        request = getattr(conn.ws, "request", None)
        if request is not None:
            try:
                host = request.headers.get("Host", "") or ""
                forwarded = (
                    request.headers.get("X-Forwarded-Proto", "") or ""
                ).split(",")[0].strip().lower()
                if forwarded in ("ws", "wss"):
                    scheme = forwarded
            except Exception:  # noqa: BLE001 - identity falls back below
                host = ""
        if host:
            return canonical_relay_identity(f"{scheme}://{host}")
        return canonical_relay_identity(
            f"ws://{self.config.host}:{self.config.port}"
        )

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
        if self._is_revoked(node_id):
            # The issuer's signed list names this node: refused with
            # the same single reason class as every other failure.
            return False
        if abs(self._wall() - hello.timestamp) > NODE_TIMESTAMP_WINDOW_SECONDS:
            return False
        version = min(hello.version_max, PROTOCOL_VERSION_MAX)
        if version < hello.version_min or version < 1:
            return False
        if version >= 2 and not self.config.relay_origin:
            # Fail closed (gate M5): with no configured origin, the
            # identity the v2 signature binds would be computed from
            # this request's Host / X-Forwarded-Proto headers —
            # values the connecting party itself supplies. A
            # malicious relay could pass a victim relay's challenge
            # to a node, collect a hello signed for the victim's
            # identity as derived from ITS OWN headers, and replay
            # it at the victim to impersonate the node — the exact
            # federation hole the binding exists to close. So v2
            # requires RELAY_ORIGIN; an unconfigured relay serves
            # v1 only.
            return False
        if version >= 2:
            signing = hello_v2_signing_text(
                relay_identity=self._relay_identity(conn),
                mode=hello.mode,
                version_min=hello.version_min,
                version_max=hello.version_max,
                token=hello.token,
                timestamp=hello.timestamp,
                challenge=hello.challenge,
            )
        else:
            signing = hello_signing_text(hello.token, hello.timestamp, hello.challenge)
        try:
            Ed25519PublicKey.from_public_bytes(hello.node_key).verify(
                hello.signature, signing
            )
        except InvalidSignature:
            return False
        except Exception:  # noqa: BLE001 - any crypto failure rejects the hello
            return False
        # No database, no guest-list peek: the exchange checked the
        # binding when it issued this token; the token's node_id
        # matching the presented key (checked above) is the binding,
        # verified offline (section 10 Identity).
        conn.node_id = node_id
        conn.node_key = hello.node_key
        conn.key_id = claims.key_id
        conn.mode = hello.mode
        conn.version = version
        conn.token_exp = float(claims.exp)
        return True

    def _evict(self, conn: _Connection) -> None:
        if conn.evicted:
            return
        conn.evicted = True
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
                elif oq.expected.get(conn.node_id) is conn:
                    # Expectations are keyed by connection. If the node
                    # already reconnected, the expectation transfers to
                    # its live connection — eviction removes only the
                    # queries THIS connection was routed, never the
                    # same node's live connection's, and a reconnecting
                    # node's later answers are not uninvited. A
                    # disconnect is neither a done nor the window
                    # ending: it never completes a query early.
                    live = self.presence.get(conn.node_id)
                    if live is not None and live is not conn and not live.evicted:
                        oq.expected[conn.node_id] = live
                    else:
                        del oq.expected[conn.node_id]
                if oq.orphaned_at is not None and not oq.expected:
                    # No responder left who could still answer: the
                    # tombstone has nothing left to absorb. The query
                    # joins the purged set (gate S4), so a responder's
                    # late answer to it is a silent drop, not a strike.
                    del self.open_queries[query_id]
                    self._purged_queries[query_id] = now

    # -- send path --------------------------------------------------------

    def _start_sender(self, conn: _Connection) -> None:
        conn.outbox = asyncio.Queue(maxsize=self.config.send_queue_messages)
        conn.outbox_bytes = 0
        conn.sender_task = asyncio.ensure_future(self._sender_loop(conn))

    async def _sender_loop(self, conn: _Connection) -> None:
        """One sender per connection: every relay->client send drains
        through here with a per-send timeout, so a peer that stops
        reading stalls only its own queue — never an answerer, never
        the loop. A send that times out ends the connection: the
        stalled peer loses its session, not everyone's answers."""
        try:
            while True:
                payload = await conn.outbox.get()  # type: ignore[union-attr]
                size = len(payload.encode("utf-8"))
                try:
                    await asyncio.wait_for(
                        conn.ws.send(payload),
                        timeout=self.config.send_timeout_seconds,
                    )
                except TimeoutError:
                    log.warning(
                        "send_timeout", extra={"error_class": "send_timeout"}
                    )
                    await self._close(conn)
                    return
                except ConnectionClosed:
                    return
                finally:
                    conn.outbox_bytes -= size
                self.counters.bytes_out += size
        except asyncio.CancelledError:
            pass
        finally:
            self._evict(conn)

    def _enqueue(self, conn: _Connection, payload: str) -> bool:
        """Queue a message for a connection. Drop-on-full: a bounded
        queue behind a stalled peer must shed load, not grow. The
        bound is in messages AND bytes: a queue of maximum-size frames
        would otherwise pin ~1GB behind one slow asker."""
        if conn.outbox is None or conn.evicted:
            return False
        size = len(payload.encode("utf-8"))
        if conn.outbox_bytes + size > self.config.send_queue_bytes:
            self.counters.send_drops += 1
            return False
        try:
            conn.outbox.put_nowait(payload)
        except asyncio.QueueFull:
            self.counters.send_drops += 1
            return False
        conn.outbox_bytes += size
        return True

    async def _send_direct(self, conn: _Connection, payload: str) -> bool:
        """Pre-sender sends (challenge, pre-hello refusals): the only
        direct socket writes left, on connections that are not yet —
        or no longer — part of the routed network. Bounded by the
        same per-send timeout as the queued path."""
        try:
            await asyncio.wait_for(
                conn.ws.send(payload),
                timeout=self.config.send_timeout_seconds,
            )
            return True
        except (TimeoutError, ConnectionClosed):
            return False

    # -- message routing ------------------------------------------------

    async def _dispatch(self, conn: _Connection, raw: str | bytes) -> bool:
        """Returns False when the connection should close."""
        try:
            msg = parse_message(raw)
        except ProtocolError as exc:
            if exc.reason == "unknown_type" and conn.version >= 2:
                # Forward compatibility: a v2 peer speaking a message
                # type this relay predates is ignored, not punished.
                return True
            return await self._violate(conn, exc.reason)
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
        if isinstance(msg, Reauth):
            return await self._route_reauth(conn, msg)
        return True

    async def _route_query(self, conn: _Connection, query: Query) -> bool:
        """Returns False when a violation closed the connection."""
        # The duplicate check runs before the budget charge: an honest
        # retry of an in-flight id must not consume query budget.
        existing = self.open_queries.get(query.query_id)
        if existing is not None:
            if existing.asker.node_id == conn.node_id:
                # The honest retry: the same node re-asking an id it
                # already has in flight (classically after a reconnect,
                # against the tombstone). A notice, never a strike.
                self._enqueue(
                    conn,
                    relay_message_to_json(
                        {
                            "type": "query_refused",
                            "query_id": query.query_id,
                            "reason": "duplicate_query",
                        }
                    ),
                )
                return True
            return await self._violate(conn, "query_id_in_use")
        state = self._state_for(conn.node_id)
        if not self._charge(state.query_times, self.config.queries_per_hour):
            # The asker learns at once (a notice, not silence) so it
            # can end the stream and back off; the strike still lands.
            self._enqueue(
                conn,
                relay_message_to_json(
                    {
                        "type": "query_refused",
                        "query_id": query.query_id,
                        "reason": "query_budget",
                    }
                ),
            )
            return await self._violate(conn, "query_budget")
        now = self._mono()
        # Leech-mode nodes (protocol NODE_MODES) never answer: they are
        # excluded from the fan-out and from the expected set, so they
        # cannot delay the all-done early close either.
        expected = {
            nid: c
            for nid, c in self.presence.items()
            if c is not conn and c.mode != "leech"
        }
        if conn.version >= 2 and query.reply_key is not None:
            # No silent confidentiality downgrade (section 10): answers
            # to a query carrying a reply_key are sealed to it, and a
            # v1 peer cannot seal — its answers would come back as
            # plaintext. Sealed queries fan out to v2 peers only.
            expected = {
                nid: c for nid, c in expected.items() if c.version >= 2
            }
        oq = _OpenQuery(
            query_id=query.query_id,
            asker=conn,
            deadline=now + self.config.window_seconds,
            grace_seconds=self.config.grace_seconds,
            expected=expected,
            expected_nodes=set(expected),
            sealed_query=query.reply_key is not None,
        )
        if not expected:
            oq.all_responded_at = now
        self.open_queries[query.query_id] = oq
        self.counters.queries_routed += 1
        # Enqueue, never await: a stuck peer's send timeout is its own
        # sender loop's problem, not the asker's handler loop's.
        for peer in expected.values():
            self._enqueue(peer, query_to_json(query, peer.version))
        if not expected:
            self._maybe_send_complete(oq, now)
        return True

    async def _route_answer(
        self, conn: _Connection, answer: Answer, size: int
    ) -> bool:
        """Returns False when a violation closed the connection."""
        oq = self.open_queries.get(answer.query_id)
        if oq is None:
            if answer.query_id in self._purged_queries:
                # Routed once, purged since: a very late answer is
                # dropped silently, never struck (gate S4).
                return True
            return await self._violate(conn, "unknown_query")
        if oq.orphaned_at is not None:
            return True  # asker is gone: silent drop, never a violation
        if oq.asker is conn:
            return await self._violate(conn, "self_answer")
        if oq.expected.get(conn.node_id) is not conn:
            if (
                conn.node_id in oq.expected_nodes
                and self.presence.get(conn.node_id) is conn
            ):
                # The node reconnected mid-window and answers on its
                # live connection: the expectation follows the node,
                # not the dead socket. Not uninvited, never a strike.
                oq.expected[conn.node_id] = conn
            else:
                # The query was never routed to this node (it
                # connected after the fan-out, or was not a recipient):
                # its answer is uninvited, however well-formed.
                return await self._violate(conn, "unrouted_answer")
        if self._mono() > oq.admission_end():
            # Late: dropped, never charged, never a strike (section
            # 10: honest slowness is not an abuse signal).
            self.counters.answers_dropped += 1
            return True
        if oq.sealed_query and answer.sealed is None:
            # A plaintext answer to a sealed query is a downgrade:
            # dropped here, BEFORE admission metering, not at
            # forward time (gate re-pass S-b). An answer that can
            # never be forwarded is not charged to the answerer's
            # budgets, does not mark it responded, and does not
            # count the query as answered — the counters must not
            # report a dropped downgrade as an answered query.
            # Silent, like every other pre-admission drop.
            self.counters.answers_dropped += 1
            return True
        # Admission first, metering second (MF-2): answers the caps
        # drop are not charged to the answerer at all — hitting the
        # total cap is a silent drop, not a charge against whoever
        # arrived last, and cap-dropped bytes must never ripen into a
        # budget strike. Only admitted bytes are metered.
        node_used = oq.per_node_bytes.get(conn.node_id, 0)
        if node_used + size > self.config.answer_node_query_cap_bytes:
            # This node's share of the query is spent: silent drop.
            self.counters.answers_dropped += 1
            return True
        if oq.bytes_used + size > self.config.answer_cap_bytes:
            # The query's total cap: likewise a silent, uncharged drop.
            self.counters.answers_dropped += 1
            return True
        state = self._state_for(conn.node_id)
        state.prune_budgets(self._mono())
        if state.answer_bytes_total() + size > self.config.answer_bytes_per_hour:
            self.counters.answers_dropped += 1
            return await self._violate(conn, "answer_byte_budget")
        state.record_answer_bytes(self._mono(), size)
        oq.per_node_bytes[conn.node_id] = node_used + size
        oq.bytes_used += size
        oq.responded.add(conn.node_id)
        if not oq.answered:
            oq.answered = True
            self.counters.queries_answered += 1
        if self._forward_answer(oq, conn, answer):
            self.counters.answers_forwarded += 1
        else:
            self.counters.answers_dropped += 1
        return True

    def _forward_answer(self, oq: _OpenQuery, conn: _Connection, answer: Answer) -> bool:
        """Serialize an admitted answer for the asker's protocol
        version and queue it. Returns True when the answer actually
        reached the asker's queue (counters count what moved, SF-5).
        The batch passes through untouched in content; a sealed
        payload passes through as the bytes it is — the relay stamps
        the envelope and never needs the plaintext."""
        asker = oq.asker
        if oq.sealed_query and answer.sealed is None:
            # The query carried a reply key, so the asker asked for
            # sealed answers — and the fan-out already excluded v1
            # peers who cannot seal. A plaintext answer anyway (a
            # v2 peer answering in the clear) is never forwarded
            # (gate S1): no hop may downgrade a sealed query.
            # _route_answer drops this case before metering (gate
            # re-pass S-b); this check is the last line if that
            # ordering ever moves.
            return False
        if answer.sealed is not None:
            if asker.version >= 2:
                return self._enqueue(
                    asker,
                    answer_to_json(
                        query_id=answer.query_id,
                        stamp_from=conn.node_id,
                        sealed=answer.sealed,
                    ),
                )
            # Cannot happen in practice (a v1 query carries no
            # reply key, so nothing answering it can seal); drop
            # rather than downgrade ciphertext to plaintext.
            return False
        if answer.batches is not None:
            if asker.version >= 2:
                return self._enqueue(
                    asker,
                    answer_to_json(
                        query_id=answer.query_id,
                        stamp_from=conn.node_id,
                        batches=answer.batches,
                    ),
                )
            delivered = False
            for batch in answer.batches:
                if self._enqueue(
                    asker,
                    answer_to_json(
                        query_id=answer.query_id,
                        stamp_from=conn.node_id,
                        batch=batch,
                    ),
                ):
                    delivered = True
            return delivered
        if asker.version >= 2:
            return self._enqueue(
                asker,
                answer_to_json(
                    query_id=answer.query_id,
                    stamp_from=conn.node_id,
                    batches=(answer.batch,),
                ),
            )
        return self._enqueue(
            asker,
            answer_to_json(
                query_id=answer.query_id,
                stamp_from=conn.node_id,
                batch=answer.batch,
            ),
        )

    async def _route_done(self, conn: _Connection, done: Done) -> bool:
        """Returns False when a violation closed the connection."""
        oq = self.open_queries.get(done.query_id)
        if oq is None:
            return True  # liveness-adjacent: dropped silently, never charged
        if oq.orphaned_at is not None:
            return True  # asker is gone: silent drop, like answers
        if oq.expected.get(conn.node_id) is not conn:
            if (
                conn.node_id in oq.expected_nodes
                and self.presence.get(conn.node_id) is conn
            ):
                # Reconnected mid-window: the expectation follows the
                # node to its live connection (see _route_answer).
                oq.expected[conn.node_id] = conn
            else:
                # Same admission rule as answers: a done from a node
                # the query was never routed to (the asker included)
                # is uninvited and charged.
                return await self._violate(conn, "unrouted_done")
        if conn.node_id in oq.done_from:
            return True  # repeats are deduped: dropped, never amplified
        oq.done_from.add(conn.node_id)
        now = self._mono()
        if now > oq.admission_end():
            return True
        oq.note_done(conn.node_id, now)
        if oq.asker.version < 2:
            # v1 askers are told about dones directly; v2 askers get
            # the relay's own complete signal instead (section 10:
            # the asker never counts dones itself).
            self._enqueue(
                oq.asker,
                relay_message_to_json({"type": "done", "query_id": done.query_id}),
            )
        if oq.all_responded_at is not None:
            self._maybe_send_complete(oq, now)
        return True

    def _maybe_send_complete(self, oq: _OpenQuery, now: float) -> None:
        """The relay's window-over signal for v2 askers: every routed
        node has signalled done, or the window has run out (the
        sweeper calls this at the deadline). Sent exactly once per
        query; the completion time is fixed here and never re-based
        by later traffic (MF-1)."""
        if oq.complete_sent or oq.orphaned_at is not None:
            return
        if oq.all_responded_at is None and now < oq.deadline:
            return  # neither all-done nor out of window: not complete
        oq.complete_sent = True
        oq.complete_time = (
            oq.all_responded_at if oq.all_responded_at is not None else oq.deadline
        )
        if oq.asker.version >= 2:
            self._enqueue(
                oq.asker,
                relay_message_to_json(
                    {"type": "complete", "query_id": oq.query_id}
                ),
            )

    async def _route_reauth(self, conn: _Connection, reauth: Reauth) -> bool:
        """In-band re-authentication (section 10): a fresh exchange
        token presented over the live connection before expiry, so
        sessions survive their tokens. A revoked key fails here —
        the exchange will not issue it a token — and its session ends
        at its old token's expiry at the latest."""
        if conn.version < 2:
            return await self._violate(conn, "unexpected_reauth")
        # Rate limit (gate M4): a healthy client re-auths about once
        # per token lifetime. A client spinning on a cached token
        # (the unfixed M4 client) would otherwise cost this relay
        # two Ed25519 verifications per round trip for as long as it
        # cares to spin. Excess attempts are refused — the client
        # tears the session down and reconnects with a fresh hello,
        # which is the honest recovery — and each one is a strike.
        now_mono = self._mono()
        cutoff = now_mono - self.config.reauth_window_seconds
        while conn.reauth_times and conn.reauth_times[0] <= cutoff:
            conn.reauth_times.popleft()
        if len(conn.reauth_times) >= self.config.reauth_max_per_window:
            if not await self._violate(conn, "reauth_rate"):
                return False
            self._enqueue(
                conn, relay_message_to_json({"type": "refused", "reason": "auth"})
            )
            return True
        conn.reauth_times.append(now_mono)
        claims = verify_access_token(
            reauth.token, self.pool_public_key, now=self._wall()
        )
        ok = (
            claims is not None
            and REQUIRED_SCOPE in claims.scopes
            and claims.node_id == conn.node_id
            and not self._is_revoked(conn.node_id)
            and abs(self._wall() - reauth.timestamp) <= NODE_TIMESTAMP_WINDOW_SECONDS
        )
        if ok:
            try:
                Ed25519PublicKey.from_public_bytes(conn.node_key).verify(
                    reauth.signature,
                    reauth_signing_text(
                        relay_identity=self._relay_identity(conn),
                        token=reauth.token,
                        timestamp=reauth.timestamp,
                    ),
                )
            except Exception:  # noqa: BLE001 - any crypto failure refuses
                ok = False
        if not ok:
            # One reason class, like hello: no oracle. The refusal
            # goes through the connection's send queue (SF-7): the
            # sender task owns the socket once hello has landed, and a
            # second direct writer could interleave frames with it.
            self._enqueue(
                conn, relay_message_to_json({"type": "refused", "reason": "auth"})
            )
            await self._close(conn)
            return False
        conn.key_id = claims.key_id  # type: ignore[union-attr]
        conn.token_exp = float(claims.exp)  # type: ignore[union-attr]
        self._enqueue(
            conn,
            relay_message_to_json({"type": "reauth_ok", "exp": claims.exp}),  # type: ignore[union-attr]
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
        """Charge a protocol/budget violation (decaying: _NodeState).
        Returns False when the connection has crossed the abuse
        threshold and should close."""
        state = self._state_for(conn.node_id)
        count = state.add_strike()
        log.warning("violation", extra={"error_class": reason, "count": count})
        if count > self.config.max_violations:
            await self._close(conn)
            return False
        return True

    # -- sweeper ----------------------------------------------------------

    def _start_sweeper(self) -> None:
        if self._sweeper_task is None:
            self._sweeper_task = asyncio.ensure_future(self._sweeper_loop())

    async def aclose(self) -> None:
        """Stop the sweeper and close every connection (shutdown and
        test teardown; a serving relay runs until killed)."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        for conn in list(self.presence.values()):
            await self._close(conn)

    async def _sweeper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                try:
                    self._sweep_once()
                except Exception:  # noqa: BLE001 - the sweeper must not die
                    log.warning("sweep_error", extra={"error_class": "sweep"})
                await self._refresh_revocations_if_due()
                self._log_counters_if_due()
        except asyncio.CancelledError:
            pass

    def _sweep_once(self) -> None:
        now = self._mono()
        wall = self._wall()
        # Complete-by-deadline and the query purge.
        for query_id, oq in list(self.open_queries.items()):
            if not oq.complete_sent and now >= oq.deadline:
                self._maybe_send_complete(oq, now)
            if now > oq.admission_end() + _PURGE_MARGIN_SECONDS:
                del self.open_queries[query_id]
                self._purged_queries[query_id] = now
        # Tombstones live an hour: long enough that any answer still
        # in flight when its query was purged has landed or died,
        # short enough that the map stays a rounding error.
        for query_id, purged_at in list(self._purged_queries.items()):
            if now - purged_at > 3600:
                del self._purged_queries[query_id]
        # Sessions end at their token's expiry (plus a short grace
        # for the re-authentication exchange) when no re-auth landed.
        for conn in list(self.presence.values()):
            if wall > conn.token_exp + self.config.reauth_grace_seconds:
                log.info(
                    "session_expired", extra={"error_class": "token_expired"}
                )
                asyncio.ensure_future(self._close(conn))
        # Prune per-node state that has fully decayed: the relay does
        # not keep a roll of every node that ever connected.
        for node_id, state in list(self.node_state.items()):
            if node_id not in self.presence and state.is_idle(now):
                del self.node_state[node_id]

    async def _refresh_revocations_if_due(self) -> None:
        if self.revocations is None or not self.revocations.url:
            return
        now = self._mono()
        if now - self._last_revocations_refresh < self.config.revocations_interval_seconds:
            return
        self._last_revocations_refresh = now
        await self.refresh_revocations()

    async def refresh_revocations(self) -> bool:
        """Fetch the issuer's current revocation list and, when it
        changed, push it to every live v2 connection. The fetch runs
        off the event loop (it is a blocking HTTPS GET)."""
        if self.revocations is None:
            return False
        changed = await asyncio.to_thread(self.revocations.refresh)
        if changed and self.revocations.current is not None:
            payload = relay_message_to_json(
                {"type": "revocations", **self.revocations.current.to_message()}
            )
            for conn in list(self.presence.values()):
                if self._is_revoked(conn.node_id):
                    # A node the new list names loses its seat now,
                    # not at its token's expiry.
                    asyncio.ensure_future(self._close(conn))
                elif conn.version >= 2:
                    self._enqueue(conn, payload)
        return changed

    def _is_revoked(self, node_id: str) -> bool:
        feed = self.revocations
        return (
            feed is not None
            and feed.current is not None
            and node_id in feed.current.keys
        )

    def _log_counters_if_due(self) -> None:
        interval = self.config.counters_log_seconds
        if not interval:
            return
        now = self._mono()
        if now - self._last_counters_log < interval:
            return
        self._last_counters_log = now
        log.info("counters", extra=self.counters_snapshot())

    # -- helpers ----------------------------------------------------------

    def _peer_count(self, excluding: str) -> int:
        """Coarse presence hint (section 10): exact under 100 peers,
        rounded to the nearest 10 above that."""
        n = sum(1 for nid in self.presence if nid != excluding)
        if n < 100:
            return n
        return round(n / 10.0) * 10

    async def _refuse(self, conn: _Connection, reason: str) -> None:
        log.info("connection_refused", extra={"error_class": reason})
        await self._send_direct(
            conn, relay_message_to_json({"type": "refused", "reason": reason})
        )
        await self._close(conn)

    async def _close(self, conn: _Connection) -> None:
        try:
            await conn.ws.close(code=1008)
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001, S110 - closing is best-effort
            pass


async def start_server(relay: Relay, host: str, port: int):
    """Start the websockets server. max_size enforces the frame cap at
    the transport; ping/pong is the heartbeat, and dead connections
    fall out of presence when their handler exits."""
    if relay.revocations is None:
        # Loud, on purpose (SF-6): with no issuer id / revocations URL
        # configured, this relay does no hello/reauth revocation
        # checks and carries no list in welcome. That must never be a
        # silent default — the token exchange remains the real gate
        # (it refuses to issue to revoked keys), but the relay-side
        # defense in depth is OFF.
        log.warning(
            "revocation_enforcement_off: no RELAY_ISSUER_ID or "
            "RELAY_REVOCATIONS_URL configured; the token exchange is "
            "the only revocation gate for this relay",
            extra={"error_class": "revocations_unconfigured"},
        )
    server = await _ws_serve(
        relay.handle_connection,
        host,
        port,
        max_size=MAX_FRAME_BYTES,
        ping_interval=20,
        ping_timeout=20,
    )
    relay._start_sweeper()
    return server


async def serve_forever(relay: Relay, host: str, port: int) -> None:
    async with await start_server(relay, host, port):
        log.info("relay_listening", extra={"host": host, "port": port})
        await asyncio.Future()

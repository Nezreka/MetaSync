"""Persistent relay client for the metadata network (DESIGN-METADATA.md
section 10).

The node is a synchronous Flask/threads program, so this client owns one
daemon thread running a private asyncio loop. Everything else in the node
talks to it through plain blocking calls (``query`` returns a stream of
batches; ``status`` returns a dict), never through asyncio.

Why the asyncio client, not ``websockets.sync``: the sync client hides
its own internal thread per connection and its receive loop fights the
routing this client needs (answers demultiplexed by query_id, the
answering handler run off the read loop, heartbeat handled by the
library). One private loop, one reader coroutine, one sender task per
connection is the shape with the fewest moving parts. The relay itself
uses the asyncio API, so the two sides share ping/pong behaviour.

Protocol mirror (byte-for-byte where it matters): the hello signature
is over ``"metasync-relay-hello\\n" + token + "\\n" + timestamp + "\\n"
+ base64url(challenge)``, exactly as relay/metasync_relay/server.py
``hello_signing_text`` computes it. The node package cannot import the
relay package (separate deployment), so the context string and the
protocol version are duplicated here with that comment as the anchor.

The client never parses claims: answer batches are opaque dicts,
forwarded to the caller as received. Verification and merge are the
caller's business (steps 4 and 5 of the build order).

Protocol v2 (step 5): the hello offers a version RANGE and signs the
relay identity and mode as well; answers arrive packed (one message,
a ``batches`` array) or sealed (HPKE to the query's ephemeral reply
key, over the zstd-dictionary-compressed signed payload — the relay
forwards those as opaque bytes and never sees plaintext); the relay
stamps every forwarded answer with the answerer's node id, checked
against the batch before any signature verification; a ``complete``
message ends a query's stream early; tokens are refreshed in-band
(``reauth``) before expiry so a session outlives its first token;
and issuer-signed revocation lists arrive in the welcome and as
``revocations`` pushes, verified here against the issuer key and
kept at the highest sequence seen. Compression is a negotiated
capability and degrades to the plain encoding; sealing does not
degrade — a query carrying a reply key is answered sealed or not
at all, on every hop (gate S1).

The ``websockets`` package is an optional dependency of this feature
only, so it is imported lazily inside the connect path: a node built
without it must still boot and run everything else, with the metadata
feature alone reporting the missing package as its last error.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import queue
import random
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .identity import NodeIdentity, b64url_decode, b64url_encode
from .meta import sealing
from .revocations import RevocationStore

RELAY_PROTOCOL_VERSION = 1  # lowest version this client offers
RELAY_PROTOCOL_VERSION_MAX = 2
HELLO_SIGNATURE_CONTEXT = "metasync-relay-hello"
HELLO_V2_SIGNATURE_CONTEXT = "metasync-relay-hello-v2"
REAUTH_SIGNATURE_CONTEXT = "metasync-relay-reauth"
HELLO_TIMEOUT_SECONDS = 30.0
# The frame cap matches the relay's: one frame may carry one packed
# or sealed answer message.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0

# Outbound answer packing: batches are packed into messages whose
# plaintext stays under this, so the wire frame (even sealed, which
# adds only a fixed 48 bytes) stays well under the frame cap.
WIRE_CHUNK_BYTES = 4 * 1024 * 1024

# Re-authenticate this long before the token expires; the relay keeps
# a further grace (RELAY_REAUTH_GRACE_SECONDS, 30s by default) for a
# re-auth that lands just after expiry.
REAUTH_LEAD_SECONDS = 120.0

# After the relay's `complete`, answers already in flight may still
# land for the relay's grace period (they still land in the asker's
# cache, §10); the stream drains what it has, then ends once that
# grace has passed since complete. The grace is the RELAY's,
# learned from its welcome (gate S3): the relay admits — and meters
# — answers until complete + its grace, so closing earlier strands
# charged answers that can never land. DEFAULT_GRACE_SECONDS is
# the fallback for a welcome that does not say.

NODE_MODES = ("full", "own_library", "leech")
DEFAULT_MODE = "own_library"
DEFAULT_WINDOW_SECONDS = 6.0
DEFAULT_GRACE_SECONDS = 10.0
# Cap on the relay-advertised grace adopted from the welcome. The
# relay is untrusted transport: its grace feeds max() in the asker's
# stream deadline, so an uncapped value — json.loads accepts
# Infinity and 1e300 — would let a hostile or compromised relay
# stall a query stream forever. 60s is comfortably above the
# relay's own default of 10s (relay/metasync_relay/config.py).
MAX_RELAY_GRACE_SECONDS = 60.0

_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_REFUSED_FLOOR_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 60.0

# A session must live at least this long before its end resets the
# reconnect backoff. Without the floor, an accept-and-close relay (or
# a flapping network path) earns a fresh ~1s retry forever, because
# every clean close would look like a healthy session.
_MIN_SESSION_SECONDS = 30.0

# Per-query buffering caps on the asker side. The relay caps admitted
# answer bytes per query too; these stop a fast answerer plus a slow
# (or absent) consumer from growing a query's queue without bound.
MAX_QUEUED_BATCHES_PER_QUERY = 256
MAX_QUEUED_BYTES_PER_QUERY = 8 * 1024 * 1024

# The answering handler's iterable is materialized in a worker thread
# (never on the read loop); this bounds what one query can make that
# thread produce.
MAX_HANDLER_BATCHES_PER_QUERY = 1000


def _materialize_handler(
    handler: Callable[[dict[str, Any]], Iterable[dict[str, Any]] | None],
    msg: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Call the answering handler AND consume its iterable, in the
    worker thread. A handler returning a lazy generator would
    otherwise do its real work wherever the result is iterated — the
    read loop — stalling heartbeats and every other query's routing.
    """
    result = handler(msg)
    if result is None:
        return None
    batches: list[dict[str, Any]] = []
    for batch in result:
        batches.append(batch)
        if len(batches) >= MAX_HANDLER_BATCHES_PER_QUERY:
            break
    return batches


def _swallow_future_exception(fut: Any) -> None:
    """Done-callback for an abandoned token fetch: retrieving the
    exception keeps the loop from logging it as never-retrieved."""
    if not fut.cancelled():
        fut.exception()


def _chunk_batches(
    batches: list[dict[str, Any]], limit: int
) -> list[list[dict[str, Any]]]:
    """Split batches into chunks whose combined JSON stays under
    ``limit`` bytes (a single oversize batch travels alone)."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for batch in batches:
        bsize = (
            len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            + 1
        )
        if current and size + bsize > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(batch)
        size += bsize
    if current:
        chunks.append(current)
    return chunks


def _pack_answer_messages(
    query_msg: dict[str, Any], batches: list[dict[str, Any]]
) -> list[str]:
    """Pack one query's answer batches into wire messages (protocol
    v2: ONE packed message per chunk, never one message per batch).

    When the query carries a reply key, each chunk's payload is
    compressed (the negotiated encoding) and HPKE sealed to the
    asker's ephemeral key — the relay forwards those bytes without
    ever seeing a claim. A query carrying a reply key is answered
    SEALED OR NOT AT ALL (gate S1): sealing unavailable here, or a
    reply key that does not decode to an X25519 key, means no answer
    is packed — falling back to plaintext batches would put claim
    content on the wire in the clear for exactly the queries whose
    askers asked for sealing, a silent confidentiality downgrade.
    (The handler still sends ``done``; refusing to answer is a
    complete, honest response.) Without a reply key (a v1 asker)
    the chunks go out as plain packed ``batches`` messages, exactly
    what was negotiated.
    """
    query_id = str(query_msg.get("query_id"))
    if not batches:
        return []
    reply_key = query_msg.get("reply_key")
    if isinstance(reply_key, str) and reply_key:
        if not sealing.sealing_available():
            return []
        try:
            public_raw = b64url_decode(reply_key)
        except Exception:  # noqa: BLE001 - an undecodable key is a refusal
            return []
        if len(public_raw) != 32:
            return []
        encoding = str(query_msg.get("reply_encoding") or sealing.ENCODING_NONE)
        if encoding not in sealing.supported_encodings():
            encoding = sealing.ENCODING_NONE
        messages = []
        for chunk in _chunk_batches(batches, WIRE_CHUNK_BYTES):
            payload = sealing.encode_payload(chunk, encoding)
            sealed = sealing.seal(
                public_raw, info=query_id.encode("utf-8"), plaintext=payload
            )
            messages.append(
                json.dumps(
                    {
                        "type": "answer",
                        "query_id": query_id,
                        "sealed": b64url_encode(sealed),
                    }
                )
            )
        return messages
    return [
        json.dumps({"type": "answer", "query_id": query_id, "batches": chunk})
        for chunk in _chunk_batches(batches, WIRE_CHUNK_BYTES)
    ]


class RelayError(Exception):
    """Any relay-client failure (connect, hello, send, query)."""


class RelayNotConnected(RelayError):
    """A query was attempted while the client has no live connection."""


class _Refused(RelayError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def hello_signing_text(token: str, timestamp: int, challenge: bytes) -> bytes:
    """Mirror of relay/metasync_relay/server.py hello_signing_text.

    The challenge is the per-connection nonce the relay issued in its
    challenge message; binding it into the signature is what makes a
    captured hello worthless on any other connection."""
    return (
        f"{HELLO_SIGNATURE_CONTEXT}\n{token}\n{timestamp}\n"
        f"{b64url_encode(challenge)}"
    ).encode()


def hello_v2_signing_text(
    relay_identity: str,
    mode: str,
    version_min: int,
    version_max: int,
    token: str,
    timestamp: int,
    challenge: bytes,
) -> bytes:
    """Mirror of the relay's v2 hello signing text: the signature also
    binds WHICH relay this hello is for (the canonical dialed origin)
    and the declared mode, so a hello captured for one relay or mode
    cannot be replayed to another."""
    return (
        f"{HELLO_V2_SIGNATURE_CONTEXT}\n{relay_identity}\n{mode}\n"
        f"{version_min}\n{version_max}\n{token}\n{timestamp}\n"
        f"{b64url_encode(challenge)}"
    ).encode()


def reauth_signing_text(relay_identity: str, token: str, timestamp: int) -> bytes:
    """Mirror of the relay's re-auth signing text. The relay's
    identity is bound in (gate S5): a re-auth captured at one relay
    cannot be replayed at another to keep a hijacked session alive.
    Same-relay replay inside the timestamp window buys an attacker
    nothing beyond the token itself, which travels in the same
    message — the binding closes the CROSS-relay hole, which is
    the one that mattered."""
    return (
        f"{REAUTH_SIGNATURE_CONTEXT}\n{relay_identity}\n{token}\n{timestamp}"
    ).encode()


def canonical_relay_identity(url: str) -> str:
    """The relay identity for a dialed URL, mirroring the relay's
    canonicalization exactly: scheme://host:port, lowercased, the port
    always explicit (443 for wss, 80 for ws), no path."""
    parts = urlsplit(str(url or "").strip())
    scheme = (parts.scheme or "ws").lower()
    host = (parts.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parts.port or (443 if scheme == "wss" else 80)
    return f"{scheme}://{host}:{port}"


def token_expiry(token: str) -> float | None:
    """The exp claim of a compact access token (header.payload.sig,
    the SIN shape the relay's tokens.py verifies), or None when the
    token does not parse — re-auth scheduling simply stays off then,
    and the relay's expiry-plus-grace close + reconnect covers it."""
    try:
        segments = str(token).split(".")
        if len(segments) != 3:
            return None
        payload = json.loads(b64url_decode(segments[1]))
        exp = payload.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:  # noqa: BLE001 - any parse failure means "unknown"
        return None


def normalize_mode(raw: Any) -> str:
    """A mode from settings, validated; anything unknown is the default."""
    mode = str(raw or "").strip()
    return mode if mode in NODE_MODES else DEFAULT_MODE


def _envelope_size(envelope: dict[str, Any]) -> int:
    """One answer envelope's own serialized size in bytes — the unit
    the per-query buffering caps charge (MF-2). Canonical separators
    keep it deterministic; it measures the envelope, never the frame
    that happened to carry it."""
    try:
        return len(
            json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError):
        return 0


@dataclass
class _QueryState:
    query_id: str
    batches: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    started: float = field(default_factory=time.monotonic)
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    grace_seconds: float = DEFAULT_GRACE_SECONDS
    finished: threading.Event = field(default_factory=threading.Event)
    # Set when the stream died abnormally (the connection dropped
    # before the query was even sent, the answer budget ran out is
    # NOT an error — that just finishes). The consumer's next() raises
    # it instead of ending the stream quietly.
    error: str | None = None
    # Router-side accounting for the buffering caps: batches and bytes
    # CURRENTLY buffered for the consumer (routed minus drained), so
    # the caps bound consumer lag, not the query's lifetime volume.
    # queued_sizes is the per-batch size ledger, in queue order: each
    # entry is the serialized size of the batch at the same position
    # in `batches`, appended before the batch itself is queued.
    queued_batches: int = 0
    queued_bytes: int = 0
    queued_sizes: deque = field(default_factory=deque)
    # Protocol v2: the ephemeral X25519 private key matching the
    # reply_key this query carried (None when sealing was not
    # offered), and the relay's `complete` signal: once it lands the
    # stream ends as soon as what is queued has drained plus the
    # relay's grace for answers already in flight.
    reply_private: bytes | None = None
    completed_at: float | None = None
    complete_grace_seconds: float = DEFAULT_GRACE_SECONDS

    @property
    def deadline(self) -> float:
        # The post-complete hold can run for the RELAY's grace
        # (complete_grace_seconds, adopted from the welcome), which
        # the caller never sees: the outer deadline must cover the
        # longer of the two graces, or a relay grace above the
        # caller's own cuts the hold short and the S3 mismatch
        # returns (gate re-pass S-c).
        return self.started + self.window_seconds + max(
            self.grace_seconds, self.complete_grace_seconds)


class _QueryStream:
    """A blocking iterator over one query's answer batches.

    Calling code iterates it to completion; closing it early (break,
    close(), garbage collection) unregisters the query so late answers
    are dropped by the router instead of piling up in a dead queue."""

    def __init__(self, client: RelayClient, state: _QueryState) -> None:
        self._client = client
        self._state = state
        self._done = False

    def __iter__(self) -> _QueryStream:
        return self

    @staticmethod
    def _take(state: _QueryState, timeout: float | None = None) -> dict[str, Any]:
        """Pop one buffered batch and credit the buffering accounting
        (MF-2): bytes leave the books when the consumer drains them,
        so the caps measure what is buffered, not what ever passed
        through. Raises queue.Empty like the queue itself."""
        if timeout is None:
            batch = state.batches.get_nowait()
        else:
            batch = state.batches.get(timeout=timeout)
        if state.queued_sizes:
            size = state.queued_sizes.popleft()
            state.queued_batches = max(0, state.queued_batches - 1)
            state.queued_bytes = max(0, state.queued_bytes - size)
        return batch

    def __next__(self) -> dict[str, Any]:
        if self._done:
            raise StopIteration
        state = self._state
        while True:
            # Batches already routed are delivered even after the
            # state finished (teardown, a spent buffer budget): the
            # stream ends only once the queue is drained.
            try:
                return self._take(state)
            except queue.Empty:
                pass
            now = time.monotonic()
            if state.finished.is_set():
                self.close()
                if state.error is not None:
                    raise RelayNotConnected(state.error)
                raise StopIteration
            if (
                state.completed_at is not None
                and now - state.completed_at >= state.complete_grace_seconds
            ):
                # The relay said the window is closed and the short
                # in-flight grace has passed with an empty queue: the
                # stream ends here, long before the deadline.
                self.close()
                raise StopIteration
            remaining = state.deadline - now
            if remaining <= 0:
                self.close()
                raise StopIteration
            try:
                return self._take(state, timeout=min(0.05, remaining))
            except queue.Empty:
                continue

    def close(self) -> None:
        if not self._done:
            self._done = True
            self._client._unregister_query(self._state)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - destructor must never raise
            pass


class RelayClient:
    """One persistent WebSocket to the relay, reconnecting forever.

    Providers are zero-argument callables, re-read on every (re)connect
    so settings changes take effect without restarting the process:

    - ``url_provider``: the relay URL; empty means the feature is off
      and the client stays down (it never connects anywhere).
    - ``mode_provider``: the node mode (``full`` / ``own_library`` /
      ``leech``); anything else normalizes to ``own_library``.
    - ``token_provider``: a current pool access token carrying
      ``meta:read``. It may exchange (HTTP) when the cached token is
      stale; its errors are connect failures, handled by backoff.
    - ``fresh_token_provider``: like the above but ALWAYS a fresh
      exchange; used only before an in-band re-auth (§10). Optional.

    ``set_query_handler`` installs the answering hook: a function taking
    the forwarded query dict and returning an iterable of batch dicts
    (or None). The handler runs in a worker thread, never on the read
    loop, and its exceptions never kill the connection: a throwing
    handler still produces a ``done`` for the query.
    """

    def __init__(
        self,
        identity: NodeIdentity,
        *,
        url_provider: Callable[[], str],
        mode_provider: Callable[[], str],
        token_provider: Callable[[], str],
        issuer_key_provider: Callable[[], Any] | None = None,
        revocation_store: RevocationStore | None = None,
        revocations_applied: Callable[[Any], None] | None = None,
        fresh_token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.identity = identity
        self._url_provider = url_provider
        self._mode_provider = mode_provider
        self._token_provider = token_provider
        # Used ONLY by the re-auth path (§10: every in-band
        # re-authentication is preceded by a FRESH exchange). The
        # plain token provider may hand back a still-fresh cached
        # token, and re-sending that token is the spin gate M4
        # found: the relay acks the same exp, the loop recomputes a
        # past-due delay, and re-auths once per round trip for ~90s.
        # None falls back to the plain provider (a provider that
        # always fetches fresh is already correct).
        self._fresh_token_provider = fresh_token_provider
        # Resolves the token issuer's Ed25519 public key (for verifying
        # revocation lists). Optional: without it, lists the relay
        # carries cannot be verified here and are dropped — the relay
        # still enforces revocation at hello on its own copy.
        self._issuer_key_provider = issuer_key_provider
        self._query_handler: Callable[[dict[str, Any]], Iterable[dict[str, Any]] | None] | None = None

        self._lock = threading.RLock()
        self._queries: dict[str, _QueryState] = {}
        self._connected = False
        self._peers: int | None = None
        self._last_error: str | None = None
        self._session_params: tuple[str, str] | None = None
        # The node's revocation lists. The service passes ONE store
        # shared with the meta store and the pool clients, so a list
        # learned on any channel (welcome, push, exchange) is the
        # list admission enforces; a standalone client keeps its own.
        self._revocations = revocation_store or RevocationStore()
        # Called with each NEWLY applied list (never for a refused or
        # stale one): the service hangs the cache purge (§10: a node
        # drops the named keys' batches when a list lands) off it.
        self._revocations_applied = revocations_applied
        # The live session's token expiry (seconds, wall clock) and
        # the event the re-auth loop waits on for its ack. Both belong
        # to the loop thread's session; guarded by _lock where read
        # from other threads.
        self._token_exp: float | None = None
        self._reauth_event: asyncio.Event | None = None
        # Expiry of the token most recently presented in a `reauth`,
        # computed locally from that token (SF-5): adopted when the
        # relay's reauth_ok ack carries no exp of its own.
        self._reauth_pending_exp: float | None = None
        # The live session's canonical relay identity (the dialed
        # origin), bound into re-auth signatures (S5). Set at hello,
        # cleared at teardown, alongside _token_exp.
        self._session_relay_identity: str | None = None
        # The live relay's answer grace, from its welcome (S3): how
        # long after `complete` this session's queries stay open for
        # in-flight answers. None before the first v2 welcome.
        self._relay_grace_seconds: float | None = None

        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._astop: asyncio.Event | None = None
        self._ws: Any | None = None
        self._outbox: asyncio.Queue | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the connection thread. Idempotent while it is alive."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="metasync-relay-client"
        )
        self._thread.start()

    def stop(self) -> bool:
        """Stop the client: send bye, close the socket, join the thread.

        Returns True once the connection thread is dead. The stop flag
        is set before anything else and is re-checked before every
        connect attempt, and the token provider never runs on the loop
        thread, so in practice the thread exits promptly. If the join
        still times out, the thread handle is KEPT (returning False):
        a later stop() retries, and start() will not spawn a second
        client to duel the survivor at the relay (presence eviction
        ping-pong) while the first is still winding down."""
        self._stop_evt.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown_ws(), loop)
            except RuntimeError:
                pass
            try:
                loop.call_soon_threadsafe(
                    lambda: self._astop.set() if self._astop else None
                )
            except RuntimeError:
                pass
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return thread is None
        thread.join(timeout=10)
        if thread.is_alive():
            return False
        if self._thread is thread:
            self._thread = None
        return True

    def kick(self) -> None:
        """Drop the current connection so the loop reconnects at once
        with fresh provider values (URL or mode changed). A no-op while
        down: the next connect attempt already reads the providers."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._close_ws(), loop)
            except RuntimeError:
                pass

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- status ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def session_params(self) -> tuple[str, str] | None:
        """The (relay URL, mode) the live session was established with,
        or None before the first successful welcome. Callers (the
        service) compare it against current settings to detect a change
        worth a kick."""
        with self._lock:
            return self._session_params

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "peers": self._peers,
                "mode": self._current_mode(),
                "relay_url": self._current_url(),
                "last_error": self._last_error,
                "revocations": self._revocations.summary(),
            }

    def revocations(self, issuer: str | None = None) -> dict[str, Any] | None:
        """The current verified revocation list (highest sequence seen
        for the issuer), as a plain dict, or None when no verified
        list has arrived. The relay enforces the list at hello; this
        is the node's own copy for display and defense in depth."""
        current = self._revocations.current(issuer)
        if current is None:
            return None
        return {
            "issuer": current.issuer,
            "seq": current.seq,
            "issued_at": current.issued_at,
            "keys": sorted(current.keys),
        }

    def _issuer_key(self) -> Any | None:
        provider = self._issuer_key_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001 - a broken provider means unverifiable
            return None

    def _apply_revocations(self, payload: Any) -> None:
        """Verify and keep a revocation list carried by the relay
        (welcome or a `revocations` push). Unverifiable or stale lists
        are dropped here, exactly as the relay drops them. A list
        that actually lands is handed to the applied callback (the
        cache purge) — failure-isolated: a purge error must never
        kill the read loop that delivered the list."""
        if not isinstance(payload, dict):
            return
        key = self._issuer_key()
        if key is None:
            return
        applied = self._revocations.apply(payload, key)
        if applied is not None and self._revocations_applied is not None:
            try:
                self._revocations_applied(applied)
            except Exception:  # noqa: BLE001, S110 - see docstring
                pass

    def _current_url(self) -> str:
        try:
            return str(self._url_provider() or "").strip()
        except Exception:  # noqa: BLE001 - a broken provider reads as "off"
            return ""

    def _current_mode(self) -> str:
        try:
            return normalize_mode(self._mode_provider())
        except Exception:  # noqa: BLE001 - a broken provider reads as the default
            return DEFAULT_MODE

    # -- querying --------------------------------------------------------

    def query(
        self,
        kind: str,
        mode: str,
        subjects: list[str] | None = None,
        terms: list[str] | None = None,
        window_s: float = DEFAULT_WINDOW_SECONDS,
        grace_s: float = DEFAULT_GRACE_SECONDS,
    ) -> _QueryStream:
        """Ask the network. Returns a blocking iterator of batch dicts.

        The query message goes out only while connected; a disconnected
        client raises RelayNotConnected rather than queueing a question
        nobody will hear. The iterator ends at the window+grace
        deadline, when the connection dies, or early when the relay's
        ``complete`` lands (every routed node signalled done, or the
        relay's window ended) — the client still cannot close on
        individual dones, which are relay-internal in v2, but the
        relay knows the responder set and its complete is the honest
        early bound.

        Each query mints an ephemeral X25519 reply key: answerers seal
        their (compressed, signed) payloads to it, so claim plaintext
        exists only at the answering node and here — never at the
        relay. When sealing is unavailable the query goes out without
        a reply key and answers come back packed and plain, exactly
        as negotiated in the hello capabilities.
        """
        if kind not in ("artist", "album", "track"):
            raise ValueError(f"unknown query kind: {kind!r}")
        if mode == "exact":
            if not subjects:
                raise ValueError("exact queries need at least one subject")
            payload: dict[str, Any] = {"subjects": list(subjects)}
        elif mode == "text":
            if not terms:
                raise ValueError("text queries need at least one term")
            payload = {"terms": list(terms)}
        else:
            raise ValueError(f"unknown query mode: {mode!r}")
        if not self.connected:
            raise RelayNotConnected("relay client is not connected")
        query_id = secrets.token_bytes(16).hex()
        state = _QueryState(
            query_id=query_id,
            window_seconds=float(window_s),
            grace_seconds=float(grace_s),
        )
        with self._lock:
            if self._relay_grace_seconds is not None:
                # The post-complete grace is the relay's (S3), not
                # the consumer's deadline grace: it tracks how long
                # the relay keeps admitting answers after complete.
                state.complete_grace_seconds = self._relay_grace_seconds
        message: dict[str, Any] = {
            "type": "query",
            "query_id": query_id,
            "kind": kind,
            "mode": mode,
            **payload,
        }
        if sealing.sealing_available():
            reply_private, reply_public = sealing.generate_reply_keypair()
            state.reply_private = reply_private
            message["reply_key"] = b64url_encode(reply_public)
            encodings = sealing.supported_encodings()
            message["reply_encoding"] = (
                sealing.ENCODING_ZSTD_DICT
                if sealing.ENCODING_ZSTD_DICT in encodings
                else sealing.ENCODING_NONE
            )
        with self._lock:
            self._queries[query_id] = state
        try:
            self._enqueue_query(state, json.dumps(message))
        except RelayNotConnected:
            self._unregister_query(state)
            raise
        return _QueryStream(self, state)

    # -- answering -------------------------------------------------------

    def set_query_handler(
        self, fn: Callable[[dict[str, Any]], Iterable[dict[str, Any]] | None] | None
    ) -> None:
        self._query_handler = fn

    # -- internals: thread and loop --------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        self._astop = asyncio.Event()
        try:
            loop.run_until_complete(self._run_loop())
        except Exception as err:  # noqa: BLE001 - the thread must not die silently
            # _run_loop handles every expected failure itself; anything
            # reaching here is a bug, and status() is the only window
            # into this thread — record the cause before the finally
            # below tears the loop down.
            self._set_error(f"relay thread died: {str(err)[:200]}")
        finally:
            with self._lock:
                self._connected = False
                self._session_params = None
                self._finish_all_queries()
            try:
                loop.close()
            except Exception:  # noqa: BLE001, S110 - shutdown is best-effort
                pass
            self._loop = None
            self._ws = None
            self._outbox = None

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep, returning True when the client was stopped mid-sleep."""
        if self._astop is None:
            await asyncio.sleep(seconds)
            return self._stop_evt.is_set()
        try:
            await asyncio.wait_for(self._astop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return self._stop_evt.is_set()

    async def _fetch_token(self, *, fresh: bool = False) -> str | None:
        """Run the token provider OFF the loop thread (it may exchange
        over HTTP and block), racing the stop signal so a wedged
        provider can neither stall the loop nor delay a stop. Returns
        the token, or None when the client was stopped mid-fetch —
        the stranded provider call finishes whenever it finishes and
        its result is discarded. ``fresh`` selects the fresh-exchange
        provider (the re-auth path's, §10); see __init__."""
        provider = self._token_provider
        if fresh and self._fresh_token_provider is not None:
            provider = self._fresh_token_provider
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(loop.run_in_executor(None, provider))
        if self._astop is None:
            return await task
        stop_wait = asyncio.ensure_future(self._astop.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            task.add_done_callback(_swallow_future_exception)
            return None
        finally:
            stop_wait.cancel()

    async def _run_loop(self) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        while not self._stop_evt.is_set():
            url = self._current_url()
            if not url:
                # Feature off: idle here, never connect anywhere. The
                # providers are re-read on every loop pass, so flipping
                # the URL on in settings wakes the client up.
                if await self._sleep_or_stop(1.0):
                    break
                continue
            mode = self._current_mode()
            try:
                token = await self._fetch_token()
            except Exception as err:  # noqa: BLE001 - any token failure is a connect failure
                self._set_error(f"token: {str(err)[:200]}")
                if await self._sleep_or_stop(self._backoff(backoff)):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                continue
            if token is None or self._stop_evt.is_set():
                # Stopped while fetching (or in the sliver of time
                # since): never open a connection after a stop, or a
                # zombie client duels its own replacement at the relay.
                break
            try:
                session_seconds = await self._connect_once(url, mode, token)
                # The backoff resets only after a session that lived
                # long enough to count as healthy. A shorter clean
                # close (an accept-and-close relay, a flapping path)
                # grows the backoff like any other failure, so the
                # retry rate decays instead of pinning at ~1 second.
                healthy = session_seconds >= _MIN_SESSION_SECONDS
                if healthy:
                    backoff = _BACKOFF_INITIAL_SECONDS
                # A clean close by the relay is still a drop: brief
                # pause so an accept-and-close relay cannot spin us.
                if await self._sleep_or_stop(self._backoff(backoff)):
                    break
                if not healthy:
                    backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            except _Refused as err:
                # Refused is terminal for this attempt and must not
                # spin: a bad key or unbound node retries on the slow
                # floor until an operator fixes it.
                self._set_error(f"refused: {err.reason}")
                if await self._sleep_or_stop(self._backoff(max(backoff, _BACKOFF_REFUSED_FLOOR_SECONDS))):
                    break
                backoff = min(max(backoff, _BACKOFF_REFUSED_FLOOR_SECONDS) * 2, _BACKOFF_MAX_SECONDS)
            except Exception as err:  # noqa: BLE001 - transport drops are reconnectable
                self._set_error(f"connect: {str(err)[:200]}")
                if await self._sleep_or_stop(self._backoff(backoff)):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    @staticmethod
    def _backoff(base: float) -> float:
        """Exponential backoff with jitter, so a fleet of restarted
        nodes does not reconnect in lockstep."""
        return base * random.uniform(0.5, 1.5)

    # -- internals: one connection ---------------------------------------

    async def _connect_once(self, url: str, mode: str, token: str) -> float:
        """One session; returns how many seconds it lived (the caller
        prices the reconnect backoff off it)."""
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError as err:
            raise RelayError(
                "the websockets package is not installed; the metadata "
                "network is unavailable, the rest of the node is unaffected"
            ) from err
        started = time.monotonic()
        async with ws_connect(
            url,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            open_timeout=15.0,
        ) as ws:
            self._ws = ws
            outbox: asyncio.Queue = asyncio.Queue()
            self._outbox = outbox
            sender = asyncio.ensure_future(self._sender(ws, outbox))
            reauth: asyncio.Task | None = None
            try:
                await self._hello(ws, url, mode, token)
                # Welcome landed: this session counts as connected.
                self._set_error(None)
                with self._lock:
                    self._token_exp = token_expiry(token)
                    self._reauth_event = asyncio.Event()
                reauth = asyncio.ensure_future(self._reauth_loop(outbox))
                async for raw in ws:
                    if self._dispatch(raw, outbox):
                        break  # the relay refused us mid-session: reconnect fresh
                # Normal exit of the receive loop = socket closed.
            finally:
                sender.cancel()
                if reauth is not None:
                    reauth.cancel()
                with self._lock:
                    self._connected = False
                    self._session_params = None
                    self._token_exp = None
                    self._reauth_event = None
                    self._reauth_pending_exp = None
                    self._session_relay_identity = None
                    self._relay_grace_seconds = None
                    self._finish_all_queries()
                self._ws = None
                self._outbox = None
        return time.monotonic() - started

    async def _reauth_loop(self, outbox: asyncio.Queue) -> None:
        """Keep the session's token fresh: shortly before expiry,
        re-exchange (off-loop, via _fetch_token) and send a `reauth`
        on THIS connection, so in-flight queries and the session
        itself survive the first token's expiry. A failed or refused
        re-auth ends the loop; the relay's expiry-plus-grace close
        then ends the session and the normal reconnect takes over."""
        try:
            while self._outbox is outbox and not self._stop_evt.is_set():
                with self._lock:
                    exp = self._token_exp
                    event = self._reauth_event
                if exp is None or event is None:
                    return
                delay = exp - REAUTH_LEAD_SECONDS - time.time()
                if delay > 0:
                    event.clear()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=delay)
                        continue  # an ack moved the expiry; recompute
                    except TimeoutError:
                        pass
                event.clear()
                try:
                    # §10: a fresh exchange precedes every in-band
                    # re-authentication — both because the design
                    # says so (it is the revocation list's delivery
                    # moment) and because the cached token is the
                    # M4 spin: re-sending it earns the same exp back
                    # and the loop would re-auth per round trip.
                    token = await self._fetch_token(fresh=True)
                except Exception:  # noqa: BLE001 - a failed exchange retries via reconnect
                    return
                if token is None or self._outbox is not outbox:
                    return
                timestamp = int(time.time())
                with self._lock:
                    relay_identity = self._session_relay_identity
                if relay_identity is None:
                    return  # session is tearing down; no re-auth to send
                message = {
                    "type": "reauth",
                    "token": token,
                    "timestamp": timestamp,
                    "signature": self.identity.sign(
                        reauth_signing_text(relay_identity, token, timestamp)
                    ),
                }
                with self._lock:
                    # Remember what THIS token expires at (SF-5): if
                    # the ack comes back without an exp, the handler
                    # adopts this instead of leaving the stale expiry
                    # in place to spin the loop on.
                    self._reauth_pending_exp = token_expiry(token)
                outbox.put_nowait(json.dumps(message))
                try:
                    await asyncio.wait_for(event.wait(), timeout=15.0)
                except TimeoutError:
                    return  # no ack: the relay will close at expiry+grace
        except asyncio.CancelledError:
            pass

    async def _sender(self, ws: Any, outbox: asyncio.Queue) -> None:
        try:
            while True:
                text = await outbox.get()
                await ws.send(text)
        except Exception:  # noqa: BLE001, S110 - a dead socket ends the sender silently
            pass

    async def _hello(self, ws: Any, url: str, mode: str, token: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        challenge_msg = json.loads(raw)
        if not isinstance(challenge_msg, dict) or challenge_msg.get("type") != "challenge":
            raise RelayError("relay did not open with a challenge")
        try:
            nonce = b64url_decode(str(challenge_msg.get("nonce") or ""))
        except Exception as err:
            raise RelayError("relay challenge nonce is not base64url") from err
        if len(nonce) != 32:
            raise RelayError("relay challenge nonce is not 32 bytes")
        timestamp = int(time.time())
        identity = canonical_relay_identity(url)
        signature = self.identity.sign(
            hello_v2_signing_text(
                identity,
                mode,
                RELAY_PROTOCOL_VERSION,
                RELAY_PROTOCOL_VERSION_MAX,
                token,
                timestamp,
                nonce,
            )
        )
        hello = {
            "type": "hello",
            "version_min": RELAY_PROTOCOL_VERSION,
            "version_max": RELAY_PROTOCOL_VERSION_MAX,
            "node_key": self.identity.public_key_b64url,
            "mode": mode,
            "token": token,
            "timestamp": timestamp,
            "challenge": b64url_encode(nonce),
            "signature": signature,
            "capabilities": {
                # What this node can accept back: HPKE sealing suites
                # and payload encodings. Empty lists negotiate the
                # plain v1-style answer path on a v2 session.
                "sealing": ["hpke-x25519-chacha"] if sealing.sealing_available() else [],
                "encodings": sealing.supported_encodings(),
                "dict_id": sealing.dictionary_id(),
            },
        }
        await ws.send(json.dumps(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            raise RelayError("relay hello reply is not an object")
        if reply.get("type") == "refused":
            raise _Refused(str(reply.get("reason") or "auth"))
        if reply.get("type") != "welcome":
            raise RelayError(f"relay hello reply was {reply.get('type')!r}, not welcome")
        if reply.get("version") != RELAY_PROTOCOL_VERSION_MAX:
            # This client speaks v2 (it needs complete, re-auth and
            # revocation pushes to behave as designed). A relay that
            # negotiates down to v1 is too old for it: refuse and let
            # the slow refused-backoff surface the mismatch instead of
            # silently degrading. The relay still serves genuine v1
            # clients; upgrading the relay first is the migration.
            raise _Refused("version")
        peers = reply.get("peers")
        grace = reply.get("grace_seconds")
        with self._lock:
            self._connected = True
            self._peers = peers if isinstance(peers, int) else None
            self._session_params = (url, mode)
            self._session_relay_identity = identity
            if isinstance(grace, (int, float)) \
                    and not isinstance(grace, bool) \
                    and math.isfinite(grace) and grace >= 0:
                # bool is an int subclass: a JSON `true` here would
                # otherwise adopt a 1-second grace (gate re-pass S-c).
                # A non-finite grace (json.loads accepts Infinity and
                # NaN) is ignored, not adopted: it would make the
                # stream deadline infinite and stall the query
                # forever. A finite grace is capped for the same
                # reason — the relay sets how long the asker waits,
                # so the wait stays bounded however large it claims.
                self._relay_grace_seconds = min(
                    float(grace), MAX_RELAY_GRACE_SECONDS)
        self._apply_revocations(reply.get("revocations"))

    async def _shutdown_ws(self) -> None:
        """Best-effort bye, then close. Runs on the loop thread."""
        ws = self._ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.send(json.dumps({"type": "bye"})), timeout=1.0)
        except Exception:  # noqa: BLE001, S110 - bye is courtesy, not contract
            pass
        await self._close_ws()

    async def _close_ws(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001, S110 - closing is best-effort
            pass

    # -- internals: inbound routing --------------------------------------

    def _dispatch(self, raw: Any, outbox: asyncio.Queue) -> bool:
        """Route one relay message. Never raises: a malformed frame must
        not kill the read loop. Returns True only when the session is
        over (a mid-session refusal), telling the read loop to tear
        the connection down so the reconnect path re-exchanges."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return False
        if not isinstance(msg, dict):
            return False
        mtype = msg.get("type")
        if mtype == "query":
            query_id = msg.get("query_id")
            if not isinstance(query_id, str):
                return False
            # The handler runs off the read loop (executor) so a slow
            # handler cannot stall heartbeats or other queries. The
            # generation check in _answer_query drops answers whose
            # connection died while the handler was working: the relay
            # already evicted that session, and answering on a fresh
            # connection would be charged as unrouted.
            asyncio.ensure_future(self._answer_query(msg, outbox))
        elif mtype == "answer":
            self._route_answer_message(msg, raw)
        elif mtype == "complete":
            query_id = msg.get("query_id")
            if isinstance(query_id, str):
                with self._lock:
                    state = self._queries.get(query_id)
                if state is not None and state.completed_at is None:
                    # The relay's window for this query is closed. The
                    # stream drains what it holds, lets a short grace
                    # pass for answers already in flight, and ends —
                    # see _QueryStream. Dones never do this: they are
                    # relay-internal in v2 (and ignored below if a v1
                    # peer's done is ever forwarded anyway).
                    state.completed_at = time.monotonic()
        elif mtype == "revocations":
            self._apply_revocations(msg)
        elif mtype == "reauth_ok":
            exp = msg.get("exp")
            with self._lock:
                if isinstance(exp, (int, float)):
                    self._token_exp = float(exp)
                elif self._reauth_pending_exp is not None:
                    # The ack carried no exp (an older or partial
                    # relay): adopt the expiry of the token this
                    # session just presented (SF-5). Trusting only the
                    # ack would leave the OLD expiry in place and spin
                    # the re-auth loop re-exchanging against it.
                    self._token_exp = self._reauth_pending_exp
                self._reauth_pending_exp = None
                event = self._reauth_event
            if event is not None:
                event.set()
        elif mtype == "query_refused":
            query_id = msg.get("query_id")
            reason = str(msg.get("reason") or "refused")
            if isinstance(query_id, str):
                with self._lock:
                    state = self._queries.get(query_id)
                if state is not None:
                    # The relay will never route this query (budget,
                    # duplicate): fail the stream NOW instead of
                    # letting the consumer wait out the full deadline
                    # for answers that cannot come.
                    self._fail_query(state, f"relay refused the query: {reason}")
        elif mtype == "refused":
            # A refusal after hello (a failed re-auth) ends the
            # session: tear down and let the reconnect re-exchange.
            self._set_error(f"refused: {msg.get('reason') or 'auth'!s}")
            return True
        elif mtype == "done":
            # Accepted and ignored. In v2 dones are answerer->relay
            # signals only; a v1 relay may still forward them, and
            # they change nothing here either way — `complete` is the
            # asker's early-close signal now.
            return False
        # welcome/challenge after hello, and unknown types, are
        # ignored: the hello handshake already consumed its own frames.
        return False

    def _route_answer_message(self, msg: dict[str, Any], raw: Any) -> None:
        """Unpack one answer message (single batch, packed batches, or
        a sealed payload) and route each batch envelope into its
        query's stream, under the query's buffering caps."""
        query_id = msg.get("query_id")
        if not isinstance(query_id, str):
            return
        with self._lock:
            state = self._queries.get(query_id)
        if state is None:
            # Answers for unknown or closed queries are dropped
            # silently: the relay charges the sender, the asker
            # owes it nothing, and the stream is done with it.
            return
        if state.reply_private is not None \
                and not isinstance(msg.get("sealed"), str):
            # This query carried a reply key: its answers are sealed
            # or they are nothing (gate S1). Plaintext batches for a
            # sealed query — a downgrading relay or answerer — are
            # dropped here, never routed into the stream.
            return
        envelopes: list[dict[str, Any]] = []
        if isinstance(msg.get("sealed"), str):
            envelopes = self._unseal_answer(msg, state)
        elif isinstance(msg.get("batches"), list):
            envelopes = [b for b in msg["batches"] if isinstance(b, dict)]
        elif isinstance(msg.get("batch"), dict):
            # One envelope: a direct batch ({batch, sig}) or a step-4
            # forwarding envelope ({forward, envelope}), as-is.
            envelopes = [msg["batch"]]
        stamp = msg.get("stamp")
        for envelope in envelopes:
            if not self._stamp_matches(stamp, envelope):
                continue
            self._route_batch(state, envelope)

    @staticmethod
    def _is_forwarding_envelope(envelope: dict[str, Any]) -> bool:
        """The real step-4 forwarding shape (responder.forwarding_
        envelope): a `forward` dict carrying the signed fields —
        origin_batch_id, origin_node, forwarded_at, sig — wrapping an
        `envelope` dict. A bare "forward" key is NOT this shape and
        must not opt an envelope out of the stamp check (SF-1)."""
        forward = envelope.get("forward")
        if not isinstance(forward, dict):
            return False
        if not isinstance(envelope.get("envelope"), dict):
            return False
        return (
            isinstance(forward.get("origin_batch_id"), str)
            and isinstance(forward.get("origin_node"), str)
            and isinstance(forward.get("forwarded_at"), str)
            and isinstance(forward.get("sig"), str)
        )

    def _stamp_matches(self, stamp: Any, envelope: dict[str, Any]) -> bool:
        """The sender-stamp pre-filter (design section 11): for a
        DIRECT answer, the relay's stamp names the authenticated
        answerer; the batch header names the signer. A mismatch means
        the envelope is not what the relay routed — drop it here,
        before any signature verification anywhere downstream.
        Forwarding envelopes keep their step-4 semantics (the stamp
        names the forwarder, the header the origin) and are never
        checked; with no stamp (a v1 relay) there is nothing to check."""
        if not isinstance(stamp, dict):
            return True
        if self._is_forwarding_envelope(envelope):
            return True
        batch = envelope.get("batch")
        if not isinstance(batch, dict):
            return True
        header = batch.get("header")
        if not isinstance(header, dict):
            return True
        node = header.get("node")
        claimed = stamp.get("from")
        if not isinstance(node, str) or not isinstance(claimed, str):
            return True
        try:
            signer_id = hashlib.sha256(b64url_decode(node)).hexdigest()
        except Exception:  # noqa: BLE001 - an undecodable key cannot match
            return False
        return signer_id == claimed

    def _unseal_answer(self, msg: dict[str, Any], state: _QueryState) -> list[dict[str, Any]]:
        """Open one sealed answer payload. Any failure — no key, bad
        ciphertext, an oversize or misshapen payload — drops the
        message: a hostile or broken answerer must not be able to
        kill the query, let alone the connection."""
        if state.reply_private is None:
            return []
        try:
            sealed = b64url_decode(str(msg["sealed"]))
            payload = sealing.open_sealed(
                state.reply_private,
                info=state.query_id.encode("utf-8"),
                sealed=sealed,
            )
            batches = sealing.decode_payload(payload)
        except Exception:  # noqa: BLE001 - drop, never raise (see docstring)
            return []
        return [b for b in batches if isinstance(b, dict)]

    def _route_batch(self, state: _QueryState, batch: dict[str, Any]) -> None:
        # Each envelope is charged its OWN serialized size (MF-2):
        # charging the whole frame per envelope made packed answers
        # (many envelopes, one frame) blow the byte cap on phantom
        # bytes and truncate themselves. For sealed payloads this is
        # the decoded envelope's size, which is what will sit in the
        # consumer's queue.
        size = _envelope_size(batch)
        if (
            state.queued_batches >= MAX_QUEUED_BATCHES_PER_QUERY
            or state.queued_bytes + size > MAX_QUEUED_BYTES_PER_QUERY
        ):
            # The query's buffering budget is spent: finish it
            # (a normal end of stream for the consumer) and drop
            # this and every later batch instead of queueing
            # without bound behind a slow consumer.
            self._complete_query(state)
            return
        state.queued_batches += 1
        state.queued_bytes += size
        # The size ledger is appended BEFORE the batch is queued, so
        # a consumer pop always finds the batch's size at the head.
        state.queued_sizes.append(size)
        state.batches.put(batch)

    async def _answer_query(self, msg: dict[str, Any], outbox: asyncio.Queue) -> None:
        # Sends go only to the outbox of the connection the query
        # arrived on (identity check, not just generation): if that
        # connection died while the handler worked, the relay already
        # evicted the session, and answering or signalling done on a
        # fresh connection would be charged as unrouted.
        query_id = str(msg.get("query_id"))
        loop = asyncio.get_running_loop()
        handler = self._query_handler
        batches: Iterable[dict[str, Any]] | None = None
        try:
            if handler is not None:
                # The handler runs in the executor AND its iterable is
                # consumed there (_materialize_handler): iterating a
                # lazy generator here would put the handler's real
                # work on the read loop, stalling heartbeats and every
                # other query's routing behind it.
                batches = await loop.run_in_executor(
                    None, _materialize_handler, handler, msg
                )
            if batches is not None:
                dicts = [b for b in batches if isinstance(b, dict)]
                for text in _pack_answer_messages(msg, dicts):
                    if self._outbox is not outbox:
                        return  # connection died mid-handler: drop, do not re-answer
                    outbox.put_nowait(text)
        except Exception:  # noqa: BLE001, S110 - a throwing handler must not kill the connection
            pass
        finally:
            if self._outbox is outbox:
                outbox.put_nowait(json.dumps({"type": "done", "query_id": query_id}))

    # -- internals: bookkeeping ------------------------------------------

    def _enqueue_query(self, state: _QueryState, text: str) -> None:
        loop = self._loop
        outbox = self._outbox
        if loop is None or outbox is None or not self.connected:
            raise RelayNotConnected("relay client is not connected")
        if threading.get_ident() == self._loop_thread_id:
            self._deliver_query(outbox, state, text)
        else:
            try:
                loop.call_soon_threadsafe(self._deliver_query, outbox, state, text)
            except RuntimeError as err:
                raise RelayNotConnected("relay loop is closed") from err

    def _deliver_query(self, outbox: asyncio.Queue, state: _QueryState, text: str) -> None:
        """Put a query on the wire — from the loop thread, against the
        outbox captured when the query was created. If that outbox is
        no longer the live connection's (the connection tore down and
        possibly a new one replaced it in between), the query was
        never sent and never will be: fail it at once instead of
        letting the consumer drain an empty stream to the deadline."""
        if self._outbox is not outbox or not self._connected:
            self._fail_query(state, "relay connection dropped before the query was sent")
            return
        outbox.put_nowait(text)

    def _unregister_query(self, state: _QueryState) -> None:
        with self._lock:
            if self._queries.get(state.query_id) is state:
                del self._queries[state.query_id]

    def _complete_query(self, state: _QueryState) -> None:
        """End a query's stream normally and stop routing to it."""
        state.finished.set()
        self._unregister_query(state)

    def _fail_query(self, state: _QueryState, error: str) -> None:
        """End a query's stream with an error the consumer will raise."""
        if state.error is None:
            state.error = error
        state.finished.set()
        self._unregister_query(state)

    def _finish_all_queries(self) -> None:
        """Connection teardown: finish every open query AND forget the
        states. Finished states must not linger in the registry across
        a reconnect — a consumer that never iterates would otherwise
        leak one state per query for the life of the process."""
        with self._lock:
            states = list(self._queries.values())
            self._queries.clear()
        for state in states:
            state.finished.set()

    def _set_error(self, err: str | None) -> None:
        with self._lock:
            self._last_error = err

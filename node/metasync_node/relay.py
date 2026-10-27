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

The ``websockets`` package is an optional dependency of this feature
only, so it is imported lazily inside the connect path: a node built
without it must still boot and run everything else, with the metadata
feature alone reporting the missing package as its last error.
"""
from __future__ import annotations

import asyncio
import json
import queue
import random
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .identity import NodeIdentity, b64url_decode, b64url_encode

RELAY_PROTOCOL_VERSION = 1
HELLO_SIGNATURE_CONTEXT = "metasync-relay-hello"
HELLO_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 1_048_576
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0

NODE_MODES = ("full", "own_library", "leech")
DEFAULT_MODE = "own_library"
DEFAULT_WINDOW_SECONDS = 6.0
DEFAULT_GRACE_SECONDS = 10.0

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


def normalize_mode(raw: Any) -> str:
    """A mode from settings, validated; anything unknown is the default."""
    mode = str(raw or "").strip()
    return mode if mode in NODE_MODES else DEFAULT_MODE


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
    # Router-side accounting for the buffering caps: batches routed
    # into this state so far, and their approximate wire bytes.
    queued_batches: int = 0
    queued_bytes: int = 0

    @property
    def deadline(self) -> float:
        return self.started + self.window_seconds + self.grace_seconds


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

    def __next__(self) -> dict[str, Any]:
        if self._done:
            raise StopIteration
        state = self._state
        while True:
            # Batches already routed are delivered even after the
            # state finished (teardown, a spent buffer budget): the
            # stream ends only once the queue is drained.
            try:
                return state.batches.get_nowait()
            except queue.Empty:
                pass
            now = time.monotonic()
            if state.finished.is_set():
                self.close()
                if state.error is not None:
                    raise RelayNotConnected(state.error)
                raise StopIteration
            remaining = state.deadline - now
            if remaining <= 0:
                self.close()
                raise StopIteration
            try:
                return state.batches.get(timeout=min(0.05, remaining))
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
    ) -> None:
        self.identity = identity
        self._url_provider = url_provider
        self._mode_provider = mode_provider
        self._token_provider = token_provider
        self._query_handler: Callable[[dict[str, Any]], Iterable[dict[str, Any]] | None] | None = None

        self._lock = threading.RLock()
        self._queries: dict[str, _QueryState] = {}
        self._connected = False
        self._peers: int | None = None
        self._last_error: str | None = None
        self._session_params: tuple[str, str] | None = None

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
            }

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
        nobody will hear. The iterator ends when the window+grace
        deadline passes or when the connection dies. There is no
        done-based early close: the client cannot know the responder
        set (the peer count is a stale hello-time hint that also counts
        leech-mode nodes, and answer frames carry no responder identity
        the client may use — batches are opaque), so the deadline is
        the only honest bound on the stream.
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
            self._queries[query_id] = state
        message = {
            "type": "query",
            "query_id": query_id,
            "kind": kind,
            "mode": mode,
            **payload,
        }
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

    async def _fetch_token(self) -> str | None:
        """Run the token provider OFF the loop thread (it may exchange
        over HTTP and block), racing the stop signal so a wedged
        provider can neither stall the loop nor delay a stop. Returns
        the token, or None when the client was stopped mid-fetch —
        the stranded provider call finishes whenever it finishes and
        its result is discarded."""
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(loop.run_in_executor(None, self._token_provider))
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
            try:
                await self._hello(ws, url, mode, token)
                # Welcome landed: this session counts as connected.
                self._set_error(None)
                async for raw in ws:
                    self._dispatch(raw, outbox)
                # Normal exit of the receive loop = socket closed.
            finally:
                sender.cancel()
                with self._lock:
                    self._connected = False
                    self._session_params = None
                    self._finish_all_queries()
                self._ws = None
                self._outbox = None
        return time.monotonic() - started

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
        signature = self.identity.sign(hello_signing_text(token, timestamp, nonce))
        hello = {
            "type": "hello",
            "version": RELAY_PROTOCOL_VERSION,
            "node_key": self.identity.public_key_b64url,
            "mode": mode,
            "token": token,
            "timestamp": timestamp,
            "challenge": b64url_encode(nonce),
            "signature": signature,
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
        peers = reply.get("peers")
        with self._lock:
            self._connected = True
            self._peers = peers if isinstance(peers, int) else None
            self._session_params = (url, mode)

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

    def _dispatch(self, raw: Any, outbox: asyncio.Queue) -> None:
        """Route one relay message. Never raises: a malformed frame must
        not kill the read loop."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "query":
            query_id = msg.get("query_id")
            if not isinstance(query_id, str):
                return
            # The handler runs off the read loop (executor) so a slow
            # handler cannot stall heartbeats or other queries. The
            # generation check in _answer_query drops answers whose
            # connection died while the handler was working: the relay
            # already evicted that session, and answering on a fresh
            # connection would be charged as unrouted.
            asyncio.ensure_future(self._answer_query(msg, outbox))
        elif mtype == "answer":
            query_id = msg.get("query_id")
            batch = msg.get("batch")
            if not isinstance(query_id, str) or not isinstance(batch, dict):
                return
            with self._lock:
                state = self._queries.get(query_id)
            if state is None:
                # Answers for unknown or closed queries are dropped
                # silently: the relay charges the sender, the asker
                # owes it nothing, and the stream is done with it.
                return
            frame_bytes = len(raw) if isinstance(raw, (str, bytes)) else 0
            if (
                state.queued_batches >= MAX_QUEUED_BATCHES_PER_QUERY
                or state.queued_bytes + frame_bytes > MAX_QUEUED_BYTES_PER_QUERY
            ):
                # The query's buffering budget is spent: finish it
                # (a normal end of stream for the consumer) and drop
                # this and every later batch instead of queueing
                # without bound behind a slow consumer.
                self._complete_query(state)
                return
            state.queued_batches += 1
            state.queued_bytes += frame_bytes
            state.batches.put(batch)
        elif mtype == "done":
            # Accepted and ignored. A done tells the asker a responder
            # has no more answers, but the client cannot use it to end
            # the stream early: it does not know the responder set
            # (see query()), so dones change nothing here.
            return
        # welcome/refused/challenge after hello, and unknown types, are
        # ignored: the hello handshake already consumed its own frames.

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
                for batch in batches:
                    if not isinstance(batch, dict):
                        continue
                    if self._outbox is not outbox:
                        return  # connection died mid-handler: drop, do not re-answer
                    outbox.put_nowait(
                        json.dumps({"type": "answer", "query_id": query_id, "batch": batch})
                    )
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

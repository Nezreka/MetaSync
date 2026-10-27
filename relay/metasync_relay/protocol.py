"""Wire messages for the relay protocol (DESIGN-METADATA.md section 10).

JSON over the persistent connection. The relay validates shapes strictly
and treats the answer batch as an opaque JSON object: it never parses
claims (section 6's signed batch is the nodes' business, not the
switchboard's).

Client to relay: hello, query, answer, done, bye.
Relay to client: challenge, welcome, refused (built by server.py
helpers here). The challenge precedes hello on every connection: the
relay issues a fresh random nonce and the hello must echo it and bind
it into its signature, so a captured hello cannot be replayed onto a
new connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import PROTOCOL_VERSION
from .tokens import decode_base64url

MAX_MESSAGE_BYTES = 1_048_576  # 1MB per single message, either direction
DEFAULT_ANSWER_CAP_BYTES = 8_388_608  # 8MB of answers admitted per query

QUERY_KINDS = frozenset({"artist", "album", "track"})
QUERY_MODES = frozenset({"exact", "text"})
NODE_MODES = frozenset({"full", "own_library", "leech"})

_MAX_SUBJECTS = 100
_MAX_SUBJECT_CHARS = 512
_MAX_TERMS = 16
_MAX_TERM_CHARS = 256


class ProtocolError(Exception):
    """A message failed validation. `reason` is an error class only;
    it never carries message content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Hello:
    node_key: bytes  # raw 32-byte Ed25519 public key
    mode: str
    token: str
    timestamp: int
    challenge: bytes  # raw 32-byte nonce from this connection's challenge
    signature: bytes  # raw 64-byte Ed25519 signature


@dataclass(frozen=True)
class Query:
    query_id: str  # normalized: 32 lowercase hex chars (128 bits)
    kind: str
    mode: str  # "exact" | "text"
    subjects: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    query_id: str
    batch: dict  # opaque section 6 batch; never inspected by the relay


@dataclass(frozen=True)
class Done:
    query_id: str


@dataclass(frozen=True)
class Bye:
    pass


Message = Hello | Query | Answer | Done | Bye


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError("duplicate_key")
        out[key] = value
    return out


def _require_keys(obj: dict, allowed: frozenset[str], required: frozenset[str]) -> None:
    keys = frozenset(obj.keys())
    if not required <= keys:
        raise ProtocolError("missing_field")
    if not keys <= allowed:
        raise ProtocolError("unknown_field")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_query_id(value: object) -> str:
    """A query_id is 128 bits of randomness minted by the asker (section
    10), written as 32 hex chars or base64url of the 16 bytes. Normalized
    to lowercase hex for routing."""
    if isinstance(value, str):
        lowered = value.lower()
        if len(lowered) == 32 and all(c in "0123456789abcdef" for c in lowered):
            return lowered
        raw = decode_base64url(value, 16)
        if raw is not None:
            return raw.hex()
    raise ProtocolError("bad_query_id")


_HELLO_KEYS = frozenset(
    {"type", "version", "node_key", "mode", "token", "timestamp", "challenge", "signature"}
)


def _parse_hello(obj: dict) -> Hello:
    _require_keys(obj, _HELLO_KEYS, _HELLO_KEYS)
    if obj["version"] != PROTOCOL_VERSION or not _is_int(obj["version"]):
        raise ProtocolError("bad_version")
    node_key = decode_base64url(obj["node_key"], 32)
    if node_key is None:
        raise ProtocolError("bad_node_key")
    if not isinstance(obj["mode"], str) or obj["mode"] not in NODE_MODES:
        raise ProtocolError("bad_mode")
    if not isinstance(obj["token"], str) or not obj["token"]:
        raise ProtocolError("bad_token")
    if not _is_int(obj["timestamp"]):
        raise ProtocolError("bad_timestamp")
    challenge = decode_base64url(obj["challenge"], 32)
    if challenge is None:
        raise ProtocolError("bad_challenge")
    signature = decode_base64url(obj["signature"], 64)
    if signature is None:
        raise ProtocolError("bad_signature")
    return Hello(
        node_key=node_key,
        mode=obj["mode"],  # type: ignore[arg-type]
        token=obj["token"],
        timestamp=obj["timestamp"],  # type: ignore[arg-type]
        challenge=challenge,
        signature=signature,
    )


def _parse_query(obj: dict) -> Query:
    _require_keys(
        obj,
        frozenset({"type", "query_id", "kind", "mode", "subjects", "terms"}),
        frozenset({"type", "query_id", "kind", "mode"}),
    )
    if not isinstance(obj["kind"], str) or obj["kind"] not in QUERY_KINDS:
        raise ProtocolError("bad_kind")
    if not isinstance(obj["mode"], str) or obj["mode"] not in QUERY_MODES:
        raise ProtocolError("bad_query_mode")
    query_id = parse_query_id(obj["query_id"])
    subjects: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    if obj["mode"] == "exact":
        raw = obj.get("subjects")
        if (
            not isinstance(raw, list)
            or not raw
            or len(raw) > _MAX_SUBJECTS
            or not all(isinstance(s, str) and 0 < len(s) <= _MAX_SUBJECT_CHARS for s in raw)
        ):
            raise ProtocolError("bad_subjects")
        if "terms" in obj:
            raise ProtocolError("unknown_field")
        subjects = tuple(raw)
    else:
        raw = obj.get("terms")
        if (
            not isinstance(raw, list)
            or not raw
            or len(raw) > _MAX_TERMS
            or not all(isinstance(s, str) and 0 < len(s) <= _MAX_TERM_CHARS for s in raw)
        ):
            raise ProtocolError("bad_terms")
        if "subjects" in obj:
            raise ProtocolError("unknown_field")
        terms = tuple(raw)
    return Query(query_id=query_id, kind=obj["kind"], mode=obj["mode"],  # type: ignore[arg-type]
                 subjects=subjects, terms=terms)


def _parse_answer(obj: dict) -> Answer:
    _require_keys(
        obj,
        frozenset({"type", "query_id", "batch"}),
        frozenset({"type", "query_id", "batch"}),
    )
    if not isinstance(obj["batch"], dict):
        raise ProtocolError("bad_batch")
    return Answer(query_id=parse_query_id(obj["query_id"]), batch=obj["batch"])


def _parse_done(obj: dict) -> Done:
    _require_keys(obj, frozenset({"type", "query_id"}), frozenset({"type", "query_id"}))
    return Done(query_id=parse_query_id(obj["query_id"]))


def parse_message(raw: str | bytes) -> Message:
    """Parse and strictly validate one client message. Raises
    ProtocolError with an error class on any failure."""
    if isinstance(raw, str):
        size = len(raw.encode("utf-8"))
        text = raw
    else:
        size = len(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("bad_encoding") from None
    if size > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ProtocolError:
        raise
    except ValueError:
        raise ProtocolError("bad_json") from None
    if not isinstance(obj, dict):
        raise ProtocolError("not_an_object")
    mtype = obj.get("type")
    if mtype == "hello":
        return _parse_hello(obj)
    if mtype == "query":
        return _parse_query(obj)
    if mtype == "answer":
        return _parse_answer(obj)
    if mtype == "done":
        return _parse_done(obj)
    if mtype == "bye":
        _require_keys(obj, frozenset({"type"}), frozenset({"type"}))
        return Bye()
    raise ProtocolError("unknown_type")


def query_to_json(query: Query) -> str:
    """Canonical re-serialization of a validated query for forwarding:
    the relay forwards what it validated, never the raw frame."""
    obj: dict[str, object] = {
        "type": "query",
        "query_id": query.query_id,
        "kind": query.kind,
        "mode": query.mode,
    }
    if query.mode == "exact":
        obj["subjects"] = list(query.subjects)
    else:
        obj["terms"] = list(query.terms)
    return json.dumps(obj, separators=(",", ":"))


def relay_message_to_json(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))

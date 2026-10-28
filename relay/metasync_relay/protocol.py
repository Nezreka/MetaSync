"""Wire messages for the relay protocol (DESIGN-METADATA.md section 10).

JSON over the persistent connection. The relay validates KNOWN fields
strictly — types, values, and the shapes a mode requires — and treats
answer content as opaque: a batch is a JSON object it never inspects,
a sealed payload is bytes it cannot read at all (section 10: sealing
is HPKE to the asker's ephemeral key, and the relay's budgets and
stamps work on the envelope, never the content). Fields the protocol
does not know are IGNORED, never a refusal (section 10's closing
forward-compatibility rule): a newer client may add fields without
breaking this relay, and the canonical re-serialization below drops
them again on the way out, so an ignored field never travels. The
refusals that remain are the ones that protect meaning, not shape:
an ambiguous version form, a field that belongs to the OTHER query
mode (a client bug there must not silently reroute its search),
duplicate keys, and the size/depth caps.

Protocol versions: v1 is the original shape (single ``version: 1`` on
hello, one batch per answer). v2 negotiates by range on hello
(``version_min``/``version_max``) and adds: capabilities, the query's
``reply_key``/``reply_encoding``, packed answers (``batches``) and
sealed answers (``sealed``), in-band ``reauth``, the relay's
``complete`` signal, ``query_refused`` notices, and pushed
``revocations``. A v2 relay serves v1 connections with v1 shapes;
per-connection shape rules are the server's, this module parses the
union and validates whatever is present.

Early close (a post-v2 addition, negotiated by capability, not a
version bump): the asker's ``close`` message ends one of its own
queries before the window runs out. It needed no version gate of
its own — a relay that predates it raises ``unknown_type``, which
the dispatch already IGNORES for v2 connections (a violation only
on a v1 connection, and genuine v1 clients never send it), so the
message alone could never break a mixed deployment. What gates
sending is the welcome: this relay advertises ``early_close`` in
its v2 welcome capabilities, and a client sends ``close`` only to
a relay that advertised it. A peer that never sends one behaves
exactly as before: its queries end at the window.

JSON parsing is defensive: non-finite constants (NaN/Infinity, which
Python's json accepts by default and would re-emit) are refused, and
pathological nesting surfaces as a ProtocolError, never as a
RecursionError escaping the protocol layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .tokens import decode_base64url, encode_base64url

# Control messages stay small; answer frames may carry a packed,
# sealed payload, so the transport cap is the larger one and the
# per-message check is shape-aware (see parse_message).
MAX_MESSAGE_BYTES = 1_048_576  # 1MB: any non-answer message
MAX_FRAME_BYTES = 8_388_608  # 8MB: any single frame, answers included
DEFAULT_ANSWER_CAP_BYTES = 8_388_608  # 8MB of answers admitted per query

QUERY_KINDS = frozenset({"artist", "album", "track"})
QUERY_MODES = frozenset({"exact", "text"})
NODE_MODES = frozenset({"full", "own_library", "leech"})
REPLY_ENCODINGS = frozenset({"zstd-dict1", "none"})

_MAX_SUBJECTS = 100
_MAX_SUBJECT_CHARS = 512
_MAX_TERMS = 16
_MAX_TERM_CHARS = 256
_MAX_BATCHES_PER_ANSWER = 1000


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
    version_min: int = 1
    version_max: int = 1
    capabilities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Query:
    query_id: str  # normalized: 32 lowercase hex chars (128 bits)
    kind: str
    mode: str  # "exact" | "text"
    subjects: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    reply_key: bytes | None = None  # raw 32-byte X25519 public key (v2)
    reply_encoding: str | None = None


@dataclass(frozen=True)
class Answer:
    query_id: str
    batch: dict | None = None  # v1: one opaque section 6 envelope
    batches: tuple | None = None  # v2: several envelopes, packed
    sealed: bytes | None = None  # v2: HPKE ciphertext, opaque here


@dataclass(frozen=True)
class Done:
    query_id: str


@dataclass(frozen=True)
class Close:
    query_id: str


@dataclass(frozen=True)
class Reauth:
    token: str
    timestamp: int
    signature: bytes  # raw 64-byte Ed25519 signature


@dataclass(frozen=True)
class Bye:
    pass


Message = Hello | Query | Answer | Done | Close | Reauth | Bye


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError("duplicate_key")
        out[key] = value
    return out


def _reject_constant(_value: str):
    raise ProtocolError("bad_constant")


def _require_keys(obj: dict, required: frozenset[str]) -> None:
    """Only presence is enforced. Unknown fields are ignored (section
    10's forward-compatibility rule), so there is deliberately no
    allowed-set check here: adding one back would reject exactly the
    newer-client messages the rule exists to carry."""
    if not required <= frozenset(obj.keys()):
        raise ProtocolError("missing_field")


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


_HELLO_V1_KEYS = frozenset(
    {"type", "version", "node_key", "mode", "token", "timestamp", "challenge", "signature"}
)
_HELLO_V2_KEYS = frozenset(
    {
        "type", "version_min", "version_max", "node_key", "mode", "token",
        "timestamp", "challenge", "signature", "capabilities",
    }
)
_HELLO_V2_REQUIRED = _HELLO_V2_KEYS - {"capabilities"}


def _parse_hello(obj: dict) -> Hello:
    if "version" in obj and ("version_min" in obj or "version_max" in obj):
        # Both version forms at once is not an unknown-field case —
        # the two forms disagree about what the client is, and
        # guessing wrong could negotiate a silent downgrade.
        raise ProtocolError("bad_version")
    if "version" in obj:
        _require_keys(obj, _HELLO_V1_KEYS)
        if obj["version"] != 1 or not _is_int(obj["version"]):
            raise ProtocolError("bad_version")
        version_min = version_max = 1
        capabilities: dict = {}
    elif "version_min" in obj or "version_max" in obj:
        _require_keys(obj, _HELLO_V2_REQUIRED)
        version_min, version_max = obj["version_min"], obj["version_max"]
        if (
            not _is_int(version_min)
            or not _is_int(version_max)
            or not 1 <= version_min <= version_max <= 99
        ):
            raise ProtocolError("bad_version")
        capabilities = obj.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ProtocolError("bad_capabilities")
    else:
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
        version_min=version_min,  # type: ignore[arg-type]
        version_max=version_max,  # type: ignore[arg-type]
        capabilities=capabilities,
    )


def _parse_query(obj: dict) -> Query:
    _require_keys(obj, frozenset({"type", "query_id", "kind", "mode"}))
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
            # Not an unknown-field case: `terms` is a known field in
            # the wrong MODE. Ignoring it would silently reroute the
            # client's search as an exact query, so the refusal stays.
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
            # Same rule as `terms` on an exact query, mirrored.
            raise ProtocolError("unknown_field")
        terms = tuple(raw)
    reply_key: bytes | None = None
    if "reply_key" in obj:
        reply_key = decode_base64url(obj["reply_key"], 32)
        if reply_key is None:
            raise ProtocolError("bad_reply_key")
    reply_encoding: str | None = None
    if "reply_encoding" in obj:
        if obj["reply_encoding"] not in REPLY_ENCODINGS:
            raise ProtocolError("bad_reply_encoding")
        reply_encoding = obj["reply_encoding"]
    return Query(
        query_id=query_id,
        kind=obj["kind"],  # type: ignore[arg-type]
        mode=obj["mode"],  # type: ignore[arg-type]
        subjects=subjects,
        terms=terms,
        reply_key=reply_key,
        reply_encoding=reply_encoding,
    )


def _parse_answer(obj: dict) -> Answer:
    _require_keys(obj, frozenset({"type", "query_id"}))
    shapes = [name for name in ("batch", "batches", "sealed") if name in obj]
    if len(shapes) != 1:
        raise ProtocolError("bad_answer_shape")
    query_id = parse_query_id(obj["query_id"])
    if shapes == ["batch"]:
        if not isinstance(obj["batch"], dict):
            raise ProtocolError("bad_batch")
        return Answer(query_id=query_id, batch=obj["batch"])
    if shapes == ["batches"]:
        raw = obj["batches"]
        if (
            not isinstance(raw, list)
            or not raw
            or len(raw) > _MAX_BATCHES_PER_ANSWER
            or not all(isinstance(b, dict) for b in raw)
        ):
            raise ProtocolError("bad_batches")
        return Answer(query_id=query_id, batches=tuple(raw))
    sealed = decode_base64url(obj["sealed"])
    if sealed is None or len(sealed) > MAX_FRAME_BYTES:
        raise ProtocolError("bad_sealed")
    return Answer(query_id=query_id, sealed=sealed)


def _parse_done(obj: dict) -> Done:
    _require_keys(obj, frozenset({"type", "query_id"}))
    return Done(query_id=parse_query_id(obj["query_id"]))


def _parse_close(obj: dict) -> Close:
    _require_keys(obj, frozenset({"type", "query_id"}))
    return Close(query_id=parse_query_id(obj["query_id"]))


def _parse_reauth(obj: dict) -> Reauth:
    _require_keys(obj, frozenset({"type", "token", "timestamp", "signature"}))
    if not isinstance(obj["token"], str) or not obj["token"]:
        raise ProtocolError("bad_token")
    if not _is_int(obj["timestamp"]):
        raise ProtocolError("bad_timestamp")
    signature = decode_base64url(obj["signature"], 64)
    if signature is None:
        raise ProtocolError("bad_signature")
    return Reauth(
        token=obj["token"],
        timestamp=obj["timestamp"],  # type: ignore[arg-type]
        signature=signature,
    )


def parse_message(raw: str | bytes) -> Message:
    """Parse and validate one client message: known fields strictly,
    unknown fields ignored. Raises ProtocolError with an error class
    on any failure."""
    if isinstance(raw, str):
        size = len(raw.encode("utf-8"))
        text = raw
    else:
        size = len(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("bad_encoding") from None
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("message_too_large")
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except RecursionError:
        # Pathological nesting is a validation failure, not a crash:
        # the connection gets a violation, not a dead relay worker.
        raise ProtocolError("too_deep") from None
    except ValueError:
        raise ProtocolError("bad_json") from None
    if not isinstance(obj, dict):
        raise ProtocolError("not_an_object")
    if size > MAX_MESSAGE_BYTES and obj.get("type") != "answer":
        # Only answers may use the large frame budget; a padded
        # control message is refused at the same 1MB line as v1.
        raise ProtocolError("message_too_large")
    mtype = obj.get("type")
    if mtype == "hello":
        return _parse_hello(obj)
    if mtype == "query":
        return _parse_query(obj)
    if mtype == "answer":
        return _parse_answer(obj)
    if mtype == "done":
        return _parse_done(obj)
    if mtype == "close":
        return _parse_close(obj)
    if mtype == "reauth":
        return _parse_reauth(obj)
    if mtype == "bye":
        _require_keys(obj, frozenset({"type"}))
        return Bye()
    raise ProtocolError("unknown_type")


def query_to_json(query: Query, version: int = 1) -> str:
    """Canonical re-serialization of a validated query for forwarding:
    the relay forwards what it validated, never the raw frame. v1
    targets get the v1 shape (the reply key is stripped: a v1 node
    cannot seal, so its answers travel packed and unsealed)."""
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
    if version >= 2 and query.reply_key is not None:
        obj["reply_key"] = encode_base64url(query.reply_key)
        if query.reply_encoding is not None:
            obj["reply_encoding"] = query.reply_encoding
    return json.dumps(obj, separators=(",", ":"))


def answer_to_json(
    *,
    query_id: str,
    stamp_from: str,
    batch: dict | None = None,
    batches: tuple | list | None = None,
    sealed: bytes | None = None,
) -> str:
    """Canonical serialization of a forwarded answer for the asker.
    Every forwarded answer carries the relay's stamp: the
    authenticated node id of the connection that sent it, so the
    asker can pre-filter direct batches against it before paying for
    signature checks (sections 9/10)."""
    obj: dict[str, object] = {
        "type": "answer",
        "query_id": query_id,
        "stamp": {"from": stamp_from, "query_id": query_id},
    }
    if sealed is not None:
        obj["sealed"] = encode_base64url(sealed)
    elif batches is not None:
        obj["batches"] = list(batches)
    else:
        obj["batch"] = batch
    return json.dumps(obj, separators=(",", ":"))


def relay_message_to_json(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))

"""Protocol v2 parser tests (DESIGN-METADATA.md section 10, step 5):
hello version ranges, packed and sealed answer shapes, re-auth,
query serialization per negotiated version, and the parser
hardening rules — no RecursionError escapes, no NaN/Infinity
constants, frame caps sized to the batch cap.
"""

from __future__ import annotations

import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from metasync_relay.protocol import (
    Answer,
    Hello,
    ProtocolError,
    Query,
    Reauth,
    answer_to_json,
    parse_message,
    query_to_json,
    relay_message_to_json,
)
from metasync_relay.tokens import decode_base64url, encode_base64url

sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from conftest import NodeIdentity


def pm(obj):
    """parse_message takes raw wire text; dump dicts for readability."""
    import json as _json

    return parse_message(_json.dumps(obj) if isinstance(obj, dict) else obj)


@pytest.fixture()
def node() -> NodeIdentity:
    return NodeIdentity()


def _hello_v2(node: NodeIdentity, **overrides) -> dict:
    msg = {
        "type": "hello",
        "version_min": 1,
        "version_max": 2,
        "node_key": encode_base64url(node.public_raw),
        "mode": "own_library",
        "token": "v1.x.y",
        "timestamp": 1700000000,
        "challenge": encode_base64url(b"\x01" * 32),
        "signature": encode_base64url(b"\x02" * 64),
        "capabilities": {"sealing": ["hpke-x25519-chacha"], "encodings": ["zstd-dict1"]},
    }
    msg.update(overrides)
    return msg


def test_v2_hello_parses_with_capabilities(node):
    msg = pm(_hello_v2(node))
    assert isinstance(msg, Hello)
    assert (msg.version_min, msg.version_max) == (1, 2)
    assert msg.capabilities["encodings"] == ["zstd-dict1"]


def test_v2_hello_without_capabilities(node):
    raw = _hello_v2(node)
    del raw["capabilities"]
    msg = pm(raw)
    assert isinstance(msg, Hello) and msg.capabilities == {}


def test_v1_hello_still_parses(node):
    msg = pm(
        {
            "type": "hello",
            "version": 1,
            "node_key": encode_base64url(node.public_raw),
            "mode": "full",
            "token": "v1.x.y",
            "timestamp": 1700000000,
            "challenge": encode_base64url(b"\x01" * 32),
            "signature": encode_base64url(b"\x02" * 64),
        }
    )
    assert isinstance(msg, Hello)
    assert (msg.version_min, msg.version_max) == (1, 1)


def test_version_forms_are_exclusive(node):
    raw = _hello_v2(node)
    raw["version"] = 2
    with pytest.raises(ProtocolError):
        pm(raw)


def test_reauth_parses():
    msg = pm(
        {
            "type": "reauth",
            "token": "v1.x.y",
            "timestamp": 1700000000,
            "signature": encode_base64url(b"\x02" * 64),
        }
    )
    assert isinstance(msg, Reauth)


def _x25519_public_b64() -> str:
    key = X25519PrivateKey.generate().public_key()
    return encode_base64url(key.public_bytes(Encoding.Raw, PublicFormat.Raw))


def _query(**overrides) -> dict:
    msg = {
        "type": "query",
        "query_id": "a" * 32,
        "kind": "track",
        "mode": "exact",
        "subjects": ["soul_x"],
    }
    msg.update(overrides)
    return msg


def test_query_with_reply_key_parses():
    key = _x25519_public_b64()
    msg = pm(_query(reply_key=key, reply_encoding="zstd-dict1"))
    assert isinstance(msg, Query)
    assert msg.reply_key == decode_base64url(key)
    assert msg.reply_encoding == "zstd-dict1"


def test_query_reply_key_must_be_32_bytes():
    with pytest.raises(ProtocolError):
        pm(_query(reply_key=encode_base64url(b"\x01" * 16)))


def test_query_reply_encoding_validated():
    with pytest.raises(ProtocolError):
        pm(_query(reply_encoding="gzip"))


def test_query_serialization_strips_reply_fields_for_v1():
    import json as _json

    key = _x25519_public_b64()
    msg = pm(_query(reply_key=key, reply_encoding="none"))
    v1 = _json.loads(query_to_json(msg, 1))
    assert "reply_key" not in v1 and "reply_encoding" not in v1
    v2 = _json.loads(query_to_json(msg, 2))
    assert v2["reply_key"] == key


def _batch() -> dict:
    return {"batch": {"header": {"node": "x"}}, "sig": "y"}


def test_packed_answer_parses():
    msg = pm(
        {"type": "answer", "query_id": "a" * 32, "batches": [_batch(), _batch()]}
    )
    assert isinstance(msg, Answer)
    assert msg.batches is not None and len(msg.batches) == 2
    assert msg.batch is None and msg.sealed is None


def test_sealed_answer_parses():
    msg = pm(
        {"type": "answer", "query_id": "a" * 32, "sealed": encode_base64url(b"\x03" * 64)}
    )
    assert isinstance(msg, Answer)
    assert msg.sealed is not None and len(msg.sealed) == 64


def test_answer_forms_are_exclusive():
    with pytest.raises(ProtocolError):
        pm(
            {"type": "answer", "query_id": "a" * 32, "batch": _batch(), "batches": [_batch()]}
        )
    with pytest.raises(ProtocolError):
        pm({"type": "answer", "query_id": "a" * 32})


def test_answer_to_json_always_stamps():
    import json as _json

    out = _json.loads(
        answer_to_json(query_id="a" * 32, stamp_from="f" * 64, batch=_batch())
    )
    assert out["type"] == "answer"
    assert out["stamp"] == {"from": "f" * 64, "query_id": "a" * 32}
    assert out["batch"] == _batch()


def test_answer_to_json_forms():
    import json as _json

    packed = _json.loads(
        answer_to_json(query_id="a" * 32, stamp_from="f" * 64, batches=[_batch()])
    )
    assert packed["batches"] == [_batch()]
    sealed = _json.loads(
        answer_to_json(query_id="a" * 32, stamp_from="f" * 64, sealed=b"\x03" * 64)
    )
    assert sealed["sealed"] == encode_base64url(b"\x03" * 64)


def test_relay_only_message_types_are_not_client_messages():
    # complete / query_refused / reauth_ok travel relay -> client only;
    # they are built as plain dicts via relay_message_to_json, and a
    # client sending one gets the unknown-type path (ignored for v2
    # connections at dispatch, never parsed into a message object).
    for mtype in ("complete", "query_refused", "reauth_ok", "revocations"):
        with pytest.raises(ProtocolError):
            pm({"type": mtype, "query_id": "a" * 32})
    import json as _json

    out = _json.loads(
        relay_message_to_json({"type": "complete", "query_id": "a" * 32})
    )
    assert out == {"type": "complete", "query_id": "a" * 32}


# -- forward compatibility: unknown fields -----------------------------------


def test_v2_hello_with_unknown_fields_parses_identically(node):
    base = _hello_v2(node)
    extra = dict(base, future_capability_note="x", another={"nested": [1]})
    assert pm(extra) == pm(base)


def test_v2_query_unknown_fields_dropped_on_forward():
    import json as _json

    base = _query(reply_key=_x25519_public_b64(), reply_encoding="none")
    extra = dict(base, trace_id="abc", priority=3)
    assert pm(extra) == pm(base)
    # The relay forwards its canonical re-serialization, never the
    # raw frame: the unknown fields do not travel.
    forwarded = _json.loads(query_to_json(pm(extra), 2))
    assert "trace_id" not in forwarded and "priority" not in forwarded


def test_v2_answer_unknown_fields_ignored_and_client_stamp_dropped():
    # A client-supplied "stamp" is just another unknown field to the
    # parser: only the relay's own stamp (answer_to_json) means
    # anything, so a forged one is dropped, not carried.
    forged = {
        "type": "answer",
        "query_id": "a" * 32,
        "batches": [_batch()],
        "stamp": {"from": "attacker", "query_id": "a" * 32},
        "extra": True,
    }
    assert pm(forged) == pm(
        {"type": "answer", "query_id": "a" * 32, "batches": [_batch()]}
    )


def test_known_field_wrong_type_still_fails_with_unknown_fields_present(node):
    # Leniency covers fields the protocol does not know, not bad
    # values in fields it does.
    with pytest.raises(ProtocolError):
        pm(_hello_v2(node, mode=42, surprise=1))
    with pytest.raises(ProtocolError):
        pm(_query(subjects="not-a-list", surprise=1))
    with pytest.raises(ProtocolError):
        pm(_hello_v2(node, version_max="two", surprise=1))


def test_mode_foreign_known_field_still_refused():
    # `terms` on an exact query is a KNOWN field in the wrong mode —
    # ignoring it would silently reroute the search, so it stays a
    # refusal even under the leniency rule.
    with pytest.raises(ProtocolError):
        pm(_query(terms=["radiohead"]))
    text = {
        "type": "query",
        "query_id": "a" * 32,
        "kind": "track",
        "mode": "text",
        "terms": ["radiohead"],
        "subjects": ["soul_x"],  # subjects do not belong on a text query
    }
    with pytest.raises(ProtocolError):
        pm(text)


# -- parser hardening -------------------------------------------------------


def test_json_rejects_nan_and_infinity_constants():
    for bad in (
        '{"type": "done", "query_id": NaN}',
        '{"type": "done", "query_id": Infinity}',
        '{"type": "done", "query_id": -Infinity}',
    ):
        with pytest.raises(ProtocolError):
            pm(bad)


def test_json_deep_nesting_is_protocol_error_not_recursion():
    raw = '{"type": "answer", "query_id": "' + "a" * 32 + '", "batch": ' + "[" * 5000 + "]" * 5000 + "}"
    with pytest.raises(ProtocolError):
        pm(raw)


def test_oversize_frame_is_protocol_error():
    raw = '{"type": "done", "query_id": "' + "a" * 32 + '", "pad": "' + "x" * (9 * 1024 * 1024) + '"}'
    with pytest.raises(ProtocolError):
        pm(raw)

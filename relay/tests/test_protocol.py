"""Wire-message validation for the relay protocol."""

from __future__ import annotations

import json
import time

import pytest
from conftest import NodeIdentity
from metasync_relay.protocol import (
    MAX_MESSAGE_BYTES,
    Answer,
    Bye,
    Done,
    Hello,
    ProtocolError,
    Query,
    parse_message,
)
from metasync_relay.tokens import encode_base64url


def _hello(node: NodeIdentity, **overrides) -> str:
    obj = {
        "type": "hello",
        "version": 1,
        "node_key": encode_base64url(node.public_raw),
        "mode": "own_library",
        "token": "aaa.bbb.ccc",
        "timestamp": int(time.time()),
        "challenge": encode_base64url(b"\x02" * 32),
        "signature": encode_base64url(b"\x01" * 64),
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_hello_parses(node_a):
    msg = parse_message(_hello(node_a))
    assert isinstance(msg, Hello)
    assert msg.node_key == node_a.public_raw
    assert msg.mode == "own_library"
    assert msg.challenge == b"\x02" * 32
    assert len(msg.signature) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"version": "1"},
        {"version": True},
        {"mode": "everything"},
        {"mode": ["full"]},
        {"node_key": encode_base64url(b"\x00" * 31)},
        {"node_key": "not!base64"},
        {"token": ""},
        {"token": 42},
        {"timestamp": True},
        {"timestamp": "now"},
        {"challenge": encode_base64url(b"\x02" * 31)},
        {"challenge": "not!base64"},
        {"signature": encode_base64url(b"\x01" * 63)},
    ],
)
def test_hello_rejects_bad_shapes(node_a, overrides):
    with pytest.raises(ProtocolError):
        parse_message(_hello(node_a, **overrides))


def test_hello_rejects_missing_field(node_a):
    obj = json.loads(_hello(node_a))
    del obj["token"]
    with pytest.raises(ProtocolError):
        parse_message(json.dumps(obj))


def test_hello_rejects_missing_challenge(node_a):
    obj = json.loads(_hello(node_a))
    del obj["challenge"]
    with pytest.raises(ProtocolError):
        parse_message(json.dumps(obj))


QID = "0123456789abcdeffedcba9876543210"


def _query(**overrides) -> str:
    obj = {
        "type": "query",
        "query_id": QID,
        "kind": "album",
        "mode": "exact",
        "subjects": ["soul_abc"],
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_query_exact_parses():
    msg = parse_message(_query())
    assert isinstance(msg, Query)
    assert msg.query_id == QID
    assert msg.subjects == ("soul_abc",)


def test_query_text_parses():
    obj = json.loads(_query())
    obj["mode"] = "text"
    del obj["subjects"]
    obj["terms"] = ["radiohead", "ok computer"]
    msg = parse_message(json.dumps(obj))
    assert isinstance(msg, Query)
    assert msg.terms == ("radiohead", "ok computer")


def _text_query_with_subjects() -> str:
    obj = json.loads(_query())
    obj["mode"] = "text"
    obj["terms"] = ["radiohead"]  # subjects key still present: not allowed
    return json.dumps(obj)


@pytest.mark.parametrize(
    "raw",
    [
        _query(kind="label"),
        _query(kind=["album"]),
        _query(mode="fuzzy"),
        _query(subjects=[]),
        _query(subjects=["x"] * 101),
        _query(subjects=[42]),
        _query(subjects=[""]),
        _query(terms=["x"]),  # terms do not belong on an exact query
        _text_query_with_subjects(),
        _query(query_id="zz" * 16),
        _query(query_id="abcd"),
    ],
)
def test_query_rejects_bad_shapes(raw):
    with pytest.raises(ProtocolError):
        parse_message(raw)


def test_query_id_forms_normalize():
    upper = parse_message(_query(query_id=QID.upper()))
    assert isinstance(upper, Query) and upper.query_id == QID
    b64 = parse_message(_query(query_id=encode_base64url(bytes.fromhex(QID))))
    assert isinstance(b64, Query) and b64.query_id == QID


def test_answer_parses_and_batch_is_opaque():
    batch = {"claims": [{"anything": [1, 2.5, None, {"deep": True}]}], "sig": "x"}
    msg = parse_message(json.dumps({"type": "answer", "query_id": QID, "batch": batch}))
    assert isinstance(msg, Answer)
    assert msg.batch == batch


@pytest.mark.parametrize("batch", [[1, 2], "claims", 42, None])
def test_answer_rejects_non_object_batch(batch):
    with pytest.raises(ProtocolError):
        parse_message(json.dumps({"type": "answer", "query_id": QID, "batch": batch}))


def test_done_and_bye_parse():
    assert isinstance(parse_message(json.dumps({"type": "done", "query_id": QID})), Done)
    assert isinstance(parse_message(json.dumps({"type": "bye"})), Bye)


def test_unknown_fields_are_ignored_not_refused(node_a):
    # Section 10's forward-compatibility rule: a field this relay
    # predates is ignored on ANY message, and the parsed message is
    # identical to the one without it. (Fields known to the protocol
    # keep their strict checks — the bad-shape tests above — and a
    # field belonging to the other query MODE stays refused: see
    # test_query_rejects_bad_shapes.)
    hello = json.loads(_hello(node_a))
    assert parse_message(json.dumps({**hello, "surprise": 1})) == parse_message(
        json.dumps(hello)
    )
    assert parse_message(_query(future_field={"x": [1, 2]})) == parse_message(_query())
    assert parse_message(
        json.dumps({"type": "done", "query_id": QID, "extra": 1})
    ) == Done(query_id=QID)
    assert isinstance(parse_message(json.dumps({"type": "bye", "extra": 1})), Bye)
    batch = {"claims": []}
    with_extra = parse_message(
        json.dumps({"type": "answer", "query_id": QID, "batch": batch, "extra": "x"})
    )
    assert with_extra == parse_message(
        json.dumps({"type": "answer", "query_id": QID, "batch": batch})
    )


def test_unknown_type_and_non_objects_rejected():
    for raw in ['{"type":"teleport"}', "[1,2]", '"hello"', "42", "not json", ""]:
        with pytest.raises(ProtocolError):
            parse_message(raw)


def test_duplicate_keys_rejected():
    raw = '{"type":"done","query_id":"' + QID + '","query_id":"' + QID + '"}'
    with pytest.raises(ProtocolError):
        parse_message(raw)


def test_oversize_message_rejected():
    raw = '{"type":"bye","pad":"' + "x" * MAX_MESSAGE_BYTES + '"}'
    with pytest.raises(ProtocolError) as excinfo:
        parse_message(raw)
    assert excinfo.value.reason == "message_too_large"

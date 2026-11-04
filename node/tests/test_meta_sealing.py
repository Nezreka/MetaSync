"""Sealing + compression tests (DESIGN-METADATA.md sections 10/11,
protocol v2 step 5): the hand-rolled HPKE must match RFC 9180, the
payload codec must round-trip and refuse bombs, and the protocol
dictionary must be byte-identical in the node and relay packages.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.meta import sealing

NODE_DICT = Path(__file__).resolve().parents[1] / "metasync_node" / "meta" / "protocol_dict_v1.bin"
RELAY_DICT = (
    Path(__file__).resolve().parents[2] / "relay" / "metasync_relay" / "protocol_dict_v1.bin"
)

# RFC 9180 Appendix A.2.1: DHKEM(X25519, HKDF-SHA256), HKDF-SHA256,
# ChaCha20-Poly1305, base mode, one-shot with AAD "Count-0".
RFC_INFO = bytes.fromhex("4f6465206f6e2061204772656369616e2055726e")
RFC_SK_EM = bytes.fromhex(
    "f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600"
)
RFC_PK_RM = bytes.fromhex(
    "4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a"
)
RFC_SK_RM = bytes.fromhex(
    "8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb"
)
RFC_ENC = bytes.fromhex(
    "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a"
)
RFC_PT = b"Beauty is truth, truth beauty"
RFC_AAD = b"Count-0"
RFC_CT = bytes.fromhex(
    "1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938e131f0def8c8b60b4db2"
    "1993c62ce81883d2dd1b51a28"
)


def test_hpke_matches_rfc9180_test_vector():
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    ephemeral = X25519PrivateKey.from_private_bytes(RFC_SK_EM)
    raw = ephemeral.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    assert raw == RFC_SK_EM
    sealed = sealing.seal(
        RFC_PK_RM,
        info=RFC_INFO,
        plaintext=RFC_PT,
        aad=RFC_AAD,
        _ephemeral_private=ephemeral,
    )
    assert sealed[:32] == RFC_ENC, "encapsulated key differs from the RFC vector"
    assert sealed[32:] == RFC_CT, "ciphertext differs from the RFC vector"
    opened = sealing.open_sealed(RFC_SK_RM, info=RFC_INFO, sealed=sealed, aad=RFC_AAD)
    assert opened == RFC_PT


def test_seal_open_round_trip_and_wrong_key_fails():
    private, public = sealing.generate_reply_keypair()
    sealed = sealing.seal(public, info=b"query-1", plaintext=b"payload")
    assert sealing.open_sealed(private, info=b"query-1", sealed=sealed) == b"payload"
    other_private, _other_public = sealing.generate_reply_keypair()
    with pytest.raises(ValueError):
        sealing.open_sealed(other_private, info=b"query-1", sealed=sealed)
    with pytest.raises(ValueError):
        sealing.open_sealed(private, info=b"query-2", sealed=sealed)


def test_payload_codec_round_trip_both_encodings():
    batches = [
        {"batch": {"header": {"node": "abc"}, "claims": [{"n": i}]}, "sig": "x"}
        for i in range(50)
    ]
    for encoding in sealing.supported_encodings():
        data = sealing.encode_payload(batches, encoding)
        assert sealing.decode_payload(data) == batches


def test_payload_codec_compresses_repetitive_claims():
    batches = [{"claims": [{"subject": "same", "field": "name"}]} for _ in range(200)]
    plain = sealing.encode_payload(batches, sealing.ENCODING_NONE)
    if sealing.ENCODING_ZSTD_DICT in sealing.supported_encodings():
        packed = sealing.encode_payload(batches, sealing.ENCODING_ZSTD_DICT)
        assert len(packed) < len(plain) // 4


def test_decode_payload_enforces_decompressed_cap():
    batches = [{"claims": [{"blob": "x" * 1000}]} for _ in range(50)]
    data = sealing.encode_payload(batches, sealing.ENCODING_NONE)
    with pytest.raises(ValueError):
        sealing.decode_payload(data, max_bytes=1024)


def test_decode_payload_enforces_decompressed_cap_on_zstd_path():
    # The compression-bomb case, on the path that actually decompresses:
    # a small frame expanding to many times the cap must be refused, and
    # output past the cap must never be materialized. (ZstdDecompressor's
    # max_output_size kwarg does not enforce this on its own.)
    if sealing.ENCODING_ZSTD_DICT not in sealing.supported_encodings():
        pytest.skip("zstd dictionary encoding unavailable here")
    batches = [{"claims": [{"blob": "x" * 4096}]} for _ in range(512)]
    data = sealing.encode_payload(batches, sealing.ENCODING_ZSTD_DICT)
    assert len(data) < 64 * 1024  # tiny on the wire, ~2MB expanded
    with pytest.raises(ValueError):
        sealing.decode_payload(data, max_bytes=64 * 1024)
    # The same payload under a cap that fits it decodes fine.
    assert sealing.decode_payload(data, max_bytes=4 * 1024 * 1024) == batches


def test_decode_payload_zstd_cap_is_a_hard_bound_not_a_hint():
    # A payload expanding to just past the cap is refused; one expanding
    # to exactly the cap is accepted (the bound is exact, not fuzzy).
    if sealing.ENCODING_ZSTD_DICT not in sealing.supported_encodings():
        pytest.skip("zstd dictionary encoding unavailable here")
    body = {"batches": [{"claims": [{"blob": "y" * 1000}]}]}
    import json as _json

    raw = _json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    data = sealing.encode_payload(body["batches"], sealing.ENCODING_ZSTD_DICT)
    assert sealing.decode_payload(data, max_bytes=len(raw)) == body["batches"]
    with pytest.raises(ValueError):
        sealing.decode_payload(data, max_bytes=len(raw) - 1)


def test_decode_payload_rejects_garbage_and_bad_shapes():
    with pytest.raises(ValueError):
        sealing.decode_payload(b"\xffnot a payload")
    with pytest.raises(ValueError):
        sealing.decode_payload(b"\x00" + b'{"not": "a list"}')


def test_dictionary_byte_identity_across_packages():
    node_bytes = NODE_DICT.read_bytes()
    assert len(node_bytes) > 0
    assert sealing.protocol_dictionary() == node_bytes
    if RELAY_DICT.exists():
        assert RELAY_DICT.read_bytes() == node_bytes, (
            "the relay and node protocol dictionaries must be byte-identical"
        )
    assert sealing.dictionary_id()  # a stable non-empty id for the hello


def test_sealing_reports_availability_without_raising():
    # Whatever the environment, these must answer, never raise: the
    # client negotiates from them and must not brick.
    assert isinstance(sealing.sealing_available(), bool)
    assert sealing.ENCODING_NONE in sealing.supported_encodings()

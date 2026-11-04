"""Sealed, compressed answer payloads — DESIGN-METADATA.md section 10.

Answers are sealed to the query's ephemeral ``reply_key`` so the relay
forwards ciphertext and never sees answer content (§12 Q2), and inside
the seal the batches are compressed with zstd and the shared protocol
dictionary — compression after signing, so signatures always cover the
canonical uncompressed bytes and compression can never blur what a
signature attests.

Sealing is HPKE (RFC 9180) base mode with the suite the design names:
KEM 0x0020 (DHKEM(X25519, HKDF-SHA256)), KDF 0x0001 (HKDF-SHA256),
AEAD 0x0003 (ChaCha20-Poly1305). It is implemented here from
``cryptography`` primitives rather than taken from that library's own
HPKE module: the module only exists in newer ``cryptography`` releases
(the node supports >= 42) and its single-shot API offers neither AAD
nor a pinnable ephemeral key, both of which the conformance story
needs. The HPKE ``info`` string is the query's ``query_id``, so a
ciphertext replayed under a different query does not open.

Everything cryptographic degrades, never bricks: if X25519 support is
somehow absent, ``sealing_available()`` is False and the relay client
negotiates plain (packed, unsealed) answers instead; compression is
negotiated per query and falls back to ``none`` whenever ``zstandard``
is not importable.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from pathlib import Path

# -- protocol constants ---------------------------------------------------

#: Wire encoding names, advertised in hello and chosen per query.
ENCODING_ZSTD_DICT = "zstd-dict1"
ENCODING_NONE = "none"

#: Prefix byte inside the sealed plaintext naming the payload encoding.
_PREFIX_NONE = 0
_PREFIX_ZSTD_DICT = 1

#: Hard decompressed-size cap for one answer payload (compression-bomb
#: guard, section 10); matches the relay's per-query total byte cap.
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024

_DICT_PATH = Path(__file__).with_name("protocol_dict_v1.bin")


def protocol_dictionary() -> bytes:
    """The shared protocol dictionary's bytes (identical file shipped
    in the relay package; a test asserts the two copies agree)."""
    try:
        return _DICT_PATH.read_bytes()
    except OSError:
        return b""


def dictionary_id() -> str:
    """Short fingerprint of the dictionary, advertised in hello so a
    peer can tell at a glance whether both sides mean the same dict."""
    data = protocol_dictionary()
    if not data:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


# -- availability -----------------------------------------------------------


def sealing_available() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: F401
            X25519PrivateKey,
        )

        return True
    except Exception:  # noqa: BLE001 - any import failure means unavailable
        return False


def _zstd():
    """The zstandard module, or None. Lazy: compression is an optional
    accelerator for this feature, never a boot dependency."""
    try:
        import zstandard

        return zstandard
    except Exception:  # noqa: BLE001
        return None


def supported_encodings() -> list[str]:
    """Encodings this node can produce, best first. ``none`` is always
    available; the dictionary encoding needs both the library and the
    dictionary file."""
    out: list[str] = []
    if _zstd() is not None and protocol_dictionary():
        out.append(ENCODING_ZSTD_DICT)
    out.append(ENCODING_NONE)
    return out


# -- HPKE (RFC 9180, base mode, suite 0x0020/0x0001/0x0003) -----------------

_KEM_ID = 0x0020
_KDF_ID = 0x0001
_AEAD_ID = 0x0003
_MODE_BASE = 0
_HASH_LEN = 32  # SHA-256
_KEY_LEN = 32  # ChaCha20-Poly1305
_NONCE_LEN = 12
_TAG_LEN = 16
_ENC_LEN = 32  # X25519 public key


def _labeled_extract(salt: bytes, suite_id: bytes, label: bytes, ikm: bytes) -> bytes:
    return _hmac.new(
        salt or bytes(_HASH_LEN),
        b"HPKE-v1" + suite_id + label + ikm,
        hashlib.sha256,
    ).digest()


def _labeled_expand(
    prk: bytes, suite_id: bytes, label: bytes, info: bytes, length: int
) -> bytes:
    labeled_info = (
        length.to_bytes(2, "big") + b"HPKE-v1" + suite_id + label + info
    )
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = _hmac.new(
            prk, block + labeled_info + bytes([counter]), hashlib.sha256
        ).digest()
        out += block
        counter += 1
    return out[:length]


def _kem_encap(public_raw: bytes, ephemeral_private=None):
    """DHKEM Encapsulate: returns (enc, shared_secret). The ephemeral
    key is injectable only so the RFC 9180 test vector can pin it;
    production callers let it be generated."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )

    sk_e = ephemeral_private or X25519PrivateKey.generate()
    enc = sk_e.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    dh = sk_e.exchange(X25519PublicKey.from_public_bytes(public_raw))
    suite_id = b"KEM" + _KEM_ID.to_bytes(2, "big")
    kem_context = enc + public_raw
    eae_prk = _labeled_extract(b"", suite_id, b"eae_prk", dh)
    shared_secret = _labeled_expand(
        eae_prk, suite_id, b"shared_secret", kem_context, _HASH_LEN
    )
    return enc, shared_secret


def _kem_decap(private_raw: bytes, enc: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )

    sk_r = X25519PrivateKey.from_private_bytes(private_raw)
    public_raw = sk_r.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    dh = sk_r.exchange(X25519PublicKey.from_public_bytes(enc))
    suite_id = b"KEM" + _KEM_ID.to_bytes(2, "big")
    kem_context = enc + public_raw
    eae_prk = _labeled_extract(b"", suite_id, b"eae_prk", dh)
    return _labeled_expand(
        eae_prk, suite_id, b"shared_secret", kem_context, _HASH_LEN
    )


def _key_schedule(shared_secret: bytes, info: bytes):
    """Base-mode key schedule: returns (AEAD key, base nonce)."""
    # The context suite_id (RFC 9180 section 5.1) is "HPKE" || kem_id ||
    # kdf_id || aead_id — the mode byte appears only inside
    # key_schedule_context, never in the suite_id.
    suite_id = (
        b"HPKE"
        + _KEM_ID.to_bytes(2, "big")
        + _KDF_ID.to_bytes(2, "big")
        + _AEAD_ID.to_bytes(2, "big")
    )
    psk_id_hash = _labeled_extract(b"", suite_id, b"psk_id_hash", b"")
    info_hash = _labeled_extract(b"", suite_id, b"info_hash", info)
    key_schedule_context = bytes([_MODE_BASE]) + psk_id_hash + info_hash
    secret = _labeled_extract(shared_secret, suite_id, b"secret", b"")
    key = _labeled_expand(secret, suite_id, b"key", key_schedule_context, _KEY_LEN)
    base_nonce = _labeled_expand(
        secret, suite_id, b"base_nonce", key_schedule_context, _NONCE_LEN
    )
    return key, base_nonce


def generate_reply_keypair() -> tuple[bytes, bytes]:
    """A fresh ephemeral X25519 keypair for one query:
    (private raw bytes, public raw bytes for the ``reply_key`` field)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    sk = X25519PrivateKey.generate()
    private_raw = sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private_raw, public_raw


def seal(
    recipient_public_raw: bytes,
    info: bytes,
    plaintext: bytes,
    aad: bytes = b"",
    *,
    _ephemeral_private=None,
) -> bytes:
    """Seal one payload: returns ``enc || ciphertext``. Single-shot
    (sequence number 0), so the nonce is the base nonce itself."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    enc, shared_secret = _kem_encap(recipient_public_raw, _ephemeral_private)
    key, nonce = _key_schedule(shared_secret, info)
    return enc + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def open_sealed(
    recipient_private_raw: bytes, info: bytes, sealed: bytes, aad: bytes = b""
) -> bytes:
    """Open a sealed payload. Raises ValueError on any failure (bad
    shape, wrong key, wrong info, tampered ciphertext)."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if len(sealed) < _ENC_LEN + _TAG_LEN:
        raise ValueError("sealed payload too short")
    enc, ciphertext = sealed[:_ENC_LEN], sealed[_ENC_LEN:]
    try:
        shared_secret = _kem_decap(recipient_private_raw, enc)
        key, nonce = _key_schedule(shared_secret, info)
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    except Exception as err:  # every open failure is one class
        raise ValueError("sealed payload does not open") from err


def _decompress_bounded(decompressor, body: bytes, max_bytes: int) -> bytes:
    """Stream-decompress ``body`` reading at most ``max_bytes + 1``
    bytes of output, refusing anything larger. ``ZstdDecompressor.
    decompress(max_output_size=...)`` does not enforce its limit (it
    happily returns many times that), so the cap is applied here by
    construction: output past the bound is never materialized."""
    import io

    reader = decompressor.stream_reader(io.BytesIO(body))
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = reader.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        raise ValueError("answer payload over size cap")
    return b"".join(chunks)


# -- answer payload codec ---------------------------------------------------


def encode_payload(batches: list, encoding: str) -> bytes:
    """Serialize an answer's batches for sealing: a prefix byte naming
    the encoding, then the (possibly dictionary-compressed) JSON body
    ``{"batches": [...]}``. Falls back to the plain body whenever the
    requested encoding is unavailable here — the prefix byte is what
    the asker reads, so the fallback is self-describing."""
    body = json.dumps(
        {"batches": list(batches)}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if encoding == ENCODING_ZSTD_DICT:
        zstd = _zstd()
        dictionary = protocol_dictionary()
        if zstd is not None and dictionary:
            compressor = zstd.ZstdCompressor(
                dict_data=zstd.ZstdCompressionDict(dictionary)
            )
            return bytes([_PREFIX_ZSTD_DICT]) + compressor.compress(body)
    return bytes([_PREFIX_NONE]) + body


def decode_payload(data: bytes, *, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> list:
    """Inverse of encode_payload. The decompressed size is capped
    BEFORE parsing: a payload that expands past ``max_bytes`` is
    refused, never materialized."""
    if not data:
        raise ValueError("empty answer payload")
    prefix, body = data[0], data[1:]
    if prefix == _PREFIX_NONE:
        if len(body) > max_bytes:
            raise ValueError("answer payload over size cap")
        raw = body
    elif prefix == _PREFIX_ZSTD_DICT:
        zstd = _zstd()
        dictionary = protocol_dictionary()
        if zstd is None or not dictionary:
            raise ValueError("zstd dictionary encoding not available here")
        decompressor = zstd.ZstdDecompressor(
            dict_data=zstd.ZstdCompressionDict(dictionary)
        )
        try:
            raw = _decompress_bounded(decompressor, body, max_bytes)
        except ValueError:
            raise
        except Exception as err:  # bad frames are one class
            raise ValueError("answer payload does not decompress") from err
    else:
        raise ValueError(f"unknown answer payload prefix {prefix}")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise ValueError("answer payload is not valid JSON") from err
    if not isinstance(obj, dict) or not isinstance(obj.get("batches"), list):
        raise ValueError("answer payload has no batches list")  # noqa: TRY004 - the codec raises one error class: ValueError
    return obj["batches"]

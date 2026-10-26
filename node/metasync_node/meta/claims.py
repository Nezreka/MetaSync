"""Claims and signed batches — DESIGN-METADATA.md §6.

The claim is the unit of data in the metadata network: one field of one
entity, asserted by one contributor. Claims travel in **batches**: a
canonical-JSON payload signed once with the node's SIN-bound Ed25519
key (PORTAL-DESIGN §3A.2). Per-claim signatures are deliberately absent
— the published corpus strips contributor identity, so nobody could
ever verify one; the batch signature gives the compiler
non-repudiation at ingest, the only place identity exists.

Canonicalization is a strict subset of JCS (RFC 8785): object keys
sorted, no insignificant whitespace, UTF-8, strings as-is. Floats are
forbidden anywhere in a batch (JCS number serialization is where
canonical-JSON implementations diverge; claim values never need them —
durations are integer milliseconds). A batch that contains a float is
invalid, on both the signing and the verifying side.
"""

import base64
import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import soulid as _soulid

BATCH_VERSION = 1
SOULID_VERSION = _soulid.SOULID_VERSION

# A SoulID is "soul_" + 16 lowercase hex chars (soulid.generate_soul_id).
# Anything else — "soul_" alone, SoulSync's soul_unnamed_<pk> placeholder
# (which also leaks a local primary key), legacy IDs — is not a v1 claim
# subject (§6: legacy subjects are alias entries, never v2 subjects).
_SUBJECT_RE = re.compile(r"^soul_[0-9a-f]{16}$")

KINDS = ("artist", "album", "track")

# Fields a claim may assert in v1. Provider-id fields are named
# "<provider>_id"; mbid fields are kind-explicit (DESIGN §7).
PROVIDER_ID_FIELDS = (
    "spotify_id", "deezer_id", "tidal_id", "itunes_id", "discogs_id",
    "hydrabase_id",
)
MBID_FIELDS = ("artist_mbid", "release_group_mbid", "recording_mbid")
SCALAR_FIELDS = ("isrc", "duration_ms", "year", "genres", "label")
SPECIAL_FIELDS = ("name", "display_name", "album_track", "provider_link")
ALL_FIELDS = PROVIDER_ID_FIELDS + MBID_FIELDS + SCALAR_FIELDS + SPECIAL_FIELDS

# `source` enum (§6/G1). "user_edit" is RESERVED: the SoulSync export
# cannot mark user edits in v1 (G2), so nothing may emit it yet —
# validate_claim rejects it even though it is in the enum.
SOURCES = (
    "musicbrainz", "spotify", "deezer", "tidal", "itunes", "discogs",
    "hydrabase", "file_tags", "user_edit",
)
RESERVED_SOURCES = ("user_edit",)

MAX_CLAIMS_PER_BATCH = 50_000          # backfill allowance (§6)
MAX_CLAIMS_PER_BATCH_STEADY = 5_000   # steady-state daily cap (§6)
MAX_NAME_CHARS = 512                   # ingest cap (§10)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ClaimError(ValueError):
    """A claim or batch that must be refused, not repaired."""


def _check_canonical_value(v, path="$"):
    if isinstance(v, float):
        raise ClaimError(f"float forbidden in batch at {path}")
    if isinstance(v, int) and not isinstance(v, bool) \
            and abs(v) > 2**53 - 1:
        # Beyond 2^53 a JCS (double-based) implementation serializes a
        # different number than Python does — the two sides would hash
        # different bytes for the same batch.
        raise ClaimError(f"integer out of canonical range at {path}")
    if isinstance(v, dict):
        for k, val in v.items():
            if not isinstance(k, str):
                raise ClaimError(f"non-string key at {path}")
            _check_canonical_value(val, f"{path}.{k}")
    elif isinstance(v, (list, tuple)):
        for i, val in enumerate(v):
            _check_canonical_value(val, f"{path}[{i}]")
    elif not isinstance(v, (str, int, bool)) and v is not None:
        raise ClaimError(f"unsupported type {type(v).__name__} at {path}")


def canonical_bytes(payload) -> bytes:
    """The exact bytes that are hashed and signed for a batch payload."""
    _check_canonical_value(payload)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def validate_claim(claim) -> dict:
    """Validate one claim dict; returns it unchanged or raises ClaimError."""
    if not isinstance(claim, dict):
        raise ClaimError("claim must be an object")
    subject = claim.get("subject")
    if not isinstance(subject, str) or not _SUBJECT_RE.match(subject):
        raise ClaimError("claim.subject must be a SoulID string")
    if claim.get("kind") not in KINDS:
        raise ClaimError(f"claim.kind must be one of {KINDS}")
    field = claim.get("field")
    if field not in ALL_FIELDS:
        raise ClaimError(f"unknown claim field: {field!r}")
    if claim.get("source") not in SOURCES:
        raise ClaimError(f"unknown claim source: {claim.get('source')!r}")
    if claim.get("source") in RESERVED_SOURCES:
        raise ClaimError(
            f"claim source {claim['source']!r} is reserved in v1 (G2)")
    if claim.get("soulid_version") != SOULID_VERSION:
        raise ClaimError("claim.soulid_version must be kinded_v2")
    if "value" not in claim:
        raise ClaimError("claim.value missing")
    value = claim["value"]
    if field in ("name", "display_name"):
        # value = the name parts as a list, same shape as the SoulID
        # input tuple (normalized parts for "name", human spelling
        # for "display_name")
        arity = {"artist": 1, "album": 2, "track": 2}[claim["kind"]]
        if (not isinstance(value, list) or len(value) != arity
                or not all(isinstance(p, str) for p in value)):
            raise ClaimError(
                f"{field} claim value must be the name-parts list")
        if any(len(p) > MAX_NAME_CHARS for p in value):
            raise ClaimError("name part over length cap")
        if any(not p.strip() for p in value):
            raise ClaimError("name part must not be blank")
        if field == "name" and \
                _soulid.generate_soul_id(claim["kind"], *value) != subject:
            # §6: a name claim is self-verifying — the compiler
            # recomputes the SoulID from the tuple and a mismatch
            # invalidates the entity's claims. Enforced here, in the
            # one shared validator, so signer and verifier both do it.
            raise ClaimError(
                "name claim tuple does not recompute to claim.subject")
    elif field in PROVIDER_ID_FIELDS + MBID_FIELDS + ("isrc",):
        # ID-shaped values are strings (§6 examples). An int or list
        # here would poison the merge (mixed-type set sorting crashes
        # a compile), so it is refused at the door, not repaired.
        if not isinstance(value, str) or not value:
            raise ClaimError(f"{field} claim value must be a string")
    elif field == "duration_ms":
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < 0:
            raise ClaimError("duration_ms claim value must be int ms")
    elif field == "year":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ClaimError("year claim value must be an int")
    elif field in ("genres", "label"):
        # One claim per genre string (§8 merge input contract).
        if not isinstance(value, str) or not value:
            raise ClaimError(f"{field} claim value must be a string")
    elif field == "album_track":
        if not isinstance(value, dict) or \
                set(value) != {"track", "disc", "position", "variant"}:
            raise ClaimError(
                "album_track value needs exactly "
                "track/disc/position/variant")
        if not isinstance(value["track"], str) or \
                not _SUBJECT_RE.match(value["track"]):
            raise ClaimError("album_track track must be a SoulID string")
        for k in ("disc", "position"):
            if isinstance(value[k], bool) or not isinstance(value[k], int) \
                    or value[k] < 1:
                raise ClaimError(f"album_track {k} must be a positive int")
        if value["variant"] is not None and \
                not isinstance(value["variant"], str):
            raise ClaimError("album_track variant must be a string or null")
    _check_canonical_value(claim, "claim")
    return claim


def _check_batch_claims(claims) -> None:
    """Batch-level claim rules (§6): at most one name claim per entity
    per batch — the name claim is the entity's identity anchor in the
    batch, and two different tuples for one subject would make the
    recompute check ambiguous."""
    seen = set()
    for claim in claims:
        if claim.get("field") == "name":
            if claim["subject"] in seen:
                raise ClaimError(
                    "duplicate name claim for subject in one batch")
            seen.add(claim["subject"])


def _check_retractions(retractions) -> None:
    if not isinstance(retractions, list) or \
            not all(isinstance(r, dict) for r in retractions):
        raise ClaimError("retractions must be a list of objects")


def make_batch(node_pubkey_b64: str, claims, retractions=None) -> dict:
    """Build an unsigned batch envelope from validated claims."""
    claims = [validate_claim(c) for c in claims]
    if len(claims) > MAX_CLAIMS_PER_BATCH:
        raise ClaimError("batch over claim cap")
    _check_batch_claims(claims)
    _check_retractions(retractions or [])
    header = {
        "version": BATCH_VERSION,
        "node": node_pubkey_b64,
        "claim_count": len(claims),
    }
    batch = {"header": header, "claims": claims,
             "retractions": retractions or []}
    _check_canonical_value(batch, "batch")
    return batch


def batch_digest(batch: dict) -> str:
    """SHA-256 hex of the canonical batch payload (header+claims+
    retractions). This digest — not the whole payload — is what gets
    signed, and it is the batch's identity at ingest."""
    return hashlib.sha256(canonical_bytes(batch)).hexdigest()


def sign_batch(private_key: Ed25519PrivateKey, batch: dict) -> dict:
    """Return the wire envelope: batch + signature over its digest."""
    digest = bytes.fromhex(batch_digest(batch))
    sig = private_key.sign(digest)
    return {"batch": batch, "sig": b64url_encode(sig)}


def verify_batch(public_key_b64: str, envelope: dict) -> dict:
    """Verify a wire envelope; returns the batch or raises ClaimError.

    Checks, in order: the header names this key, the signature is the
    key's over the batch digest, and every claim validates. Server
    receipt time is assigned by the receiver, never taken from the
    batch (§6).
    """
    try:
        batch = envelope["batch"]
        sig = b64url_decode(envelope["sig"])
        header = batch["header"]
        if not isinstance(header, dict):
            raise ClaimError("batch header must be an object")
        pub = Ed25519PublicKey.from_public_bytes(
            b64url_decode(public_key_b64))
        digest = bytes.fromhex(batch_digest(batch))
    except ClaimError:
        raise
    except Exception as e:
        # A malformed key, bad base64, or unencodable payload (lone
        # surrogates) is a refusal, not a server error.
        raise ClaimError(f"malformed envelope: {e}") from e
    if header.get("version") != BATCH_VERSION:
        raise ClaimError("unsupported batch version")
    if header.get("node") != public_key_b64:
        raise ClaimError("batch header node does not match the signing key")
    try:
        pub.verify(sig, digest)
    except InvalidSignature as e:
        raise ClaimError("batch signature invalid") from e
    claims = batch.get("claims")
    if not isinstance(claims, list):
        raise ClaimError("batch claims must be a list")
    if len(claims) > MAX_CLAIMS_PER_BATCH:
        raise ClaimError("batch over claim cap")
    if header.get("claim_count") != len(claims):
        raise ClaimError("claim_count does not match claims")
    for claim in claims:
        validate_claim(claim)
    _check_batch_claims(claims)
    _check_retractions(batch.get("retractions", []))
    return batch

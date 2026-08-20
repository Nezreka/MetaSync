"""The claim — MetaSync's unit of exchange, and the whole protocol surface.

A claim says: *"I hold this entity in my library, and these are the provider ids
I resolved for it myself."* Three properties make it trustworthy without a
reputation system:

1. **Self-verifying key.** ``key`` is a pure hash of the normalized name parts
   carried in ``norm``. Any receiver re-derives it. A peer cannot bind a payload
   to a key it did not legitimately derive.
2. **Never re-signed.** A relayed claim keeps its original ``node`` and ``sig``.
   Corroboration is therefore a count of *distinct observers*, not of copies —
   which is what stops the network echoing its own output back as consensus.
3. **Independent observation only.** A node may only sign claims its own
   enrichment resolved (SoulSync match_status == 'matched' from that provider's
   own worker). Ids MetaSync itself supplied are never re-exported; see
   ``export.py`` for where that filter is enforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidSignature

from . import canonical, schema, soulid
from .identity import NodeIdentity, public_key_from_id

WIRE_VERSION = 1

# Where a claim came from, which is not the same question as whether it is
# correct. An OBSERVED claim is possession-backed: a node's own library held the
# entity and its own enrichment resolved the ids. A DATASET claim is derived
# from a public corpus (a MusicBrainz dump, say) — often more accurate than any
# single library, but it is one source no matter how many nodes redistribute it,
# so it must never be counted as independent corroboration. One public dump
# would otherwise outvote every real person on the network.
SRC_OBSERVED = "observed"
SRC_DATASET = "dataset"
SOURCES = (SRC_OBSERVED, SRC_DATASET)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_RE = re.compile(r"^soul_[0-9a-f]{16}$")

_TOP_LEVEL_KEYS = frozenset({
    "v", "entity", "key", "song_key", "key_path", "norm", "ids", "facts",
    "observed_on", "src",
})


class InvalidClaim(ValueError):
    """A claim is malformed, over-limit, or its key does not match its parts."""


@dataclass(frozen=True)
class Claim:
    """One node's observation about one entity."""

    entity: str
    norm: Mapping[str, str]
    ids: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)
    observed_on: str = ""
    key_path: Optional[str] = None   # artist only — see soulid.ARTIST_PATHS
    src: str = SRC_OBSERVED

    @property
    def key(self) -> str:
        """The rendezvous key, derived (never stored) from the normalized parts."""
        n = self.norm
        if self.entity == "artist":
            return soulid.artist_key(n["artist"], n.get("qualifier"))
        if self.entity == "album":
            return soulid.album_key(n["artist"], n["album"])
        return soulid.release_track_key(n["artist"], n["album"], n["title"])

    @property
    def song_key(self) -> Optional[str]:
        """Track only: the song-level key linking versions across releases."""
        if self.entity != "track":
            return None
        return soulid.song_key(self.norm["artist"], self.norm["title"])

    @property
    def is_cross_node_key(self) -> bool:
        """False for artist keys derived by a library-dependent fallback.

        SoulSync's artist soul_id falls back to the alphabetically-first album in
        the local library, which differs per install — such a key is meaningless
        as a rendezvous point and must not be published or matched on.
        """
        if self.entity != "artist":
            return True
        return self.key_path in soulid.CROSS_NODE_ARTIST_PATHS

    def to_payload(self) -> Dict[str, Any]:
        """The dict that gets canonically encoded and signed. Empty values are
        omitted so 'absent' has exactly one representation."""
        payload: Dict[str, Any] = {
            "v": WIRE_VERSION,
            "entity": self.entity,
            "key": self.key,
            "norm": {k: v for k, v in sorted(self.norm.items()) if v},
            "observed_on": self.observed_on,
        }
        if self.src != SRC_OBSERVED:
            # Omitted when observed, so the common case stays the shortest
            # encoding and claims signed before this field existed still verify.
            payload["src"] = self.src
        if self.entity == "track":
            payload["song_key"] = self.song_key
        if self.key_path:
            payload["key_path"] = self.key_path
        if self.ids:
            payload["ids"] = {
                p: sorted(set(v)) for p, v in sorted(self.ids.items()) if v
            }
        if self.facts:
            payload["facts"] = {k: v for k, v in sorted(self.facts.items()) if v is not None}
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Claim":
        """Rebuild a claim from a received payload. Does NOT validate — call
        ``validate()`` on the payload first, or use ``SignedClaim.verify``."""
        return cls(
            entity=payload["entity"],
            norm=dict(payload.get("norm", {})),
            ids={p: tuple(v) for p, v in payload.get("ids", {}).items()},
            facts=dict(payload.get("facts", {})),
            observed_on=payload.get("observed_on", ""),
            key_path=payload.get("key_path"),
            src=payload.get("src", SRC_OBSERVED),
        )

    def sign(self, identity: NodeIdentity) -> "SignedClaim":
        payload = self.to_payload()
        validate(payload)
        return SignedClaim(
            payload=payload,
            node=identity.node_id,
            sig=identity.sign(_signing_bytes(payload, identity.node_id)),
        )


@dataclass(frozen=True)
class SignedClaim:
    """A claim plus the observer's signature. Relay preserves both verbatim."""

    payload: Mapping[str, Any]
    node: str
    sig: str

    def to_wire(self) -> Dict[str, Any]:
        return {"claim": dict(self.payload), "node": self.node, "sig": self.sig}

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> "SignedClaim":
        missing = {"claim", "node", "sig"} - set(wire)
        if missing:
            raise InvalidClaim(f"wire envelope missing {sorted(missing)}")
        return cls(payload=wire["claim"], node=wire["node"], sig=wire["sig"])

    def verify(self) -> Claim:
        """Check the signature, then the payload, then that the key matches its
        parts. Returns the Claim, or raises InvalidClaim."""
        try:
            public_key_from_id(self.node).verify(
                _sig_bytes(self.sig), _signing_bytes(self.payload, self.node)
            )
        except (InvalidSignature, ValueError) as exc:
            raise InvalidClaim(f"signature does not verify: {exc}") from exc
        validate(self.payload)
        return Claim.from_payload(self.payload)


def _sig_bytes(sig: str) -> bytes:
    from .identity import b64d
    return b64d(sig)


def _signing_bytes(payload: Mapping[str, Any], node: str) -> bytes:
    """Bind the signature to the signer: the node id is inside the signed bytes,
    so a signature cannot be lifted onto another node's envelope."""
    return b"metasync/claim/1\n" + canonical.encode({"claim": payload, "node": node})


def validate(payload: Mapping[str, Any]) -> None:
    """Full structural + derivation check. Raises InvalidClaim on any problem."""
    unknown = set(payload) - _TOP_LEVEL_KEYS
    if unknown:
        raise InvalidClaim(f"unknown top-level keys: {sorted(unknown)}")

    if payload.get("v") != WIRE_VERSION:
        raise InvalidClaim(f"unsupported wire version {payload.get('v')!r}")

    entity = payload.get("entity")
    if entity not in schema.ENTITIES:
        raise InvalidClaim(f"unknown entity {entity!r}")

    observed = payload.get("observed_on", "")
    if not isinstance(observed, str) or not _DATE_RE.match(observed):
        raise InvalidClaim("observed_on must be a YYYY-MM-DD string")

    src = payload.get("src", SRC_OBSERVED)
    if src not in SOURCES:
        raise InvalidClaim(f"unknown src {src!r}")
    if src == SRC_OBSERVED and "src" in payload:
        raise InvalidClaim("src must be omitted for observed claims, not written explicitly")

    _validate_norm(entity, payload.get("norm"))
    _validate_key_path(entity, payload.get("key_path"))
    _validate_ids(entity, payload.get("ids", {}))
    _validate_facts(entity, payload.get("facts", {}))

    # SoulSync emits `soul_unnamed_<library_pk>` for entities with no usable
    # name (soulid_worker.py:263, :580). That value embeds the user's media
    # server primary key and is meaningless off-install — it must never be
    # accepted or relayed, even though it would fail key derivation anyway.
    for field_name in ("key", "song_key"):
        value = payload.get(field_name)
        if isinstance(value, str) and value and not _KEY_RE.match(value):
            raise InvalidClaim(f"{field_name} is not a derived soul id: {value!r}")

    claim = Claim.from_payload(payload)
    if payload.get("key") != claim.key:
        raise InvalidClaim(
            f"key {payload.get('key')!r} does not match its normalized parts "
            f"(expected {claim.key!r})"
        )
    if entity == "track" and payload.get("song_key") != claim.song_key:
        raise InvalidClaim("song_key does not match its normalized parts")
    if entity != "track" and "song_key" in payload:
        raise InvalidClaim("song_key is only valid on track claims")

    # Canonical-encodability is part of validity: if it cannot be re-encoded to
    # the same bytes, no peer can verify the signature.
    canonical.encode(dict(payload))


def _validate_norm(entity: str, norm: Any) -> None:
    if not isinstance(norm, dict):
        raise InvalidClaim("norm must be an object")
    allowed = set(schema.NORM_FIELDS[entity])
    unknown = set(norm) - allowed
    if unknown:
        raise InvalidClaim(f"norm has unknown fields for {entity}: {sorted(unknown)}")
    for required in schema.REQUIRED_NORM[entity]:
        if not norm.get(required):
            raise InvalidClaim(f"norm.{required} is required for {entity} claims")
    for key, value in norm.items():
        if not isinstance(value, str) or len(value) > schema.MAX_NORM_LENGTH:
            raise InvalidClaim(f"norm.{key} must be a string under {schema.MAX_NORM_LENGTH} chars")
        if soulid.normalize_for_soul_id(value) != value:
            raise InvalidClaim(
                f"norm.{key} must already be normalized "
                f"(got {value!r}, expected {soulid.normalize_for_soul_id(value)!r})"
            )


def _validate_key_path(entity: str, key_path: Any) -> None:
    if entity != "artist":
        if key_path is not None:
            raise InvalidClaim("key_path is only valid on artist claims")
        return
    if key_path not in soulid.ARTIST_PATHS:
        raise InvalidClaim(
            f"artist claims must declare key_path as one of {soulid.ARTIST_PATHS}"
        )


def _validate_ids(entity: str, ids: Any) -> None:
    if not isinstance(ids, dict):
        raise InvalidClaim("ids must be an object")
    allowed = schema.ID_PROVIDERS[entity]
    for provider, values in ids.items():
        if provider not in allowed:
            raise InvalidClaim(f"{provider!r} is not a claimable id source for {entity}")
        if not isinstance(values, (list, tuple)) or not values:
            raise InvalidClaim(f"ids.{provider} must be a non-empty array")
        if len(values) > schema.MAX_IDS_PER_PROVIDER:
            raise InvalidClaim(
                f"ids.{provider} has {len(values)} entries, max {schema.MAX_IDS_PER_PROVIDER}"
            )
        if list(values) != sorted(set(values)):
            raise InvalidClaim(f"ids.{provider} must be sorted and free of duplicates")
        for value in values:
            if not isinstance(value, str) or not value or len(value) > schema.MAX_ID_LENGTH:
                raise InvalidClaim(f"ids.{provider} entries must be non-empty strings under {schema.MAX_ID_LENGTH} chars")


def _validate_facts(entity: str, facts: Any) -> None:
    if not isinstance(facts, dict):
        raise InvalidClaim("facts must be an object")
    spec = schema.FACT_FIELDS[entity]
    for name, value in facts.items():
        if name not in spec:
            raise InvalidClaim(f"{name!r} is not a claimable fact for {entity}")
        kind, bound = spec[name]
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidClaim(f"facts.{name} must be an integer")
            low, high = bound
            if not low <= value <= high:
                raise InvalidClaim(f"facts.{name}={value} outside [{low}, {high}]")
        elif kind == "bool":
            if not isinstance(value, bool):
                raise InvalidClaim(f"facts.{name} must be a boolean")
        elif kind == "str":
            if not isinstance(value, str) or len(value) > bound:
                raise InvalidClaim(f"facts.{name} must be a string under {bound} chars")
        elif kind == "date":
            if not isinstance(value, str) or not _DATE_RE.match(value):
                raise InvalidClaim(f"facts.{name} must be YYYY-MM-DD")
        elif kind == "strlist":
            if not isinstance(value, (list, tuple)) or len(value) > bound:
                raise InvalidClaim(f"facts.{name} must be an array of at most {bound} strings")
            if list(value) != sorted(set(value)):
                raise InvalidClaim(f"facts.{name} must be sorted and free of duplicates")
            for item in value:
                if not isinstance(item, str) or not item or len(item) > schema.MAX_GENRE_LENGTH:
                    raise InvalidClaim(f"facts.{name} entries must be non-empty short strings")

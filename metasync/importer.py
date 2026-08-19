"""Turn SoulSync export rows into signed claims.

This module is the provenance boundary, and it is where the network's core
premise is either upheld or quietly broken. Three rules, all enforced here:

* **Only matched ids.** An id is published only when the provider's own
  ``*_match_status`` is ``'matched'``. A NULL status means the enrichment worker
  never ran or never confirmed it; publishing that would be publishing a guess.
* **Never re-export what the network supplied.** ``soul_id`` is the key, never a
  payload id, and no MetaSync/Hydrabase-sourced column is read. What a node
  publishes is what its own enrichment resolved — otherwise corroboration
  degenerates into one node's answer echoed back N times.
* **Keys are derived, never copied.** The claim key is recomputed from the
  current normalized names, so it is verifiable by every receiver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import schema, soulid
from .claim import Claim, InvalidClaim, validate

# (export column -> provider). Mirrors api/serializers.py's allowlists.
ID_COLUMNS: Dict[str, Dict[str, str]] = {
    "artist": {
        "musicbrainz_id": "musicbrainz", "spotify_artist_id": "spotify",
        "itunes_artist_id": "itunes", "deezer_id": "deezer",
        "discogs_id": "discogs", "amazon_id": "amazon", "tidal_id": "tidal",
        "qobuz_id": "qobuz", "audiodb_id": "audiodb", "genius_id": "genius",
        "jiosaavn_id": "jiosaavn",
    },
    "album": {
        "musicbrainz_release_id": "musicbrainz", "spotify_album_id": "spotify",
        "itunes_album_id": "itunes", "deezer_id": "deezer",
        "discogs_id": "discogs", "amazon_id": "amazon", "tidal_id": "tidal",
        "qobuz_id": "qobuz", "audiodb_id": "audiodb", "jiosaavn_id": "jiosaavn",
        "bandcamp_url": "bandcamp", "upc": "upc",
    },
    "track": {
        "musicbrainz_recording_id": "musicbrainz", "spotify_track_id": "spotify",
        "itunes_track_id": "itunes", "deezer_id": "deezer",
        "amazon_id": "amazon", "tidal_id": "tidal", "qobuz_id": "qobuz",
        "audiodb_id": "audiodb", "genius_id": "genius",
        "jiosaavn_id": "jiosaavn", "bandcamp_url": "bandcamp", "isrc": "isrc",
    },
}

# Standards-body identifiers, not the output of a fuzzy match, so no
# match_status governs them — they are published whenever present.
INTRINSIC_IDS = frozenset({"isrc", "upc"})

MATCHED = "matched"

_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


@dataclass
class ImportStats:
    """What the walk did, and — more usefully — what it refused to do."""

    rows: int = 0
    claims: int = 0
    skipped_no_key: int = 0          # no soul_id, or unusable name parts
    skipped_no_ids: int = 0          # nothing survived the match_status gate
    skipped_invalid: int = 0         # failed claim validation
    skipped_artist_unverifiable: int = 0
    stale_stored_key: int = 0        # SoulSync's stored soul_id is out of date
    ids_dropped_unmatched: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        if self.reasons:
            out["reasons"] = dict(sorted(self.reasons.items(), key=lambda kv: -kv[1]))
        return out


def today() -> str:
    """Day precision on purpose: a per-second timestamp on every claim would
    let an observer reconstruct when a user's library was active."""
    return datetime.now(timezone.utc).date().isoformat()


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return str(value).strip() if value not in (None, "") else ""


def _int(value: Any) -> Optional[int]:
    """An integer, rounding a real-valued field rather than discarding it.

    Some columns are REAL in practice even where an integer is implied — `bpm`
    comes back as 87.2. A strict int() would drop every one of them silently.
    Floats never reach the wire (MCJ/1 rejects them); they are rounded here, at
    the import boundary, where the rounding is visible.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return round(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _bounded_int(value: Any, name: str, entity: str) -> Optional[int]:
    """An int, or None if it falls outside the range the schema permits."""
    parsed = _int(value)
    if parsed is None:
        return None
    kind, bound = schema.FACT_FIELDS[entity][name]
    low, high = bound
    return parsed if low <= parsed <= high else None


def _genres(value: Any) -> Optional[List[str]]:
    if not isinstance(value, (list, tuple)):
        return None
    cleaned = sorted({
        str(g).strip() for g in value
        if str(g).strip() and len(str(g).strip()) <= schema.MAX_GENRE_LENGTH
    })
    return cleaned[:schema.MAX_GENRES] or None


def _release_date(value: Any) -> Optional[str]:
    """A bare YYYY-MM-DD, or None. SoulSync stores several shapes here."""
    if not value:
        return None
    match = _ISO_DATE_RE.match(str(value).strip())
    if not match:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return match.group(1)


def collect_ids(row: Mapping[str, Any], entity: str,
                stats: Optional[ImportStats] = None) -> Dict[str, Tuple[str, ...]]:
    """Every id the row carries that this node resolved *itself*.

    An id whose provider's ``*_match_status`` is anything but ``'matched'`` is
    dropped — including NULL, which means the worker never confirmed it. Strict
    on purpose: an unconfirmed id published as an observation is exactly the
    noise the network exists to filter out.
    """
    ids: Dict[str, Tuple[str, ...]] = {}
    for column, provider in ID_COLUMNS[entity].items():
        value = _text(row, column)
        if not value or len(value) > schema.MAX_ID_LENGTH:
            continue
        if provider not in INTRINSIC_IDS:
            if _text(row, f"{provider}_match_status") != MATCHED:
                if stats:
                    stats.ids_dropped_unmatched += 1
                    stats.note(f"unmatched:{provider}")
                continue
        if provider in schema.ID_PROVIDERS[entity]:
            ids[provider] = (value,)
    return ids


def track_claim(row: Mapping[str, Any], stats: ImportStats) -> Optional[Claim]:
    artist = soulid.normalize_for_soul_id(_text(row, "artist_name"))
    album = soulid.normalize_for_soul_id(_text(row, "album_title"))
    title = soulid.normalize_for_soul_id(_text(row, "title"))
    if not (artist and album and title):
        # A track with no album cannot carry a release-level key, and provider
        # track ids are release-specific, so there is nothing to say about it.
        stats.skipped_no_key += 1
        stats.note("track:missing name part")
        return None

    derived = soulid.release_track_key(artist, album, title)
    stored = _text(row, "album_soul_id")
    if stored and stored != derived:
        # SoulSync writes soul_id once and never recomputes it, so a retagged
        # album leaves a stale id behind. The derived key is the one every other
        # node will compute from the same names, so it wins.
        stats.stale_stored_key += 1

    ids = collect_ids(row, "track", stats)
    if not ids:
        stats.skipped_no_ids += 1
        return None

    facts: Dict[str, Any] = {}
    duration_ms = _int(row.get("duration"))
    if duration_ms and duration_ms > 0:
        # SoulSync stores milliseconds; the wire carries seconds, because
        # providers disagree at millisecond granularity and comparing raw ms
        # across nodes would manufacture disagreement out of rounding noise.
        seconds = round(duration_ms / 1000)
        if 0 < seconds <= 86400:
            facts["duration"] = seconds
    for name in ("track_number", "disc_number", "bpm", "year"):
        parsed = _bounded_int(row.get(name), name, "track")
        if parsed is not None:
            facts[name] = parsed
    if isinstance(row.get("explicit"), bool):
        facts["explicit"] = row["explicit"]

    return Claim(
        entity="track",
        norm={"artist": artist, "album": album, "title": title},
        ids=ids,
        facts=facts,
        observed_on=today(),
    )


def album_claim(row: Mapping[str, Any], stats: ImportStats) -> Optional[Claim]:
    artist = soulid.normalize_for_soul_id(_text(row, "artist_name"))
    album = soulid.normalize_for_soul_id(_text(row, "title"))
    if not (artist and album):
        stats.skipped_no_key += 1
        stats.note("album:missing name part")
        return None

    stored = _text(row, "soul_id")
    if stored and stored != soulid.album_key(artist, album):
        stats.stale_stored_key += 1

    ids = collect_ids(row, "album", stats)
    if not ids:
        stats.skipped_no_ids += 1
        return None

    facts: Dict[str, Any] = {}
    for name in ("year", "track_count"):
        parsed = _bounded_int(row.get(name), name, "album")
        if parsed is not None:
            facts[name] = parsed
    release_date = _release_date(row.get("release_date"))
    if release_date:
        facts["release_date"] = release_date
    for name, cap in (("record_type", 32), ("label", 200)):
        value = _text(row, name)
        if value and len(value) <= cap:
            facts[name] = value
    genres = _genres(row.get("genres"))
    if genres:
        facts["genres"] = genres

    return Claim(
        entity="album",
        norm={"artist": artist, "album": album},
        ids=ids,
        facts=facts,
        observed_on=today(),
    )


def recover_artist_qualifier(row: Mapping[str, Any], name: str) -> Optional[str]:
    """Work out which qualifier produced this row's stored artist soul_id.

    SoulSync hashes ``name + max(deezer_id, itunes_id)`` from a live lookup but
    never records the qualifier itself (only, since the soul_id_path column,
    *which kind* it was). The candidates are reconstructable from the row's own
    provider ids, so try each and keep the one that reproduces the stored id.

    Returning None means the key could not be reproduced — the artist's id came
    from a lookup whose result is no longer visible in the row — and the claim
    is dropped rather than published with an unverifiable key.
    """
    stored = _text(row, "soul_id")
    if not stored:
        return None

    deezer = _int(row.get("deezer_id"))
    itunes = _int(row.get("itunes_artist_id"))
    numeric = [value for value in (deezer, itunes) if value is not None]

    candidates: List[str] = []
    if numeric:
        candidates.append(str(max(numeric)))
    candidates.extend(str(value) for value in numeric)

    for candidate in candidates:
        if soulid.artist_key(name, candidate) == stored:
            return candidate
    return None


def artist_claim(row: Mapping[str, Any], stats: ImportStats) -> Optional[Claim]:
    name = soulid.normalize_for_soul_id(_text(row, "name"))
    if not name:
        stats.skipped_no_key += 1
        stats.note("artist:missing name")
        return None

    # `soul_id_path` is a hint, not the gate. SoulSync writes it only when it
    # computes a NEW soul_id, so every artist that predates the column has it
    # NULL — which is most of a real library. The real test is reconstruction:
    # if a qualifier rebuilt from this row's own provider ids reproduces the
    # stored hash, the canonical path provably produced it, whatever the label
    # says. An explicit non-canonical label is still honoured, since that key
    # is known to hash a library-dependent value. See docs/soulid.md.
    declared_path = _text(row, "soul_id_path")
    if declared_path in (soulid.ARTIST_PATH_ALBUM, soulid.ARTIST_PATH_NAME):
        stats.skipped_artist_unverifiable += 1
        stats.note(f"artist:declared non-canonical path ({declared_path})")
        return None

    qualifier = recover_artist_qualifier(row, name)
    if not qualifier:
        stats.skipped_artist_unverifiable += 1
        stats.note("artist:qualifier not recoverable")
        return None

    ids = collect_ids(row, "artist", stats)
    if not ids:
        stats.skipped_no_ids += 1
        return None

    facts: Dict[str, Any] = {}
    genres = _genres(row.get("genres"))
    if genres:
        facts["genres"] = genres

    return Claim(
        entity="artist",
        norm={"artist": name, "qualifier": qualifier},
        ids=ids,
        facts=facts,
        observed_on=today(),
        key_path=soulid.ARTIST_PATH_CANONICAL,
    )


BUILDERS = {"artist": artist_claim, "album": album_claim, "track": track_claim}


def claims_from_rows(rows: Iterable[Mapping[str, Any]], entity: str,
                     stats: Optional[ImportStats] = None) -> Tuple[List[Claim], ImportStats]:
    """Convert export rows to claims, counting every row that does not make it."""
    stats = stats or ImportStats()
    build = BUILDERS[entity]
    claims: List[Claim] = []
    for row in rows:
        stats.rows += 1
        claim = build(row, stats)
        if claim is None:
            continue
        try:
            validate(claim.to_payload())
        except InvalidClaim as exc:
            stats.skipped_invalid += 1
            stats.note(f"invalid:{str(exc)[:60]}")
            continue
        claims.append(claim)
        stats.claims += 1
    return claims, stats

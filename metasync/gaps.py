"""What could MetaSync fill in, if SoulSync asked it?

MetaSync holds id maps, not display metadata — claims carry normalized names
only, so it can never serve artwork or a search result. What it can do is answer
"you are missing this provider's id for this entity, and I hold one."

This module measures how often that is true, by re-walking the export and
comparing each row against the store. It changes nothing; it only counts, so the
value of an integration can be judged before anyone builds one.

At a single node the answer is not zero, because normalization merges editions,
remasters and duplicate library entries onto one key: an id resolved for one
copy of a track can fill the gap on another copy that was never matched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from . import soulid
from .importer import ID_COLUMNS, INTRINSIC_IDS, MATCHED, _text
from .soulsync import SoulSyncClient
from .store import ClaimStore


@dataclass
class GapReport:
    rows: int = 0
    rows_with_a_key: int = 0
    rows_with_a_gap: int = 0
    fillable: Dict[str, int] = field(default_factory=dict)
    already_present: Dict[str, int] = field(default_factory=dict)
    still_missing: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        def ordered(d):
            return dict(sorted(d.items(), key=lambda kv: -kv[1]))
        return {
            "rows": self.rows,
            "rows_with_a_key": self.rows_with_a_key,
            "rows_with_a_gap": self.rows_with_a_gap,
            "fill_rate": round(100 * self.rows_with_a_gap / self.rows_with_a_key, 2)
            if self.rows_with_a_key else 0.0,
            "fillable": ordered(self.fillable),
            "already_present": ordered(self.already_present),
            "still_missing": ordered(self.still_missing),
        }


def row_key(row: Mapping[str, Any], entity: str) -> Optional[str]:
    """The key this row would claim, or None if its names cannot produce one."""
    artist = soulid.normalize_for_soul_id(_text(row, "artist_name") or _text(row, "name"))
    if not artist:
        return None
    if entity == "artist":
        return _text(row, "soul_id") or None
    title = soulid.normalize_for_soul_id(_text(row, "title"))
    if entity == "album":
        return soulid.album_key(artist, title) if title else None
    album = soulid.normalize_for_soul_id(_text(row, "album_title"))
    return soulid.release_track_key(artist, album, title) if (album and title) else None


def present_providers(row: Mapping[str, Any], entity: str) -> set:
    """Providers this row already has a usable id for."""
    have = set()
    for column, provider in ID_COLUMNS[entity].items():
        if not _text(row, column):
            continue
        if provider in INTRINSIC_IDS or _text(row, f"{provider}_match_status") == MATCHED:
            have.add(provider)
    return have


def measure(client: SoulSyncClient, store: ClaimStore, entity: str, limit: int = 500,
            min_observers: int = 1, max_rows: Optional[int] = None) -> GapReport:
    report = GapReport()
    all_providers = set(ID_COLUMNS[entity].values())

    for page in client.export(entity, limit=limit):
        for row in page["rows"]:
            report.rows += 1
            key = row_key(row, entity)
            if not key:
                continue
            report.rows_with_a_key += 1

            have = present_providers(row, entity)
            for provider in have:
                report.already_present[provider] = report.already_present.get(provider, 0) + 1

            known = set(store.resolve(entity, key, min_observers=min_observers))
            fillable = known - have
            for provider in fillable:
                report.fillable[provider] = report.fillable.get(provider, 0) + 1
            if fillable:
                report.rows_with_a_gap += 1
            for provider in all_providers - have - known:
                report.still_missing[provider] = report.still_missing.get(provider, 0) + 1

        if max_rows and report.rows >= max_rows:
            break

    return report

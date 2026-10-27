"""The merge — DESIGN-METADATA.md §8/§9, the step-6 rewrite.

One pure function over ADMITTED claims (own library + answer
cache), grouped by (subject, field, value). The retired compiler-
era library that used to live here expected aggregated inputs
({"accounts", "tier"}) from a design with a server in the middle;
this one consumes the real claim rows the store holds, with their
provenance attached, and produces the MERGED RECORD — the contract
step 8 (the SoulSync depot) and step 9 (the node view) consume.
The shape is versioned (MERGED_RECORD_VERSION) and documented in
DATA.md §19; a consumer keys off "version", never off field
presence.

Input rows (one per claim, built by meta/querier.py):

    {"subject", "kind", "field", "value", "source",
     "origin":      asserting node's pubkey b64url, None = own library
     "batch_id":    the batch the claim arrived in (evidence only)
     "asserted_at": the origin batch's signed time, capped at receipt
     "standing":    §9 standing recorded at admission
     "verdict":     {"tier", "verified_at"} | None  (§7, per claim identity)}

The rules, per §8/§9:

- SUPPORT of a value = the count of DISTINCT asserting origin
  keys with standing, excluding this node's own key (local data
  is not "support", it is local) and excluding network-sourced
  claims (zero support, always, whatever their standing). Re-
  served copies of one origin's claim count once — dedupe is by
  (subject, field, value, origin key), never by batch. A re-
  served copy of THIS node's own batch is a network row like any
  other (R1-SF-2): "local" comes only from the live own-library
  claims (origin None), never from the cache, or a value this
  node once asserted and has since corrected would keep winning
  its field as "local" forever.
- REPORTS of a value = the count of distinct asserting origins
  holding a strong-verified verdict for it. Duration consensus
  is computed over reports, not distinct values (MF-4).
- TIER of a value = the best §7 verdict among its asserting claim
  identities: "strong-verified" > "plausible" > "unverified", with
  verified_at carried from the verdict that earned it. A network-
  sourced value this node strong-verifies itself is strong at
  N=1 (§6) — tier and support are separate axes on purpose.
- SUPPORT LABEL is the only count a user surface may show:
  "strong-verified" (tier strong), "3+ nodes" (support >= 3),
  "1-2 nodes" (support 1-2), "unverified" (support 0). The exact
  count rides in the record for the DEPOT's ranking (§9 ranks the
  3+ bucket top); display surfaces use the label, never the
  number, and never the origin keys (§9).
- LOCAL data always wins its fields: a value the node's own
  library asserts is marked local and named as the field's
  local_value; the merged view never overwrites it, and network
  values sit beside it, visibly not local.
- EXPIRY (§6): an unverified cached claim older than 90 days from
  its origin's asserted_at drops out of the merge entirely.
  Strong-verified cached claims do not expire on the timer (their
  verified_at ages instead: past 90 days the entry is flagged
  verification_stale and reliance waits for a re-check).
- Field rules (§8): provider ids and MBIDs are SETS — every value
  kept, never a winner picked; multiple MBIDs are kept and the
  field is flagged multiple. ISRC is set-valued. Year merges only
  MusicBrainz-sourced values (provider years are local-only until
  the terms check clears; the node's OWN year is its local_value).
  Genres union: every asserted genre is an entry, ranked by tier
  then support; genres are never_auto_fill (advisory, never
  promoted on agreement alone). Durations merge per album_track
  variant context: a variant's consensus is the median of its
  STRONG reports — weighted by report multiplicity, so two
  origins reporting 284000 outvote one reporting 290000
  (MF-4) — with outliers > 2 s from the median dropped; a bare
  consensus exists only when every variant context agrees within
  ±2 s. Tracklists never collapse variants; a variant-less
  tracklist keys by its (disc count, track count) shape, never
  one shared "" group (R2-SF-11), and a variant's fingerprint
  is §7's (disc, position, normalized title) hash.

The merge is deterministic and reputation-free (§9: reputation
may reorder a DISPLAY, never the merge). apply_display_order is
the presentation helper that does that reordering, separately.
"""
from __future__ import annotations

import hashlib
import statistics
from typing import Any

from .soulid import normalize_for_soul_id

MERGED_RECORD_VERSION = 1

TIER_STRONG = "strong-verified"
TIER_PLAUSIBLE = "plausible"
TIER_UNVERIFIED = "unverified"
_TIER_RANK = {TIER_UNVERIFIED: 0, TIER_PLAUSIBLE: 1, TIER_STRONG: 2}

SUPPORT_FLOOR = 3
CACHE_EXPIRY_S = 90 * 24 * 3600
VERIFICATION_STALE_S = 90 * 24 * 3600
DURATION_TOLERANCE_MS = 2000

_SET_FIELDS = (
    "spotify_id", "deezer_id", "tidal_id", "itunes_id", "discogs_id",
    "hydrabase_id", "artist_mbid", "release_group_mbid",
    "recording_mbid", "isrc", "provider_link",
)
_MBID_FIELDS = ("artist_mbid", "release_group_mbid", "recording_mbid")


def support_label(tier: str, support: int) -> str:
    """The §9 bucket — the only form of a count a user may see."""
    if tier == TIER_STRONG:
        return "strong-verified"
    if support >= SUPPORT_FLOOR:
        return "3+ nodes"
    if support >= 1:
        return "1-2 nodes"
    return "unverified"


def value_key(value: Any) -> str:
    """A value's identity string inside the merge — the store's
    canonical form, so merge groups exactly what the store keys."""
    from .store import value_key as _vk

    return _vk(value)


def _row_expired(row: dict[str, Any], now: float) -> bool:
    """§6 cache aging: unverified cached claims expire 90 days
    after their origin's asserted_at. Own claims never expire; a
    strong verdict exempts its claim from the timer."""
    if row.get("origin") is None:
        return False
    asserted_at = row.get("asserted_at")
    if asserted_at is None or asserted_at + CACHE_EXPIRY_S >= now:
        return False
    verdict = row.get("verdict") or {}
    return verdict.get("tier") != TIER_STRONG


def _aggregate(rows: list[dict[str, Any]], own_key: str | None,
               now: float) -> dict[str, Any]:
    """One (field, value) group -> its merged value entry.

    `reports` counts the distinct origins holding a STRONG
    verdict for this value — the multiplicity the duration
    consensus is weighted by (MF-4). `local` is set ONLY by
    live own-library rows (origin None): a re-served copy of
    this node's own batch arriving through the cache carries
    origin == own_key and merges as a network row — visible in
    origins, never local, never support (R1-SF-2)."""
    origins: set[str] = set()
    batches: set[str] = set()
    sources: set[str] = set()
    local = False
    support_origins: set[str] = set()
    report_origins: set[str] = set()
    tier = TIER_UNVERIFIED
    verified_at: float | None = None
    value = rows[0]["value"]
    for row in rows:
        sources.add(row["source"])
        origin = row.get("origin")
        if origin is None:
            local = True
        else:
            origins.add(origin)
            if origin != own_key and row.get("standing") \
                    and row["source"] != "network":
                support_origins.add(origin)
        if row.get("batch_id"):
            batches.add(row["batch_id"])
        verdict = row.get("verdict") or {}
        vtier = verdict.get("tier")
        if vtier == TIER_STRONG and origin is not None:
            report_origins.add(origin)
        if vtier in _TIER_RANK and _TIER_RANK[vtier] > _TIER_RANK[tier]:
            tier = vtier
            verified_at = verdict.get("verified_at")
        elif vtier == tier and tier != TIER_UNVERIFIED:
            vat = verdict.get("verified_at")
            if vat is not None and (verified_at is None or vat > verified_at):
                verified_at = vat
    support = len(support_origins)
    entry: dict[str, Any] = {
        "value": value,
        "tier": tier,
        "support": support,
        "support_label": support_label(tier, support),
        "reports": len(report_origins),
        "local": local,
        "origins": sorted(origins),
        "batches": sorted(batches),
        "sources": sorted(sources),
    }
    if tier != TIER_UNVERIFIED and verified_at is not None:
        entry["verified_at"] = verified_at
        if now - verified_at > VERIFICATION_STALE_S:
            entry["verification_stale"] = True
    return entry


def _entry_sort_key(entry: dict[str, Any]) -> tuple:
    return (-_TIER_RANK[entry["tier"]], -entry["support"],
            value_key(entry["value"]))


def _field_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    local_values = [e["value"] for e in entries if e["local"]]
    return {
        "values": entries,
        "local_value": local_values[0] if len(local_values) == 1 else None,
        "local_values": local_values,
        "disputed": len(entries) > 1,
    }


def _merge_duration(entries_by_context: dict[str, list[dict[str, Any]]],
                    all_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """§8 durations: a consensus per album_track variant context
    (median of the context's STRONG reports, outliers > 2 s from
    the median dropped), and a bare consensus only when every
    context agrees within ±2 s.

    MF-4: the median runs over verified REPORTS — each strong
    entry contributes its value once per distinct origin that
    holds a strong verdict for it — not over distinct values.
    Two origins reporting 284000 and one reporting 290000 are
    three reports, and the consensus is 284000; the old
    distinct-value median called it 287000, a number nobody
    reported."""
    by_variant: dict[str, int] = {}
    for context, entries in entries_by_context.items():
        strong: list[int] = []
        for e in entries:
            if e["tier"] == TIER_STRONG \
                    and isinstance(e["value"], int):
                strong.extend(
                    [e["value"]] * max(1, e.get("reports", 1)))
        if not strong:
            continue
        median = statistics.median(strong)
        kept = [v for v in strong
                if abs(v - median) <= DURATION_TOLERANCE_MS]
        if kept:
            by_variant[context] = round(statistics.median(kept))
    out = _field_entry(all_entries)
    out["by_variant"] = by_variant
    out["consensus_ms"] = None
    if by_variant:
        medians = list(by_variant.values())
        if max(medians) - min(medians) <= DURATION_TOLERANCE_MS:
            out["consensus_ms"] = round(statistics.median(medians))
    return out


def _merge_album_track(groups: dict[str, list[dict[str, Any]]],
                       track_titles: dict[str, str] | None = None,
                       ) -> dict[str, Any]:
    """§8 tracklists: variants never collapse. Within a variant,
    one entry per (disc, position) slot — the strongest, then the
    most-supported, then the lowest track id (deterministic).

    The variant fingerprint is §7's: a hash of the ordered (disc,
    position, NORMALIZED TITLE) sequence — the same form the
    verifier computes over fetched tracklists, so a merged
    variant and a provider tracklist can be compared directly.
    Titles come from the caller (the tracks' own name claims);
    a slot whose title is unknown falls back to its track id,
    which keeps the fingerprint deterministic but not §7-
    comparable for that variant."""
    variants: dict[str, Any] = {}
    for variant, entries in sorted(groups.items()):
        slots: dict[tuple, dict[str, Any]] = {}
        for entry in entries:
            value = entry["value"]
            slot = (value["disc"], value["position"])
            rank = (_TIER_RANK[entry["tier"]], entry["support"])
            current = slots.get(slot)
            if current is None or rank > current[0] or (
                    rank == current[0]
                    and value["track"] < current[1]["value"]["track"]):
                slots[slot] = (rank, entry)
        ordered = [slots[k][1] for k in sorted(slots)]
        titles = track_titles or {}
        sequence = "\n".join(
            f"{e['value']['disc']}:{e['value']['position']}:"
            f"{normalize_for_soul_id(titles.get(e['value']['track'], e['value']['track']))}"
            for e in ordered)
        fingerprint = hashlib.sha256(
            sequence.encode("utf-8")).hexdigest()[:16]
        variants[variant] = {
            "fingerprint": fingerprint,
            "tracks": [
                {"track": e["value"]["track"],
                 "disc": e["value"]["disc"],
                 "position": e["value"]["position"],
                 "tier": e["tier"], "support": e["support"],
                 "local": e["local"]}
                for e in ordered
            ],
            "settled": all(
                e["tier"] == TIER_STRONG or e["support"] >= SUPPORT_FLOOR
                or e["local"] for e in ordered),
        }
    return {"variants": variants, "local_value": None,
            "local_values": [], "disputed": len(variants) > 1}


def merge_subject(subject: str, kind: str, rows: list[dict[str, Any]],
                  *, now: float, own_key: str | None = None,
                  slot_rows: list[dict[str, Any]] = (),
                  track_titles: dict[str, str] | None = None,
                  ) -> dict[str, Any]:
    """Merge every admitted row for one subject into its record.

    `slot_rows` are album_track rows (their subject is an album)
    placing THIS track — the duration merge's variant contexts.
    `track_titles` maps track SoulIDs to their titles (from the
    tracks' own name claims) for the §7 variant fingerprint.
    Rows for other subjects are ignored; expired rows are counted
    and dropped (§6)."""
    live: list[dict[str, Any]] = []
    expired = 0
    for row in rows:
        if row.get("subject") != subject:
            continue
        if _row_expired(row, now):
            expired += 1
            continue
        live.append(row)

    by_field: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in live:
        by_field.setdefault(row["field"], {}).setdefault(
            value_key(row["value"]), []).append(row)

    fields: dict[str, Any] = {}
    name_value: Any = None
    for field, groups in sorted(by_field.items()):
        entries = [_aggregate(group, own_key, now)
                   for group in groups.values()]
        entries.sort(key=_entry_sort_key)
        if field == "album_track":
            fields[field] = _merge_album_track(
                _album_track_groups(groups, own_key, now),
                track_titles)
            continue
        if field == "duration_ms":
            fields[field] = _merge_duration(
                _duration_contexts(entries, slot_rows, own_key), entries)
            continue
        if field == "year":
            merged = _merge_year(entries)
            fields[field] = merged
            continue
        entry = _field_entry(entries)
        if field in _MBID_FIELDS:
            worthy = [e for e in entries
                      if e["tier"] == TIER_STRONG
                      or e["support"] >= SUPPORT_FLOOR or e["local"]]
            entry["multiple"] = len(worthy) > 1
        if field == "genres":
            entry["never_auto_fill"] = True
        fields[field] = entry
        if field == "name":
            local = [e for e in entries if e["local"]]
            name_value = (local[0] if local else entries[0])["value"] \
                if entries else None

    return {
        "version": MERGED_RECORD_VERSION,
        "subject": subject,
        "kind": kind,
        "name": name_value,
        "fields": fields,
        "expired_claims": expired,
    }


def _album_track_groups(groups: dict[str, list[dict[str, Any]]],
                        own_key: str | None, now: float,
                        ) -> dict[str, list[dict[str, Any]]]:
    """album_track value-groups re-keyed by variant: one aggregate
    entry per distinct slot claim value, under its variant.

    Variant-less claims (variant: null) key by the (disc count,
    track count) SHAPE of their asserting origin's slot set —
    key "shape:<discs>x<count>" (R2-SF-11). Two different
    releases whose claims both omit the variant must not collapse
    into one "" group and merge into a Frankenstein tracklist;
    an entry asserted by origins with different shapes appears
    under each, like a duration report in several contexts."""
    slots_by_origin: dict[Any, set] = {}
    discs_by_origin: dict[Any, int] = {}
    for group in groups.values():
        for row in group:
            value = row["value"]
            if isinstance(value.get("variant"), str):
                continue
            origin = row.get("origin")
            slots_by_origin.setdefault(origin, set()).add(
                (value["disc"], value["position"]))
            discs_by_origin[origin] = max(
                discs_by_origin.get(origin, 0),
                int(value["disc"] or 0))
    shape_of = {
        origin: f"shape:{discs_by_origin[origin]}x{len(slots)}"
        for origin, slots in slots_by_origin.items()}
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for group in groups.values():
        entry = _aggregate(group, own_key, now)
        variant = entry["value"].get("variant")
        if isinstance(variant, str):
            by_variant.setdefault(variant, []).append(entry)
            continue
        keys = {shape_of[row.get("origin")] for row in group
                if row.get("origin") in shape_of}
        if not keys:
            keys = {"shape:0x0"}
        for key in sorted(keys):
            by_variant.setdefault(key, []).append(entry)
    return by_variant


def _duration_contexts(entries: list[dict[str, Any]],
                       slot_rows: list[dict[str, Any]],
                       own_key: str | None,
                       ) -> dict[str, list[dict[str, Any]]]:
    """Each duration entry's contexts: the variants its asserting
    origins place the track in (from slot_rows). An origin that
    places the track in several variants contributes its report to
    each — a duration report cannot be split across the releases
    it came with. Entries with no placing origin sit in the bare
    context."""
    variants_by_origin: dict[str | None, set[str]] = {}
    for row in slot_rows:
        value = row.get("value") or {}
        if not isinstance(value, dict):
            continue
        variant = value.get("variant")
        if isinstance(variant, str) and variant:
            variants_by_origin.setdefault(row.get("origin"), set()).add(
                variant)
    contexts: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        keys: set[str] = set()
        if entry["local"]:
            keys |= variants_by_origin.get(None, set())
            keys |= variants_by_origin.get(own_key, set())
        for origin in entry["origins"]:
            keys |= variants_by_origin.get(origin, set())
        if not keys:
            keys = {""}
        for key in keys:
            contexts.setdefault(key, []).append(entry)
    return contexts


def _merge_year(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """§8: the merged year is the MusicBrainz release-group year
    or absent. Non-MusicBrainz network values are held out of the
    merge (listed under "held"); the node's own year, whatever its
    source, is its local_value — local data is local."""
    merged = [e for e in entries
              if "musicbrainz" in e["sources"] or e["local"]]
    held = [e["value"] for e in entries
            if "musicbrainz" not in e["sources"] and not e["local"]]
    out = _field_entry(merged)
    if held:
        out["held"] = held
    return out


# -- the depot projection (step 8's contract) -------------------------------


def support_bucket(support: int) -> str:
    """Support as the depot may see it: a BUCKET, never a count
    (R1-SF-3/R2-SF-9). §9's buckets exactly: "none" (0), "1-2"
    (1-2), "3+" (>= 3 — the support floor bucket). The precise
    count on a sparse item is the fingerprint being hidden, so
    1 and 2 are never distinguishable on this surface."""
    if support <= 0:
        return "none"
    if support <= 2:
        return "1-2"
    return "3+"


def _project_value_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "value": entry["value"],
        "tier": entry["tier"],
        "local": entry["local"],
        "support": support_bucket(entry["support"]),
        "support_label": entry["support_label"],
    }
    if "verified_at" in entry:
        out["verified_at"] = entry["verified_at"]
    if entry.get("verification_stale"):
        out["verification_stale"] = True
        if entry["tier"] == TIER_STRONG:
            # SF-10: a strong verdict older than §6's reliance
            # window is NOT strong on this surface. The depot's
            # promotion floor (a) reads projected tiers, so
            # presenting the stale verdict as strong would let a
            # depot that ignores the flag promote on dead
            # evidence. The internal record keeps the verdict and
            # the flag (a re-check refreshes it); here the tier
            # demotes to unverified and the label follows the
            # demoted tier — the §9 bucket for the count.
            out["tier"] = TIER_UNVERIFIED
            out["support_label"] = support_label(
                TIER_UNVERIFIED, entry["support"])
    return out


def _project_field(entry: dict[str, Any]) -> dict[str, Any]:
    if "variants" in entry:  # album_track
        return {
            "variants": {
                name: {
                    "fingerprint": variant["fingerprint"],
                    "settled": variant["settled"],
                    "tracks": [
                        {"track": t["track"], "disc": t["disc"],
                         "position": t["position"],
                         "tier": t["tier"],
                         "support": support_bucket(t["support"]),
                         "local": t["local"]}
                        for t in variant["tracks"]
                    ],
                }
                for name, variant in entry["variants"].items()
            },
            "local_value": None,
            "local_values": [],
            "disputed": bool(entry.get("disputed")),
        }
    values = [_project_value_entry(e) for e in entry.get("values", [])]
    out: dict[str, Any] = {
        "values": values,
        "local_value": entry.get("local_value"),
        "local_values": entry.get("local_values", []),
        "disputed": bool(entry.get("disputed")),
    }
    # The field's head leads with the LOCAL value when one exists
    # (SF-11): the projection's contract is that value/local name
    # the user's own data first — display order governs the values
    # list beneath, but a strong network value must not pose as
    # the field's head over an unverified local one.
    head = None
    for projected in values:
        if projected["local"]:
            head = projected
            break
    if head is None and values:
        head = values[0]
    out["value"] = head["value"] if head else entry.get("local_value")
    out["tier"] = head["tier"] if head else TIER_UNVERIFIED
    out["local"] = head["local"] if head else bool(
        entry.get("local_value") is not None)
    out["support"] = head["support"] if head else "none"
    # "held" is deliberately NOT projected: it carries non-
    # MusicBrainz NETWORK year values, and §8 says provider years
    # are absent from what SoulSync sees — only local or
    # MusicBrainz-sourced years pass (in `values` above).
    for flag in ("multiple", "never_auto_fill",
                 "consensus_ms", "by_variant"):
        if flag in entry:
            out[flag] = entry[flag]
    return out


def project_for_depot(record: dict[str, Any]) -> dict[str, Any]:
    """The COARSE PROJECTION of a merged record — the contract
    the SoulSync-facing routes serve and step 8's depot consumes
    (R1-SF-3/R2-SF-9).

    Per field: the chosen/local value, its tier, its local flag,
    and support as a BUCKET (support_bucket). What it NEVER
    carries: raw origin keys, batch ids, or exact support counts
    — §9 shows users buckets, and SoulSync is a user surface; the
    exact record (origins, batches, counts, per-value reports)
    stays the node's internal contract for step 9's own view,
    which reads the querier directly, not these routes. Support
    labels ride along (they are the §9 display form). Provider
    ids inside values and the disambiguation settlement are
    public identifiers, not provenance, and stay."""
    out: dict[str, Any] = {
        "version": record.get("version"),
        "subject": record.get("subject"),
        "kind": record.get("kind"),
        "name": record.get("name"),
        "found": bool(record.get("found")),
        "fields": {field: _project_field(entry)
                   for field, entry
                   in record.get("fields", {}).items()},
    }
    block = record.get("disambiguation")
    if block:
        projected = {
            "settled": block.get("settled"),
            "via": block.get("via"),
            "promotion": bool(block.get("promotion")),
            "survivors": block.get("survivors", []),
        }
        if block.get("settled_at") is not None:
            projected["settled_at"] = block["settled_at"]
        out["disambiguation"] = projected
    return out


# -- §8 disambiguation ------------------------------------------------------


def _settles_fetched(row: dict[str, Any]) -> bool:
    """One album_of row settles §8's fetched-link rung (gate r2
    MF-2): the link claim carries a strong-verified verdict AND
    its from_id is an id the USER'S OWN claims assert for that
    album (`own_id_match`, marked by the caller — own claims
    carry no verdicts, so an own link is judged by its presence
    in own claims, never by a verdict row) AND the user's own
    album_of data does not contradict it (`own_contradiction`).
    Anything less — a strong link about a stranger's id, or one
    the user's own data disputes — settles nothing."""
    return (row.get("verdict") or {}).get("tier") == TIER_STRONG \
        and bool(row.get("own_id_match")) \
        and not row.get("own_contradiction")


def disambiguate_artist(candidates: dict[str, list[dict[str, Any]]],
                        *, own_key: str | None = None,
                        mbid_settled: str | None = None,
                        ) -> dict[str, Any]:
    """Same-name-artist disambiguation (§8) over provider_link
    evidence. `candidates` maps a candidate artist key (a provider
    id on the shared provider) to the album_of link rows asserting
    albums belong to it. The ladder:

    1. a shared strong-verified MBID settles it outright
       (mbid_settled, computed by the caller from MBID claims —
       and "shared" means shared with the user's own
       non-`network` data, gate MF-2);
    2. a FETCHED provider link — a link claim carrying a
       strong-verified verdict, i.e. the link itself came out of a
       provider response under §7 — settles it with ONE album, and
       the link must be about the USER's own album (gate MF-2,
       tightened by gate r2): its from_id is a provider id the
       user's OWN claims assert for that album (the caller marks
       such rows `own_id_match` — own claims carry no verdicts,
       so own links are judged by their presence in own claims,
       never by verdict rows), and the user's own album_of data
       must not contradict it (`own_contradiction`: own data
       links the same album, on the same provider, to a different
       artist). A fetched link on a stranger's album — or on a
       stranger's id asserted onto the user's album subject, and
       album subjects are keyed by artist name and title, which a
       namesake's album shares — proves the link exists; it does
       not prove the link is about THIS user's artist;
    3. UNVERIFIED links settle DISPLAY ONLY, and only when one
       candidate is linked from >= 3 distinct local albums (the
       counting rule exists because a bare claim is cheap to
       assert; a fetched link is not). "Local" is the live
       own-library rows (origin None) only — a re-served copy of
       this node's own link batch is a network row, not a local
       assertion (R1-SF-2), so `own_key` no longer widens it.

    Zero or >= 2 survivors at every rung -> no settlement: the
    value is display/depot only, and the survivors are returned."""
    if mbid_settled is not None and mbid_settled in candidates:
        return {"settled": mbid_settled, "via": "shared_mbid",
                "promotion": True, "survivors": [mbid_settled]}
    fetched = [key for key, rows in candidates.items()
               if any(_settles_fetched(r) for r in rows)]
    if len(fetched) == 1:
        return {"settled": fetched[0], "via": "fetched_link",
                "promotion": True, "survivors": fetched}
    if len(fetched) > 1:
        return {"settled": None, "via": None, "promotion": False,
                "survivors": sorted(fetched)}
    local_counts: dict[str, int] = {}
    for key, rows in candidates.items():
        albums = {r["subject"] for r in rows
                  if r.get("origin") is None}
        local_counts[key] = len(albums)
    best = max(local_counts.values(), default=0)
    leaders = [k for k, n in local_counts.items() if n == best]
    if best >= SUPPORT_FLOOR and len(leaders) == 1:
        return {"settled": leaders[0], "via": "local_link_count",
                "promotion": False, "survivors": leaders}
    return {"settled": None, "via": None, "promotion": False,
            "survivors": sorted(candidates)}


def apply_display_order(record: dict[str, Any],
                        reputation_for) -> dict[str, Any]:
    """§9 presentation, with TIER PRECEDENCE preserved
    (R1-SF-5): strong-verified first, then plausible, then
    supported-but-unverified, then the unverified tail — and
    reputation reorders ONLY within that tail, by the best
    reputation of an asserting origin (descending), value
    identity as the tiebreak. Verified and supported values keep
    the merge's deterministic order (the sort keys reproduce
    _entry_sort_key inside their classes) — reputation never
    reorders evidence, and never lifts an unverified value above
    a verified one. The record is adjusted in place and returned
    (it is a view, not the stored merge)."""
    for field_entry in record.get("fields", {}).values():
        values = field_entry.get("values")
        if not values:
            continue

        def rank(entry: dict[str, Any]) -> tuple:
            if entry["tier"] != TIER_UNVERIFIED:
                return (0, -_TIER_RANK[entry["tier"]],
                        -entry["support"], value_key(entry["value"]))
            if entry["support"] > 0:
                return (1, 0, -entry["support"],
                        value_key(entry["value"]))
            best = 0
            for origin in entry["origins"]:
                rep = reputation_for(origin) or {}
                best = max(best, int(rep.get("checks_passed", 0))
                           - int(rep.get("checks_failed", 0)))
            return (2, 0, -best, value_key(entry["value"]))

        values.sort(key=rank)
    return record

"""The enrichment view model — DESIGN-METADATA.md §13 step 9.

A pure presenter over the FULL merged record (meta/merge.py),
read from the querier directly. The presenter READS the whole
record — its one interpretation, the gap classification, is
computed from the record's origin lists — but what it hands
the page for any value is exactly what the coarse depot
projection exposes (merge.project_for_depot's field set), no
more: the value, its tier, its local flag, its sources, its
verdict time and staleness, and support as the §9 BUCKET
alone. §9 binds this page like every user surface (DATA.md
§19: both rules bind step 8/9): a precise count next to a
sparse item fingerprints how many libraries hold it, so
THIS view exposes no exact SUPPORT count of its own —
support crosses as the bucket alone, and origin keys, batch ids,
exact counts and per-value report counts are stripped at
two layers: per value HERE, in _value_view, and at the
route, which builds the template context from these views
plus a fixed set of lookup facts — the service's result
dict (the full records, the own key string, the full
querier status) never crosses into the context at all.
The values are RE-SORTED here too (_display_order): the
merge orders by exact support, and an order is a rendering
too, so the page's order is (tier, bucket, value identity)
— nothing the bucket doesn't already say. The year's held
values get the same discipline in _field_view — value
order alone, the only key the page shows for them.
Scoped honestly, though: what the presenter renders is
a MERGED record, and
a few disclosures are inherited from the merge's own
arithmetic rather than added by this view — which slot
winner a variant shows, the shape variants an entry is
filed under, and the duration consensus, which is a median
over strong REPORTS (so report counts do shape that one
rendered number, for all that per-value report counts are
stripped). They are named as one cluster in DATA.md §20,
ticketed against the merge/projection — fixing them means
bucket-ranked merge, a design decision above this surface.
This view adds no exposure of its own beyond them. And
asserting node keys are never shown to the user, not even
the node's own operator on the node's own page. The status tiles
are shaped HERE too (backlog_view): both backlog counters
cross as PRESENCE only ("none" / "some"), because both
move for reasons correlated with other nodes' claims —
newly admitted claims land in the unverified queue
unverdicted, and asks force-recheck stale verdicts away —
so any finer rendering hands those movements back by
subtraction. The one provenance
mark that survives is a boolean — "this node's own,
re-served" — for a
value whose only non-local assertions are the operator's own
key (their own identity, as a flag, never the key string).
It renders only for the node's own operator, behind the UI's
admin login, from data already in the node's local store;
nothing here is served to SoulSync or re-served to the
network, and viewing never promotes anything into the
library.

DISPLAY-ONLY, stated precisely: this presenter computes
nothing that feeds back into the merge, the store, or the
querier, and the page built on it writes nothing itself.
What a lookup DOES depends on the scope the operator picks
(see NodeService.meta_enrichment): "local" is a pure read of
what the node already holds; "ask" runs the node's normal
ask, which may admit new answers into the cache and schedule
verification — exactly like any ask, and the page says so.
Its one piece of interpretation is the GAP classification —
a field (or value) the network asserts and the user's own
library does not is a gap the enrichment could one day close;
a field the library holds alone is local ground the network
has not touched; a field asserted only by this node's own
re-served batches is this node's, not the network's. The
classification reads only the record's own local flags and
origin lists; showing it never acts on it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .merge import (
    TIER_PLAUSIBLE,
    TIER_STRONG,
    TIER_UNVERIFIED,
    support_bucket,
    support_label,
    value_key,
)

# Tier -> the stylesheet's status-pill modifier (base.html's
# pill vocabulary: ok = green, pending_approval = amber).
TIER_PILL = {
    "strong-verified": "ok",
    "plausible": "pending_approval",
    "unverified": "",
}

# The page's OWN order for a field's values (DATA.md §20,
# gate MF-1): the merge orders values by EXACT support
# (merge._entry_sort_key, kept by apply_display_order inside
# its classes), and an order is a rendering too — of two
# same-tier values inside bucket "1-2", the one shown above
# a value-identity-earlier rival is exactly the 2. So the
# presenter re-sorts by everything the page SHOWS and
# nothing it doesn't: true tier, support BUCKET, value
# identity. The unverified tail (support 0) deliberately
# keeps the record's incoming order — apply_display_order
# ranked it by asserting-origin reputation, §9's designed
# behaviour — so every key there ties and the stable sort
# lets it through untouched.
_TIER_RANK = {TIER_STRONG: 2, TIER_PLAUSIBLE: 1, TIER_UNVERIFIED: 0}
_BUCKET_RANK = {"3+": 2, "1-2": 1, "none": 0}


def _display_order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A field's raw entries in the page's order: (tier
    class, true tier, bucket, value identity), the
    unverified tail in the record's own order. The sort
    reads the TRUE tier on purpose: a stale strong sorts
    with the strongs here while _value_view demotes its
    pill to unverified — the sort is by what the verdict
    IS, the pill by what the page may call it (SF-3); do
    not "align" the two."""

    def key(entry: dict[str, Any]) -> tuple:
        tier = entry.get("tier", TIER_UNVERIFIED)
        support = entry.get("support", 0)
        bucket = _BUCKET_RANK[support_bucket(support)]
        if tier != TIER_UNVERIFIED:
            return (0, -_TIER_RANK.get(tier, 0), -bucket,
                    value_key(entry.get("value")))
        if support > 0:
            return (1, 0, -bucket, value_key(entry.get("value")))
        return (2, 0, 0, "")

    return sorted(entries, key=key)


def format_timestamp(epoch: Any) -> str | None:
    """An epoch float as a UTC stamp, or None for anything that
    is not a number (verdict times in the record are floats;
    anything else is a bug upstream and renders as absent, not
    as a crash)."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def format_duration_ms(ms: Any) -> str | None:
    """Milliseconds as m:ss (h:mm:ss past an hour)."""
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
        return None
    total_s = round(ms / 1000)
    hours, rem = divmod(total_s, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_value(value: Any, field: str | None = None) -> str:
    """One claim value as display text. Shapes follow
    meta/claims.py: name/display_name are name-part lists,
    provider_link is the link dict, album_track is the slot
    dict, everything else is a scalar. Unknown shapes degrade
    to a deterministic compact rendering — never an exception
    (the crowd's strings are data, and this page renders data
    it did not write)."""
    if isinstance(value, list):
        # Name parts: (artist,) / (artist, title) — the title
        # after an em dash reads as the entity's name.
        parts = [str(part) for part in value]
        return " — ".join(parts) if parts else "(empty)"
    if isinstance(value, dict):
        if "relation" in value and "from_id" in value:
            return (f"{value.get('provider')}: {value.get('from_id')} "
                    f"→ {value.get('to_kind')} {value.get('to_id')} "
                    f"· {value.get('relation')}")
        if "track" in value and "position" in value:
            variant = value.get("variant")
            return (f"disc {value.get('disc')} · track "
                    f"{value.get('position')} · {value.get('track')}"
                    + (f" · {variant}" if variant else ""))
        return ", ".join(f"{k}: {format_value(v)}"
                         for k, v in sorted(value.items()))
    if field == "duration_ms":
        pretty = format_duration_ms(value)
        if pretty is not None:
            return f"{value} ms · {pretty}"
    return str(value)


def _display_tier(tier: str, stale: Any) -> str:
    """The tier a user surface may show for a verdict: a
    strong verdict past §6's reliance window demotes to
    unverified (the projection's SF-10 discipline), for
    values and tracklist slots alike. The record keeps the
    true tier; only the display changes."""
    if tier == TIER_STRONG and stale:
        return TIER_UNVERIFIED
    return tier


def _value_view(entry: dict[str, Any], field: str,
                own_key: str | None) -> dict[str, Any]:
    # THE §9 STRIP. The record entry carries the exact support
    # count, the report count, the asserting origin keys and
    # the batch ids; NONE of them crosses into the template
    # context. Support leaves as the bucket alone — the same
    # support_bucket() the depot projection applies — with the
    # §9 support_label riding along, mirroring
    # merge._project_value_entry's exposure exactly. The one
    # provenance fact kept is `own`: a boolean, true when the
    # value's only non-local assertions are this node's OWN key
    # (its batches, re-served back) — the operator's own
    # identity as a flag. No key string, not even our own, is
    # ever placed in the view.
    origins = entry.get("origins", [])
    own = bool(origins) and own_key is not None \
        and all(o == own_key for o in origins)
    tier = entry.get("tier", "unverified")
    label = entry.get("support_label", "unverified")
    if _display_tier(tier, entry.get("verification_stale")) != tier:
        # The projection's discipline (merge._project_value_
        # entry, SF-10): a strong verdict past §6's reliance
        # window is not strong on a user surface — the tier
        # and label demote exactly as the projection demotes
        # them. The stale flag rides along below, so the page
        # still shows the fact beside the demoted label.
        tier = TIER_UNVERIFIED
        label = support_label(TIER_UNVERIFIED,
                              entry.get("support", 0))
    return {
        "display": format_value(entry.get("value"), field),
        "tier": tier,
        "tier_pill": TIER_PILL.get(tier, ""),
        "verified_at": format_timestamp(entry.get("verified_at")),
        "verification_stale": bool(entry.get("verification_stale")),
        "support": support_bucket(entry.get("support", 0)),
        "support_label": label,
        "local": bool(entry.get("local")),
        "own": own,
        "sources": list(entry.get("sources", [])),
    }


def _slot_view(slot: dict[str, Any]) -> dict[str, Any]:
    # Slots demote like values: a stale strong slot is not
    # strong on this page — the demoted tier takes its green
    # pill with it, and the stale flag stays beside it. VIEW
    # ONLY: the depot projection does not demote stale strong
    # slots (merge._project_field serves them as strong), a
    # named known inconsistency (DATA.md §20); nothing here
    # changes what project_for_depot serves.
    tier = _display_tier(slot["tier"],
                         slot.get("verification_stale"))
    return {
        "track": slot["track"], "disc": slot["disc"],
        "position": slot["position"], "tier": tier,
        "tier_pill": TIER_PILL.get(tier, ""),
        # Slot support is bucketed too — the projection
        # buckets it, and so does §9.
        "support": support_bucket(slot["support"]),
        "local": bool(slot["local"]),
        "verification_stale":
            bool(slot.get("verification_stale")),
    }


def _held_sort_key(value: Any) -> tuple:
    """The sort key for held values: the VALUE alone — the
    only key the page shows for them. Numbers sort by the
    number (value_key compares canonical text, under which
    999 would follow 2003); everything else sorts by value
    identity. Readability only: either branch's order
    depends on the value alone and discloses nothing."""
    if isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, value_key(value))


def _field_view(field: str, entry: dict[str, Any],
                own_key: str | None) -> dict[str, Any]:
    ordered = _display_order(entry.get("values", []))
    values = [_value_view(e, field, own_key) for e in ordered]
    # Local values ride the same re-sort (the record's own
    # list carries them in the merge's exact-support order),
    # derived from the re-sorted entries so the field reads
    # one way.
    local_values = [format_value(e.get("value"), field)
                    for e in ordered if e.get("local")]
    # GAP presence, in the merge's own terms. A merged value is
    # LOCAL when a live own-library row asserts it; NETWORK when
    # any origin beyond this node asserts it; OWN when its only
    # non-local assertions are this node's own key — a re-served
    # copy of our own batch is this node's data coming back, not
    # the network holding anything (merge._aggregate: visible in
    # origins, never local, never support). A value with no
    # origins at all and no local row should not exist (entries
    # are built from rows); if one appears it counts as network
    # — the conservative reading of a non-local assertion.
    has_local = bool(local_values)
    has_network = False
    has_own = False
    for entry_v in entry.get("values", []):
        if entry_v.get("local"):
            has_local = True
            continue
        origins = entry_v.get("origins", [])
        if origins and own_key is not None \
                and all(o == own_key for o in origins):
            has_own = True
        else:
            has_network = True
    if "variants" in entry:  # album_track
        # Slot entries carry no origins in the merged record,
        # so the own/network split is not recoverable here —
        # locality is all the slots can say.
        has_local = any(t["local"] for variant
                        in entry["variants"].values()
                        for t in variant["tracks"])
        has_network = any(not t["local"] for variant
                          in entry["variants"].values()
                          for t in variant["tracks"])
    # Held values (year: non-MusicBrainz network years, held
    # out of the merge by §8) are network presence too — a year
    # whose only network story is held must not classify as if
    # the network had nothing. It classifies "held" (held, not
    # served) whenever no SERVED network value merged.
    has_held = bool(entry.get("held"))
    if has_local and has_network:
        gap = "both"
    elif has_network:
        gap = "network"
    elif has_held:
        gap = "held"
    elif has_own and not has_local:
        gap = "own"
    elif has_local:
        gap = "local"
    else:
        gap = "none"
    view: dict[str, Any] = {
        "field": field,
        "disputed": bool(entry.get("disputed")),
        "local_values": local_values,
        "values": values,
        "gap": gap,
        "multiple": bool(entry.get("multiple")),
        "never_auto_fill": bool(entry.get("never_auto_fill")),
    }
    if "held" in entry:  # year: non-MusicBrainz network years
        # Held values render in VALUE order alone
        # (confirmation-4 MF-1): the merge built this list
        # from entries in its exact-support order, and the
        # page shows no tier and no bucket for a held value,
        # so that order would disclose tier/support
        # comparisons nothing else on the page justifies.
        # Value identity is the only key the page shows for
        # them, so it is the only key they may be sorted by
        # (_held_sort_key: the value alone, numbers by the
        # number itself).
        view["held"] = [format_value(v, field) for v in
                        sorted(entry["held"], key=_held_sort_key)]
    if "consensus_ms" in entry:  # duration_ms
        view["consensus"] = format_duration_ms(entry["consensus_ms"]) \
            if entry["consensus_ms"] is not None else None
        view["consensus_ms"] = entry["consensus_ms"]
        view["by_variant"] = {
            name: format_duration_ms(ms)
            for name, ms in sorted(entry.get("by_variant", {}).items())}
    if "variants" in entry:  # album_track
        view["variants"] = [
            {"name": name,
             "fingerprint": variant["fingerprint"],
             "settled": bool(variant["settled"]),
             "tracks": [_slot_view(t) for t in variant["tracks"]]}
            for name, variant in sorted(entry["variants"].items())]
    return view


def record_view(record: dict[str, Any], *,
                own_key: str | None = None) -> dict[str, Any]:
    """One full merged record as the enrichment page renders it:
    header facts, the gap classification, per-field value views
    (each carrying only the depot projection's exposure for its
    value — §9), the §8 disambiguation outcome, and
    the record's own verification tally (how many of its
    values and tracklist slots stand FRESH strong, how many
    stand fresh plausible, how many were never verified, and
    how many verdicts have gone stale — a partition: each
    counted once)."""
    fields = [_field_view(field, entry, own_key)
              for field, entry in record.get("fields", {}).items()]
    # The tally counts every value AND every album_track slot
    # (a record whose only strong verdict is a slot must not
    # headline "0 strong-verified" over a table showing it).
    # It counts from the RECORD's entries, not the views —
    # a stale strong's view tier is demoted for display
    # (above) while the tally still owes its true tier. The
    # four numbers PARTITION what they count: "strong" is the
    # RELIANCE-grade headline (fresh strong verdicts only),
    # and a stale verdict — strong or plausible — counts in
    # "stale" alone, never also in its tier's bucket, so the
    # four add up to the values and slots counted.
    tally = {"strong": 0, "plausible": 0, "unverified": 0, "stale": 0}

    def _count(tier: str, stale: bool) -> None:
        if stale:
            tally["stale"] += 1
        elif tier == TIER_STRONG:
            tally["strong"] += 1
        elif tier == "plausible":
            tally["plausible"] += 1
        else:
            tally["unverified"] += 1

    for entry in record.get("fields", {}).values():
        for value in entry.get("values", []):
            _count(value.get("tier", "unverified"),
                   bool(value.get("verification_stale")))
        # Variant-less slot entries are duplicated under every
        # asserting origin's shape key by the merge (R2-SF-11:
        # "shape:<discs>x<count>") — the same entry winning its
        # slot in two shape variants is ONE entry and counts
        # once. Named variants hold distinct entries (a claim
        # names its variant), so their slots count per variant.
        shape_slots: set[tuple] = set()
        for name, variant in (entry.get("variants") or {}).items():
            for slot in variant.get("tracks", []):
                if name.startswith("shape:"):
                    key = (slot["track"], slot["disc"],
                           slot["position"])
                    if key in shape_slots:
                        continue
                    shape_slots.add(key)
                _count(slot.get("tier", "unverified"),
                       bool(slot.get("verification_stale")))
    block = record.get("disambiguation")
    disambiguation = None
    if block:
        disambiguation = {
            "settled": block.get("settled"),
            "via": block.get("via"),
            "promotion": bool(block.get("promotion")),
            "survivors": list(block.get("survivors", [])),
            "settled_at": format_timestamp(block.get("settled_at")),
        }
    # The header name (DATA.md §20, gate MF-1): never the
    # merge's own pick — merge_subject takes entries[0] in
    # exact-support order, the same disclosure as the value
    # order, acted out on the name field. A LOCAL name wins
    # (the operator's own library is not a network count);
    # otherwise the first name of the re-sorted list. A
    # record with no name FIELD (synthetic ones) falls back
    # to the record's name as recorded.
    name = None
    name_entry = record.get("fields", {}).get("name")
    if name_entry is not None:
        ordered = _display_order(name_entry.get("values", []))
        if ordered:
            chosen = next((e for e in ordered if e.get("local")),
                          ordered[0])
            name = format_value(chosen.get("value"))
    if name is None:
        raw_name = record.get("name")
        name = format_value(raw_name) if raw_name is not None else None
    return {
        "subject": record.get("subject"),
        "kind": record.get("kind"),
        "version": record.get("version"),
        "found": bool(record.get("found")),
        "name": name,
        # A BOOLEAN, never the count: own claims never expire
        # (merge._row_expired), so the record's expired_claims
        # counts exactly the OTHER nodes' claims §6 aging
        # dropped — an exact network-assertion count, which is
        # what §9 buckets hide. Whether any expired is all
        # this page may say (the projection does not carry
        # even that).
        "expired_network_claims": bool(record.get("expired_claims")),
        "fields": fields,
        "gap_fields": {
            "local": [v["field"] for v in fields if v["gap"] == "local"],
            "network": [v["field"] for v in fields
                        if v["gap"] == "network"],
            "both": [v["field"] for v in fields if v["gap"] == "both"],
            "own": [v["field"] for v in fields if v["gap"] == "own"],
            "held": [v["field"] for v in fields if v["gap"] == "held"],
        },
        "disputed_fields": [v["field"] for v in fields if v["disputed"]],
        "verification": tally,
        "disambiguation": disambiguation,
    }


def _presence(count: Any) -> str | None:
    """One backlog counter as PRESENCE: "none" / "some".
    Anything that is not a plain count (missing, mistyped)
    crosses as None — the tile renders empty rather than
    inventing a state."""
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return "none" if count == 0 else "some"


def backlog_view(
        verification: dict[str, Any] | None) -> dict[str, Any] | None:
    """The page's two status tiles, shaped for the template:
    the querier's verification backlog reduced to PRESENCE —
    "none" / "some" — for BOTH tiles. No count crosses, and
    no bucket either: both counters move for reasons
    correlated with OTHER nodes' claims, so any gradation
    finer than presence hands those movements back by
    subtraction between two renders.

    unverified_claims: newly admitted claims land in this
    queue unverdicted — verification runs on a background
    pool and has not finished at render time — so the
    count's movement around an ask is exactly how many new
    origin-and-claim pairs the ask admitted: for one field,
    a support count §9 hides. Even the bucket leaked, at
    its lower boundary: a drained queue ("none"), an ask,
    two values on the record at bucket "1-2" and the queue
    at "1-2" pin each value at exactly one origin.

    stale_verdicts: an earlier fold kept this one exact on
    the claim that admissions never move it. Wrong twice
    over. An ask on a record whose strong verdicts have
    gone stale schedules FORCED re-checks of that record,
    one per origin holding a stale verdict; when the
    re-checks land, those rows refresh (same-or-better) or
    clear (contradiction), and the count drops by exactly
    the number of origins §9 buckets as "1-2" or "3+" —
    the drop separates 1 from 2 and counts the rest. And
    admissions move it from the other side: admitting a
    batch runs prune_answer_cache, and an evicted batch
    takes its verdicts with it. The honest sentence is
    that these counters move for reasons correlated with
    other nodes' claims — which is exactly why the page
    shows presence only. The operator can still see
    whether any backlog exists at all, which is all the
    tiles are for.

    None in (no querier status behind the lookup) means
    None out (no tiles)."""
    if verification is None:
        return None
    return {"unverified_claims": _presence(
                verification.get("unverified_claims")),
            "stale_verdicts": _presence(
                verification.get("stale_verdicts"))}

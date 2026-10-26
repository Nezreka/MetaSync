"""The merge library — DESIGN-METADATA.md §8.

One pure, versioned implementation, run by the compiler worker
(claims -> corpus) and by the node (corpus + fresh claims -> local
view). Same rules, both input shapes.

Input here is the *aggregated* claim: raw claims from many batches,
grouped by (subject, field, value), each carrying

    {"subject", "kind", "field", "value",
     "accounts": <distinct contributor-approved accounts asserting it>,
     "tier": "strong" | "plausible" | "unverified",
     "source": <claim source enum>}

Tiers come from verification (§7): "strong" = cross-matched against
provider responses (both sides from the providers, never the claim);
"plausible" = name+duration match only — treated as unverifiable for
gating, because live/remaster/re-record pairs pass it while being the
wrong item.

Publication gate (§9): a value publishes iff it is strong-verified OR
asserted by >= FLOOR_ACCOUNTS independent approved accounts. What
doesn't publish is returned under "held" — held, not discarded.
"""

import hashlib
import statistics

MERGE_RULES_VERSION = 1
FLOOR_ACCOUNTS = 3

_SET_FIELDS = (
    "spotify_id", "deezer_id", "tidal_id", "itunes_id", "discogs_id",
    "hydrabase_id", "artist_mbid", "release_group_mbid",
    "recording_mbid", "isrc", "provider_link",
)


def _publishable(agg) -> bool:
    return agg["tier"] == "strong" or agg["accounts"] >= FLOOR_ACCOUNTS


def _support(aggs) -> str:
    # The label describes EVERY value it is attached to: a set that
    # mixes one strong-verified value with floor-only values must not
    # wear "strong-verified" — §8's enrich rule lets a strong-verified
    # MBID settle a field outright, so overclaiming the tier here
    # would auto-settle values that only passed the floor.
    if aggs and all(a["tier"] == "strong" for a in aggs):
        return "strong-verified"
    return "3+ accounts"


def _entry(values, aggs, never_auto_fill=False):
    return {
        "values": values,
        "support": _support(aggs),
        "never_auto_fill": never_auto_fill,
    }


_MBID_FIELDS = ("artist_mbid", "release_group_mbid", "recording_mbid")


def _merge_set_field(aggs, field=None):
    """Set-valued fields: every publishable value, never a winner.
    Same-name artists share an artist SoulID and two of their Spotify
    IDs are both true (§8) — picking one would be data loss. Multiple
    published MBIDs are kept AND flagged (§8): never_auto_fill is the
    flag, so enrich never settles on one of several."""
    pub = [a for a in aggs if _publishable(a)]
    held = [a["value"] for a in aggs if not _publishable(a)]
    out = {}
    if pub:
        values = sorted({a["value"] for a in pub})
        out["published"] = _entry(
            values, pub,
            never_auto_fill=field in _MBID_FIELDS and len(values) > 1)
    if held:
        out["held"] = sorted(set(held))
    return out


def _merge_year(aggs):
    """Year is MusicBrainz-only until the terms check clears provider
    years (§7.1/§8), and only strong-verified MB years publish."""
    ok = [a for a in aggs if a["tier"] == "strong"
          and a["source"] == "musicbrainz"]
    if not ok:
        return {"held": sorted({a["value"] for a in aggs})} if aggs else {}
    values = sorted({a["value"] for a in ok})
    return {"published": _entry(values, ok, never_auto_fill=len(values) > 1)}


def _merge_genres(aggs):
    """Genres: one claim per genre string. Publish the floor-passing
    ones ranked by support; counts are published only as the coarse
    bucket inside `support`, never as exact numbers (§9/S-A)."""
    pub = [a for a in aggs if _publishable(a)]
    held = sorted({a["value"] for a in aggs if not _publishable(a)})
    out = {}
    if pub:
        ranked = sorted(pub, key=lambda a: (-a["accounts"], a["value"]))
        out["published"] = {
            "values": [a["value"] for a in ranked],
            "support": _support(pub),
            "never_auto_fill": True,  # genres are advisory, always
        }
    if held:
        out["held"] = held
    return out


def _median_ms(samples) -> int:
    """Median as integer milliseconds: durations are integer ms
    everywhere in the claim/canonical formats, so an even-count
    median (x.5) is rounded rather than leaking a float."""
    return round(statistics.median(samples))


def _merge_duration(aggs):
    """Durations merge per album_track variant context (§8): median of
    publishable reports within a variant, outliers >2s dropped. A bare
    duration publishes only when every variant median agrees ±2s —
    a track SoulID ignores album, so live/remaster/single durations of
    the 'same' track legitimately differ."""
    by_variant = {}
    for a in aggs:
        if _publishable(a):
            by_variant.setdefault(a["value"].get("variant"), []).append(
                a["value"]["ms"])
    variants = {}
    medians = []
    for variant, samples in by_variant.items():
        med = statistics.median(samples)
        kept = [s for s in samples if abs(s - med) <= 2000]
        if not kept:
            # Every report is an outlier against the median (e.g. two
            # reports >4s apart): there is no consensus value for this
            # variant, so it publishes nothing — and must not crash
            # the compile by taking the median of an empty list.
            continue
        med = _median_ms(kept)
        # No "reports" count here: §9 publishes support coarse,
        # never as exact counts.
        variants[str(variant)] = {"ms": med}
        medians.append(med)
    out = {"variants": variants}
    if medians and max(medians) - min(medians) <= 2000:
        out["published"] = {"values": [_median_ms(medians)],
                            "support": _support(
                                [a for a in aggs if _publishable(a)]),
                            "never_auto_fill": False}
    return out


def _merge_album_track(aggs):
    """Tracklists: variants never collapse (§8). A variant publishes
    when strong-verified (its tracklist matched a fetched provider
    release, §7) or floor-supported. Variant fingerprint = hash of
    the ordered (disc, position, track SoulID) sequence; two provider
    releases with the same fingerprint are the same variant (§7)."""
    by_variant = {}
    for a in aggs:
        v = a["value"]
        by_variant.setdefault(v["variant"], []).append(a)
    variants = {}
    held = []
    for variant, group in by_variant.items():
        # one entry per (disc, position): strongest tier, then most
        # accounts, then lowest track SoulID — a deterministic final
        # tiebreak, so the worker and the node merge the same claims
        # to the same tracklist regardless of input order (§8: one
        # library, run everywhere).
        slots = {}
        for a in group:
            key = (a["value"]["disc"], a["value"]["position"])
            rank = (a["tier"] == "strong", a["accounts"])
            if key not in slots or rank > slots[key][0] or (
                    rank == slots[key][0]
                    and a["value"]["track"] < slots[key][1]["value"]["track"]):
                slots[key] = (rank, a)
        ordered = [slots[k][1] for k in sorted(slots)]
        seq = "\n".join(
            f"{a['value']['disc']}:{a['value']['position']}:{a['value']['track']}"
            for a in ordered)
        fingerprint = hashlib.sha256(seq.encode("utf-8")).hexdigest()[:16]
        entry = {
            "fingerprint": fingerprint,
            "tracks": [{"track": a["value"]["track"],
                        "disc": a["value"]["disc"],
                        "position": a["value"]["position"]}
                       for a in ordered],
            "support": _support(ordered),
        }
        # The variant gate is per claim, not per variant (§9): EVERY
        # slot must clear it. Gating on any one slot would publish the
        # below-floor slots riding on a single verified position —
        # and §7 strong-verifies a variant only when its FULL
        # tracklist matches a fetched release.
        if ordered and all(_publishable(a) for a in ordered):
            variants[str(variant)] = entry
        else:
            held.append(str(variant))
    out = {"variants": variants}
    if held:
        out["held_variants"] = held
    return out


def _merge_display_name(aggs):
    """Display names are presentation, not identity (§6): a
    strong-verified MusicBrainz name wins outright; otherwise the
    most-asserted floor-passing form wins. Never used for matching."""
    mb = [a for a in aggs if a["tier"] == "strong"
          and a["source"] == "musicbrainz"]
    if mb:
        # Deterministic on ties (repr of the value): worker and node
        # must pick the same winner from the same claims (§8).
        best = min(mb, key=lambda a: (-a["accounts"], repr(a["value"])))
        return {"published": _entry([best["value"]], mb)}
    pub = [a for a in aggs if _publishable(a)]
    if not pub:
        if not aggs:
            return {}
        held, seen = [], set()
        for a in aggs:  # values are lists here; dedupe by repr
            key = repr(a["value"])
            if key not in seen:
                seen.add(key)
                held.append(a["value"])
        return {"held": held}
    best = min(pub, key=lambda a: (-a["accounts"], repr(a["value"])))
    return {"published": _entry([best["value"]], pub)}


_FIELD_MERGERS = {f: (lambda aggs, _f=f: _merge_set_field(aggs, _f))
                  for f in _SET_FIELDS}
_FIELD_MERGERS.update({
    "year": _merge_year,
    "genres": _merge_genres,
    "duration_ms": _merge_duration,
    "album_track": _merge_album_track,
    "display_name": _merge_display_name,
})


def merge_subject(kind: str, subject: str, agg_claims) -> dict:
    """Merge all aggregated claims for one entity into its corpus entry.

    Name claims are not merged here: a name claim is verified at ingest
    by recomputing the SoulID (§6) and the normalized tuple is identity,
    not a field. Everything else lands under "fields".
    """
    by_field = {}
    for agg in agg_claims:
        if agg["subject"] != subject or agg["field"] == "name":
            continue
        by_field.setdefault(agg["field"], []).append(agg)
    fields = {}
    for field, group in by_field.items():
        merger = _FIELD_MERGERS.get(field)
        if merger is None:
            continue  # unknown field: not merged, not published (§6 enum)
        merged = merger(group)
        if merged:
            fields[field] = merged
    return {
        "subject": subject,
        "kind": kind,
        "merge_rules_version": MERGE_RULES_VERSION,
        "fields": fields,
    }

"""Merge tests (DESIGN-METADATA §8/§9, step 6 rewrite).

The merge consumes admitted claim rows with provenance and emits
the versioned merged record. Support counts distinct standing
ORIGIN keys; tiers come from §7 verdicts; local data wins its
fields; expired unverified cache drops out.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.meta import merge as M
from metasync_node.meta import soulid as S
from metasync_node.meta import verify as V

NOW = 1_800_000_000.0
DAY = 24 * 3600
SUBJ = S.generate_soul_id("track", "Radiohead", "Airbag")
ALBUM = S.generate_soul_id("album", "Radiohead", "OK Computer")
STRONG = {"tier": "strong-verified", "verified_at": NOW - 100}
PLAUSIBLE = {"tier": "plausible", "verified_at": NOW - 100}


def row(field, value, origin="origin-a", source="deezer", standing=True,
        verdict=None, asserted_at=NOW - 100, batch_id="batch-1",
        subject=SUBJ, kind="track"):
    return {"subject": subject, "kind": kind, "field": field,
            "value": value, "source": source, "origin": origin,
            "batch_id": batch_id, "asserted_at": asserted_at,
            "standing": standing, "verdict": verdict}


def own(field, value, source="file_tags", subject=SUBJ, kind="track"):
    return row(field, value, origin=None, source=source, standing=False,
               batch_id=None, subject=subject, kind=kind)


def merged(rows, **kwargs):
    kwargs.setdefault("now", NOW)
    return M.merge_subject(SUBJ, "track", rows, **kwargs)


def values(record, field):
    return record["fields"][field]["values"]


def entry(record, field, value):
    for e in values(record, field):
        if e["value"] == value:
            return e
    raise AssertionError(f"no entry for {field}={value!r}")


class SupportCountingTest(unittest.TestCase):
    def test_distinct_standing_origins_count(self):
        rec = merged([
            row("deezer_id", "123", origin="a"),
            row("deezer_id", "123", origin="b"),
            row("deezer_id", "123", origin="c"),
        ])
        e = entry(rec, "deezer_id", "123")
        self.assertEqual(e["support"], 3)
        self.assertEqual(e["support_label"], "3+ nodes")

    def test_same_origin_twice_counts_once(self):
        # Two batches, one origin (a re-served copy rides a second
        # batch): support dedupes by origin key, never by batch.
        rec = merged([
            row("deezer_id", "123", origin="a", batch_id="b1"),
            row("deezer_id", "123", origin="a", batch_id="b2"),
        ])
        e = entry(rec, "deezer_id", "123")
        self.assertEqual(e["support"], 1)
        self.assertEqual(e["origins"], ["a"])
        self.assertEqual(e["batches"], ["b1", "b2"])

    def test_no_standing_no_support_but_displayed(self):
        # The sub-30-day attestation shape: admission recorded
        # standing=False. The value displays at zero support.
        rec = merged([row("deezer_id", "123", origin="a", standing=False)])
        e = entry(rec, "deezer_id", "123")
        self.assertEqual(e["support"], 0)
        self.assertEqual(e["support_label"], "unverified")
        self.assertEqual(e["tier"], "unverified")

    def test_network_source_never_supports(self):
        rec = merged([
            row("deezer_id", "123", origin="a", source="network"),
            row("deezer_id", "123", origin="b", source="network"),
            row("deezer_id", "123", origin="c", source="network"),
        ])
        e = entry(rec, "deezer_id", "123")
        self.assertEqual(e["support"], 0)
        self.assertEqual(e["support_label"], "unverified")

    def test_network_value_strong_verified_by_this_node(self):
        # §6: a network claim this node strong-verifies itself is
        # trusted at N=1 — tier strong, support still zero.
        rec = merged([row("deezer_id", "123", origin="a",
                          source="network", verdict=STRONG)])
        e = entry(rec, "deezer_id", "123")
        self.assertEqual(e["tier"], "strong-verified")
        self.assertEqual(e["support"], 0)
        self.assertEqual(e["support_label"], "strong-verified")

    def test_own_assertion_is_local_not_support(self):
        rec = merged([own("deezer_id", "123", source="deezer"),
                      row("deezer_id", "123", origin="a")])
        e = entry(rec, "deezer_id", "123")
        self.assertTrue(e["local"])
        self.assertEqual(e["support"], 1)


class LocalWinsTest(unittest.TestCase):
    def test_local_value_marked_and_named(self):
        rec = merged([
            own("label", "XL"),
            row("label", "Parlophone", origin="a"),
            row("label", "Parlophone", origin="b"),
            row("label", "Parlophone", origin="c"),
        ])
        field = rec["fields"]["label"]
        self.assertEqual(field["local_value"], "XL")
        self.assertTrue(entry(rec, "label", "XL")["local"])
        self.assertFalse(entry(rec, "label", "Parlophone")["local"])
        self.assertTrue(field["disputed"])


class SetFieldsTest(unittest.TestCase):
    def test_two_true_ids_both_kept(self):
        artist = S.generate_soul_id("artist", "Nirvana")
        rec = M.merge_subject(artist, "artist", [
            row("spotify_id", "AAA", origin="a", source="spotify",
                subject=artist, kind="artist"),
            row("spotify_id", "BBB", origin="b", source="spotify",
                subject=artist, kind="artist"),
        ], now=NOW)
        self.assertEqual(
            {e["value"] for e in values(rec, "spotify_id")},
            {"AAA", "BBB"})

    def test_multiple_mbids_kept_and_flagged(self):
        rec = merged([
            row("recording_mbid", "mb-1", origin="a",
                source="musicbrainz", verdict=STRONG),
            row("recording_mbid", "mb-2", origin="b",
                source="musicbrainz", verdict=STRONG),
        ])
        field = rec["fields"]["recording_mbid"]
        self.assertEqual(len(field["values"]), 2)
        self.assertTrue(field["multiple"])

    def test_single_mbid_not_flagged(self):
        rec = merged([row("recording_mbid", "mb-1", origin="a",
                          source="musicbrainz", verdict=STRONG)])
        self.assertFalse(rec["fields"]["recording_mbid"]["multiple"])


class YearTest(unittest.TestCase):
    def test_provider_year_held_out_of_merge(self):
        rec = merged([row("year", 1997, origin="a", source="spotify")])
        field = rec["fields"]["year"]
        self.assertEqual(field["values"], [])
        self.assertEqual(field["held"], [1997])

    def test_musicbrainz_year_merges(self):
        rec = merged([row("year", 1997, origin="a",
                          source="musicbrainz")])
        self.assertEqual(
            [e["value"] for e in values(rec, "year")], [1997])

    def test_own_year_is_local_value(self):
        rec = merged([own("year", 1997, source="spotify")])
        self.assertEqual(rec["fields"]["year"]["local_value"], 1997)


class GenresTest(unittest.TestCase):
    def test_union_ranked_and_advisory(self):
        rec = merged([
            row("genres", "art rock", origin="a"),
            row("genres", "art rock", origin="b"),
            row("genres", "alternative rock", origin="a"),
        ])
        field = rec["fields"]["genres"]
        self.assertTrue(field["never_auto_fill"])
        self.assertEqual(
            [e["value"] for e in field["values"]],
            ["art rock", "alternative rock"])
        self.assertEqual(entry(rec, "genres", "art rock")["support"], 2)


class DurationTest(unittest.TestCase):
    def _slots(self, origin, variant, subject=SUBJ):
        return [row("album_track",
                    {"track": subject, "disc": 1, "position": 1,
                     "variant": variant},
                    origin=origin, source="file_tags", subject=ALBUM,
                    kind="album")]

    def test_consensus_from_strong_reports_outliers_dropped(self):
        rows = [
            row("duration_ms", 284000, origin="a", verdict=STRONG),
            row("duration_ms", 284500, origin="b", verdict=STRONG),
            row("duration_ms", 240000, origin="c", verdict=STRONG),
        ]
        rec = merged(rows)
        field = rec["fields"]["duration_ms"]
        self.assertEqual(field["consensus_ms"], 284250)

    def test_unverified_reports_make_no_consensus(self):
        rec = merged([row("duration_ms", 284000, origin="a")])
        self.assertIsNone(rec["fields"]["duration_ms"]["consensus_ms"])

    def test_variant_contexts_disagree_no_bare_consensus(self):
        rows = [
            row("duration_ms", 284000, origin="a", verdict=STRONG),
            row("duration_ms", 200000, origin="b", verdict=STRONG),
        ]
        slots = self._slots("a", "mb:rel-1") + self._slots("b", "mb:rel-2")
        rec = merged(rows, slot_rows=slots)
        field = rec["fields"]["duration_ms"]
        self.assertEqual(field["by_variant"],
                         {"mb:rel-1": 284000, "mb:rel-2": 200000})
        self.assertIsNone(field["consensus_ms"])

    def test_variant_contexts_agree_bare_consensus(self):
        rows = [
            row("duration_ms", 284000, origin="a", verdict=STRONG),
            row("duration_ms", 285000, origin="b", verdict=STRONG),
        ]
        slots = self._slots("a", "mb:rel-1") + self._slots("b", "mb:rel-2")
        rec = merged(rows, slot_rows=slots)
        self.assertEqual(
            rec["fields"]["duration_ms"]["consensus_ms"], 284500)


class AlbumTrackTest(unittest.TestCase):
    def test_variants_never_collapse(self):
        def slot(variant, track, pos):
            return row("album_track",
                       {"track": track, "disc": 1, "position": pos,
                        "variant": variant},
                       origin="a", source="musicbrainz", subject=ALBUM,
                       kind="album")
        other = S.generate_soul_id("track", "Radiohead", "Paranoid Android")
        rec = M.merge_subject(ALBUM, "album", [
            slot("mb:rel-1", SUBJ, 1), slot("mb:rel-1", other, 2),
            slot("mb:rel-2", SUBJ, 1),
        ], now=NOW)
        variants = rec["fields"]["album_track"]["variants"]
        self.assertEqual(sorted(variants), ["mb:rel-1", "mb:rel-2"])
        self.assertEqual(len(variants["mb:rel-1"]["tracks"]), 2)
        self.assertEqual(len(variants["mb:rel-2"]["tracks"]), 1)
        self.assertNotEqual(variants["mb:rel-1"]["fingerprint"],
                            variants["mb:rel-2"]["fingerprint"])


class ExpiryTest(unittest.TestCase):
    def test_unverified_cached_claim_expires_at_90_days(self):
        rec = merged([row("deezer_id", "123", origin="a",
                          asserted_at=NOW - 100 * DAY)])
        self.assertEqual(rec["fields"], {})
        self.assertEqual(rec["expired_claims"], 1)

    def test_strong_verified_claim_survives_the_timer(self):
        rec = merged([row("deezer_id", "123", origin="a",
                          asserted_at=NOW - 100 * DAY, verdict=STRONG)])
        self.assertEqual(len(values(rec, "deezer_id")), 1)

    def test_own_claims_never_expire(self):
        rec = merged([own("deezer_id", "123", source="deezer")])
        self.assertEqual(len(values(rec, "deezer_id")), 1)

    def test_stale_verification_flagged(self):
        stale = {"tier": "strong-verified",
                 "verified_at": NOW - 100 * DAY}
        rec = merged([row("deezer_id", "123", origin="a", verdict=stale)])
        self.assertTrue(entry(rec, "deezer_id", "123")
                        ["verification_stale"])


class WeightedDurationTest(unittest.TestCase):
    """MF-4: the variant consensus is the median over verified
    REPORTS, weighted by how many distinct origins hold a strong
    verdict for each value."""

    def test_consensus_weights_verified_reports(self):
        rows = [
            row("duration_ms", 284000, origin="a", verdict=STRONG),
            row("duration_ms", 284000, origin="b", verdict=STRONG),
            row("duration_ms", 290000, origin="c", verdict=STRONG),
        ]
        rec = merged(rows)
        field = rec["fields"]["duration_ms"]
        # Report-weighted: [284000, 284000, 290000] -> median
        # 284000, the 290000 report dropped as an outlier. The
        # distinct-value median would be 287000 — a value nobody
        # reported — and its outlier pass would keep nothing.
        self.assertEqual(field["consensus_ms"], 284000)
        self.assertEqual(entry(rec, "duration_ms", 284000)
                         ["reports"], 2)
        self.assertEqual(entry(rec, "duration_ms", 290000)
                         ["reports"], 1)


class OwnOriginCacheTest(unittest.TestCase):
    """R1-SF-2: a re-served copy of this node's own batch is a
    network row, not local knowledge."""

    def test_resaved_own_batch_is_not_local(self):
        own_key = "own-key"
        rows = [
            own("deezer_id", "999", source="deezer"),
            row("deezer_id", "555", origin=own_key, standing=True),
        ]
        rec = M.merge_subject(SUBJ, "track", rows, now=NOW,
                              own_key=own_key)
        e = entry(rec, "deezer_id", "555")
        self.assertFalse(e["local"])
        self.assertEqual(e["origins"], [own_key])
        self.assertEqual(e["support"], 0)  # own key never supports
        field = rec["fields"]["deezer_id"]
        self.assertEqual(field["local_value"], "999")

    def test_corrected_value_wins_over_own_resave(self):
        # The node once asserted 284000, corrected its library to
        # 285000, and its OLD batch comes back through the cache:
        # the live library value is the local one.
        own_key = "own-key"
        rows = [
            own("duration_ms", 285000, source="file_tags"),
            row("duration_ms", 284000, origin=own_key),
        ]
        rec = M.merge_subject(SUBJ, "track", rows, now=NOW,
                              own_key=own_key)
        field = rec["fields"]["duration_ms"]
        self.assertEqual(field["local_value"], 285000)


class VariantShapeTest(unittest.TestCase):
    """R2-SF-11: variant-less tracklists key by their (disc
    count, track count) shape; the variant fingerprint is §7's
    (disc, position, normalized title) form."""

    def _slot(self, track, disc, pos, origin="a", variant=None):
        return row("album_track",
                   {"track": track, "disc": disc, "position": pos,
                    "variant": variant},
                   origin=origin, source="file_tags", subject=ALBUM,
                   kind="album")

    def test_variantless_tracklists_key_by_shape(self):
        t1 = S.generate_soul_id("track", "Radiohead", "Airbag")
        t2 = S.generate_soul_id("track", "Radiohead",
                                "Paranoid Android")
        t3 = S.generate_soul_id("track", "Radiohead", "Karma Police")
        rows = [
            # origin a: a 1-disc, 2-track shape
            self._slot(t1, 1, 1, origin="a"),
            self._slot(t2, 1, 2, origin="a"),
            # origin b: a 2-disc, 2-track shape
            self._slot(t1, 1, 1, origin="b"),
            self._slot(t3, 2, 1, origin="b"),
        ]
        rec = M.merge_subject(ALBUM, "album", rows, now=NOW)
        variants = rec["fields"]["album_track"]["variants"]
        self.assertEqual(sorted(variants),
                         ["shape:1x2", "shape:2x2"])
        self.assertNotIn("", variants)
        tracks_1x2 = {t["track"] for t in variants["shape:1x2"]
                      ["tracks"]}
        self.assertEqual(tracks_1x2, {t1, t2})

    def test_variant_fingerprint_is_the_section7_form(self):
        t1 = S.generate_soul_id("track", "Radiohead", "Airbag")
        t2 = S.generate_soul_id("track", "Radiohead",
                                "Paranoid Android")
        rows = [
            self._slot(t1, 1, 1, variant="mb:rel-1"),
            self._slot(t2, 1, 2, variant="mb:rel-1"),
        ]
        rec = M.merge_subject(
            ALBUM, "album", rows, now=NOW,
            track_titles={t1: "Airbag", t2: "Paranoid Android"})
        got = rec["fields"]["album_track"]["variants"]["mb:rel-1"][
            "fingerprint"]
        expected = V.tracklist_fingerprint([
            {"disc": 1, "position": 1, "title": "Airbag"},
            {"disc": 1, "position": 2, "title": "Paranoid Android"},
        ])
        self.assertEqual(got, expected)


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, val in obj.items():
            yield key
            yield from _all_keys(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


class ProjectionTest(unittest.TestCase):
    """R1-SF-3 + R2-SF-9: the SoulSync-facing projection buckets
    support and never carries provenance internals."""

    def test_support_buckets(self):
        rows = [
            own("label", "mine"),
            row("label", "v0", origin="a", standing=False),
            row("label", "v1", origin="b"),
            row("label", "v2", origin="c"), row("label", "v2",
                                               origin="d"),
            row("label", "v3", origin="e"), row("label", "v3",
                                                origin="f"),
            row("label", "v3", origin="g"),
        ]
        rec = merged(rows)
        projected = M.project_for_depot(rec)
        by_value = {e["value"]: e for e in
                    projected["fields"]["label"]["values"]}
        self.assertEqual(by_value["v0"]["support"], "none")
        # §9's buckets exactly (gate MF-3): 1 and 2 are the same
        # bucket — "1-2" — never distinguishable on this surface.
        self.assertEqual(by_value["v1"]["support"], "1-2")
        self.assertEqual(by_value["v2"]["support"], "1-2")
        self.assertEqual(by_value["v3"]["support"], "3+")
        self.assertEqual(
            projected["fields"]["label"]["local_value"], "mine")

    def test_no_provenance_internals_anywhere(self):
        rows = [
            own("deezer_id", "999", source="deezer"),
            row("deezer_id", "123", origin="a", verdict=STRONG),
        ]
        rec = merged(rows)
        rec["disambiguation"] = {
            "settled": "deezer:399", "via": "fetched_link",
            "promotion": True, "survivors": ["deezer:399"],
            "settled_at": NOW - 5}
        projected = M.project_for_depot(rec)
        keys = set(_all_keys(projected))
        self.assertNotIn("origins", keys)
        self.assertNotIn("batches", keys)
        self.assertNotIn("reports", keys)
        for e in projected["fields"]["deezer_id"]["values"]:
            self.assertIsInstance(e["support"], str)
        # The full record keeps the internal contract untouched.
        self.assertIn("origins", rec["fields"]["deezer_id"]
                      ["values"][0])
        # The settlement rides the projection (public ids only).
        self.assertEqual(
            projected["disambiguation"]["settled"], "deezer:399")
        self.assertEqual(
            projected["disambiguation"]["settled_at"], NOW - 5)

    def test_album_track_support_is_bucketed_per_track(self):
        rows = [
            row("album_track",
                {"track": SUBJ, "disc": 1, "position": 1,
                 "variant": "mb:rel-1"},
                origin="a", source="musicbrainz", subject=ALBUM,
                kind="album"),
            row("album_track",
                {"track": SUBJ, "disc": 1, "position": 1,
                 "variant": "mb:rel-1"},
                origin="b", source="musicbrainz", subject=ALBUM,
                kind="album"),
        ]
        rec = M.merge_subject(ALBUM, "album", rows, now=NOW)
        projected = M.project_for_depot(rec)
        track = projected["fields"]["album_track"]["variants"][
            "mb:rel-1"]["tracks"][0]
        self.assertEqual(track["support"], "1-2")
        self.assertEqual(track["track"], SUBJ)


class NameTest(unittest.TestCase):
    def test_name_prefers_local(self):
        rec = merged([
            own("name", ["Radiohead", "Airbag"]),
            row("name", ["Radiohead", "Airbag"], origin="a"),
        ])
        self.assertEqual(rec["name"], ["Radiohead", "Airbag"])
        self.assertEqual(rec["version"], M.MERGED_RECORD_VERSION)


class DisambiguationTest(unittest.TestCase):
    def _link(self, album_subject, origin=None, verdict=None,
              own_id_match=None):
        r = row("provider_link",
                {"provider": "deezer", "from_id": "1",
                 "to_kind": "artist", "to_id": "9",
                 "relation": "album_of"},
                origin=origin, source="deezer", verdict=verdict,
                subject=album_subject, kind="album")
        if own_id_match is not None:
            r["own_id_match"] = own_id_match
        return r

    def test_fetched_link_settles_with_one_album(self):
        album = S.generate_soul_id("album", "X", "Y")
        out = M.disambiguate_artist({
            "artist-1": [self._link(album, verdict=STRONG,
                                     own_id_match=True)],
            "artist-2": [self._link(album)],
        })
        self.assertEqual(out["settled"], "artist-1")
        self.assertEqual(out["via"], "fetched_link")
        self.assertTrue(out["promotion"])

    def test_unverified_links_need_three_local_albums(self):
        albums = [S.generate_soul_id("album", "X", f"Y{i}")
                  for i in range(3)]
        out = M.disambiguate_artist({
            "artist-1": [self._link(a) for a in albums],
            "artist-2": [self._link(albums[0])],
        })
        self.assertEqual(out["settled"], "artist-1")
        self.assertEqual(out["via"], "local_link_count")
        self.assertFalse(out["promotion"])  # display only

    def test_unverified_links_below_three_settle_nothing(self):
        album = S.generate_soul_id("album", "X", "Y")
        out = M.disambiguate_artist({
            "artist-1": [self._link(album)],
            "artist-2": [],
        })
        self.assertIsNone(out["settled"])
        self.assertEqual(out["survivors"], ["artist-1", "artist-2"])

    def test_shared_mbid_settles_outright(self):
        out = M.disambiguate_artist({"artist-1": [], "artist-2": []},
                                    mbid_settled="artist-2")
        self.assertEqual(out["settled"], "artist-2")
        self.assertEqual(out["via"], "shared_mbid")


class DisplayOrderTierTest(unittest.TestCase):
    """R1-SF-5: display order preserves tier precedence — a
    supported-but-unverified value never outranks a verified one,
    whatever the value sort or reputations say."""

    def test_supported_unverified_never_outranks_verified(self):
        rows = [
            row("label", "aaa-supported", origin="s1"),
            row("label", "aaa-supported", origin="s2"),
            row("label", "zzz-verified", origin="v",
                verdict=PLAUSIBLE),
        ]
        rec = merged(rows)
        M.apply_display_order(
            rec, lambda origin: {"checks_passed": 99,
                                 "checks_failed": 0})
        ordered = [e["value"] for e in values(rec, "label")]
        self.assertEqual(ordered[0], "zzz-verified")
        self.assertEqual(ordered[1], "aaa-supported")


class DisplayOrderTest(unittest.TestCase):
    def test_reputation_orders_only_the_unverified_tail(self):
        rec = merged([
            row("label", "A-label", origin="low", standing=False),
            row("label", "B-label", origin="high", standing=False),
            row("label", "C-label", origin="mid", verdict=PLAUSIBLE),
        ])
        reps = {"low": {"checks_passed": 0, "checks_failed": 5},
                "high": {"checks_passed": 9, "checks_failed": 0},
                "mid": {"checks_passed": 0, "checks_failed": 0}}
        M.apply_display_order(rec, reps.get)
        ordered = [e["value"] for e in values(rec, "label")]
        # plausible stays first (merge order for evidence); the two
        # unverified swap so the better-reputed origin leads.
        self.assertEqual(ordered, ["C-label", "B-label", "A-label"])


class StaleStrongProjectionTest(unittest.TestCase):
    """Gate SF-10: the depot projection presents a strong verdict
    older than the 90-day reliance window as UNVERIFIED — the
    internal record keeps the verdict and the stale flag."""

    def test_stale_strong_projects_as_unverified(self):
        stale = {"tier": "strong-verified",
                 "verified_at": NOW - 100 * DAY}
        rec = merged([row("deezer_id", "123", verdict=stale)])
        internal = entry(rec, "deezer_id", "123")
        self.assertEqual(internal["tier"], "strong-verified")
        self.assertTrue(internal["verification_stale"])
        projected = M.project_for_depot(rec)
        e = projected["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["tier"], "unverified")
        self.assertTrue(e["verification_stale"])
        self.assertEqual(e["verified_at"], NOW - 100 * DAY)
        self.assertEqual(
            projected["fields"]["deezer_id"]["tier"], "unverified")

    def test_fresh_strong_projects_as_strong(self):
        rec = merged([row("deezer_id", "123", verdict=STRONG)])
        projected = M.project_for_depot(rec)
        e = projected["fields"]["deezer_id"]["values"][0]
        self.assertEqual(e["tier"], "strong-verified")
        self.assertNotIn("verification_stale", e)


class LocalLeadsProjectionTest(unittest.TestCase):
    """Gate SF-11: when a local value exists, the projection's
    head leads with it regardless of tier ranking."""

    def test_local_value_heads_over_strong_network_value(self):
        rec = merged([
            own("deezer_id", "111"),
            row("deezer_id", "222", origin="a", verdict=STRONG),
        ])
        projected = M.project_for_depot(rec)
        head = projected["fields"]["deezer_id"]
        self.assertEqual(head["value"], "111")
        self.assertTrue(head["local"])
        # The values list beneath keeps both, strong included.
        by_value = {e["value"]: e for e in head["values"]}
        self.assertEqual(by_value["222"]["tier"], "strong-verified")
        self.assertEqual(by_value["111"]["tier"], "unverified")


class HeldYearsProjectionTest(unittest.TestCase):
    """Gate minor: held (non-MusicBrainz network) years never
    reach the projection — only local or MusicBrainz-sourced
    years pass to SoulSync."""

    def test_held_year_absent_from_projection(self):
        rec = merged([
            own("name", ["Radiohead", "Airbag"]),
            row("year", 1997, origin="a", source="deezer"),
            row("year", 1998, origin="b", source="musicbrainz"),
        ])
        # The internal record keeps the held year on record.
        self.assertEqual(rec["fields"]["year"]["held"], [1997])
        projected = M.project_for_depot(rec)
        year = projected["fields"]["year"]
        self.assertNotIn("held", year)
        self.assertEqual(
            [e["value"] for e in year["values"]], [1998])


class FetchedLinkLocalityTest(unittest.TestCase):
    """Gate MF-2 (merge side), tightened by gate r2: the
    fetched-link rung fires only when the link's from_id is a
    provider id the USER'S OWN claims assert for that album
    (`own_id_match`) and the user's own album_of data does not
    contradict the link (`own_contradiction`). A strong verdict
    alone settles nothing — the provider may have asserted the
    link about a stranger's album entirely."""

    def _link(self, album_subject, origin="origin-a", verdict=None,
              own_id_match=None, own_contradiction=None):
        r = row("provider_link",
                {"provider": "deezer", "from_id": "1",
                 "to_kind": "artist", "to_id": "9",
                 "relation": "album_of"},
                origin=origin, source="deezer", verdict=verdict,
                subject=album_subject, kind="album")
        if own_id_match is not None:
            r["own_id_match"] = own_id_match
        if own_contradiction is not None:
            r["own_contradiction"] = own_contradiction
        return r

    def test_fetched_link_on_a_strangers_ids_settles_nothing(self):
        album = S.generate_soul_id("album", "X", "Y")
        out = M.disambiguate_artist({
            "artist-1": [self._link(album, verdict=STRONG)],
            "artist-2": [],
        })
        self.assertIsNone(out["settled"])
        self.assertFalse(out["promotion"])

    def test_fetched_link_on_the_users_own_id_settles(self):
        album = S.generate_soul_id("album", "X", "Y")
        out = M.disambiguate_artist({
            "artist-1": [self._link(album, verdict=STRONG,
                                    own_id_match=True)],
            "artist-2": [],
        })
        self.assertEqual(out["settled"], "artist-1")
        self.assertEqual(out["via"], "fetched_link")
        self.assertTrue(out["promotion"])

    def test_own_contradiction_blocks_the_rung(self):
        # The link IS about one of the user's own ids — but the
        # user's own album_of data links that album to a
        # different artist. The user's data wins the argument:
        # no settlement on this rung.
        album = S.generate_soul_id("album", "X", "Y")
        out = M.disambiguate_artist({
            "artist-1": [self._link(album, verdict=STRONG,
                                    own_id_match=True,
                                    own_contradiction=True)],
            "artist-2": [],
        })
        self.assertIsNone(out["settled"])
        self.assertFalse(out["promotion"])

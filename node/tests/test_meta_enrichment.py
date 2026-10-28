"""Step 9 — the enrichment view (DESIGN-METADATA.md §13).

Three layers, mirroring how the surface is built:

- the PRESENTER (meta/enrichment.py): a pure view-model over
  the full merged record — gap classification off the record's
  own local flags and origin lists, but per value it exposes
  only the coarse depot projection's field set (§9 binds this
  surface too: support as the bucket alone, no origin keys,
  no batch ids, no exact counts, no report counts), verdict
  times formatted, every value shape rendered without an
  exception;
- the SERVICE surface (NodeService.meta_enrichment): FULL
  records — unprojected, where meta_record serves the coarse
  depot projection — under two scopes: "ask" rides the
  querier's existing ask paths (admission + verification
  scheduling, like any ask), "local" is a pure read over
  Querier.record() that schedules nothing, fetches nothing,
  and admits nothing; and store-free on a node whose claims
  store was never created (viewing must not create the file);
- the PAGE (GET /enrichment): login-gated (a GET form — the
  page itself has no write path), every network-sourced
  string escaped on the way out, and no origin key, batch id
  or exact count anywhere in the HTML.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metasync_node.config import Config
from metasync_node.identity import NodeIdentity
from metasync_node.meta import claims as C
from metasync_node.meta import enrichment, merge
from metasync_node.meta import soulid as S
from metasync_node.meta.store import (
    DB_FILENAME as META_DB_FILENAME,
)
from metasync_node.meta.store import value_key
from metasync_node.service import NodeService
from metasync_node.store import Store

from tests.mocks import browser_client
from tests.test_meta_admission import (
    SUBJECT,
    envelope,
    track_claims,
)
from tests.test_meta_builder import ALBUM_ID, FakeSoulSync, full_pages
from tests.test_meta_querier import (
    DAY,
    FakeRelayClient,
    RecordingVerifier,
)


def _value(value, **over):
    entry = {"value": value, "tier": "unverified", "support": 0,
             "support_label": "unverified", "reports": 0,
             "local": False, "origins": [], "batches": [],
             "sources": ["network"]}
    entry.update(over)
    return entry


def _record():
    return {
        "version": 1, "subject": "soul_x", "kind": "album",
        "name": ["Radiohead", "OK Computer"], "found": True,
        "expired_claims": 2,
        "fields": {
            "deezer_id": {
                "values": [
                    _value("3135556", tier="strong-verified", support=4,
                           support_label="3+ nodes", reports=3, local=True,
                           origins=["ownkey", "peerkey"], batches=["b1"],
                           sources=["deezer"], verified_at=1_700_000_000.0),
                    _value("999", support=1, support_label="1-2 nodes",
                           origins=["peerkey"], batches=["b2"])],
                "local_value": "3135556", "local_values": ["3135556"],
                "disputed": True},
            "genres": {
                "values": [_value("alternative", tier="plausible",
                                  support=2, support_label="1-2 nodes",
                                  origins=["peerkey"], batches=["b2"])],
                "local_values": [], "disputed": False,
                "never_auto_fill": True},
            "year": {
                "values": [
                    _value(1997, tier="strong-verified", support=3,
                           support_label="3+ nodes", reports=2, local=True,
                           origins=["ownkey"], batches=["b0"],
                           sources=["musicbrainz"],
                           verified_at=1_700_000_100.0)],
                "local_value": 1997, "local_values": [1997],
                "disputed": False, "held": [2001]},
            "label": {
                "values": [_value("XL", local=True, origins=["ownkey"],
                                  batches=["b0"], sources=["file_tags"])],
                "local_values": ["XL"], "disputed": False},
            "duration_ms": {
                "values": [], "local_values": [], "disputed": False,
                "by_variant": {"standard": 284000},
                "consensus_ms": 284000},
            "album_track": {
                "values": [], "local_values": [], "disputed": False,
                "variants": {"standard": {
                    "fingerprint": "fp1", "settled": True,
                    "tracks": [{"track": "soul_t", "disc": 1,
                                "position": 1, "tier": "strong-verified",
                                "support": 3, "local": True}]}}},
        },
        "disambiguation": {
            "settled": "deezer:3135556", "via": "own_record",
            "promotion": True, "survivors": ["deezer:3135556"],
            "settled_at": 1_700_000_200.0},
    }


class PresenterTest(unittest.TestCase):
    def test_gap_classification_reads_the_local_flags(self):
        view = enrichment.record_view(_record(), own_key="ownkey")
        # album_track's locality lives on its slots, not its
        # (empty) values list — it still classifies as local.
        self.assertEqual(view["gap_fields"]["local"],
                         ["label", "album_track"])
        self.assertEqual(view["gap_fields"]["network"], ["genres"])
        self.assertEqual(view["gap_fields"]["both"], ["deezer_id"])
        # year's local 1997 is real, but its ONLY network
        # presence is the held 2001 — it classifies "held", not
        # "local", so the summary cannot read as if the network
        # had nothing (the local value still renders in the
        # field body).
        self.assertEqual(view["gap_fields"]["held"], ["year"])
        self.assertEqual(view["gap_fields"]["own"], [])
        self.assertEqual(view["disputed_fields"], ["deezer_id"])
        self.assertEqual(view["name"], "Radiohead — OK Computer")
        # Expired claims surface as a BOOLEAN only: own claims
        # never expire, so the record's count is exactly the
        # other nodes' claims §6 dropped — an exact network
        # count, which §9 forbids on a user surface.
        self.assertTrue(view["expired_network_claims"])
        self.assertNotIn("expired_claims", view)
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["album_track"]["gap"], "local")
        self.assertEqual(fields["year"]["gap"], "held")

    def test_values_expose_only_the_projections_provenance(self):
        # §9 binds step 9 (DATA.md §19): the presenter READS
        # the full record (gap classification needs origins)
        # but a value's view carries the depot projection's
        # exposure and nothing more — support as the bucket
        # string, never the exact count; no origin keys, no
        # batch ids, no report counts, on ANY value.
        view = enrichment.record_view(_record(), own_key="ownkey")
        fields = {f["field"]: f for f in view["fields"]}
        for field in view["fields"]:
            for value in field["values"]:
                self.assertNotIn("origins", value)
                self.assertNotIn("batches", value)
                self.assertNotIn("reports", value)
                self.assertIsInstance(value["support"], str)
        strong = fields["deezer_id"]["values"][0]
        self.assertEqual(strong["support"], "3+")  # bucket of 4
        self.assertEqual(strong["support_label"], "3+ nodes")
        self.assertEqual(strong["tier_pill"], "ok")
        self.assertTrue(strong["verified_at"].endswith("UTC"))
        # The own mark survives as a BOOLEAN only: the year
        # value's sole non-local assertion is this node's own
        # key; the strong deezer value mixes own + peer, and
        # the genres value is a peer's alone.
        self.assertTrue(fields["year"]["values"][0]["own"])
        self.assertFalse(strong["own"])
        self.assertFalse(fields["genres"]["values"][0]["own"])
        self.assertTrue(fields["genres"]["never_auto_fill"])
        self.assertEqual(fields["year"]["held"], ["2001"])

    def test_values_render_in_bucket_order_not_exact_support(self):
        # Confirmation-3 MF-1: the merge orders a field's
        # values by EXACT support (apply_display_order keeps
        # _entry_sort_key inside its classes), so two
        # same-tier values in one bucket, shown in that
        # order, disclose the split — the one above a
        # value-identity-earlier rival is the higher count
        # (in "1-2", exactly the 2). The presenter re-sorts
        # by (tier, bucket, value identity), so the order
        # carries nothing the bucket doesn't say. Entries
        # below arrive in exact-support order (higher
        # support first) on purpose.
        record = _record()
        record["fields"] = {
            "deezer_id": {
                "values": [
                    _value("200", tier="plausible", support=2,
                           support_label="1-2 nodes",
                           origins=["p1", "p2"], batches=["b2"]),
                    _value("100", tier="plausible", support=1,
                           support_label="1-2 nodes",
                           origins=["p1"], batches=["b1"])],
                "local_values": [], "disputed": True},
            "spotify_id": {
                "values": [
                    _value("200", tier="plausible", support=4,
                           support_label="3+ nodes",
                           origins=["p1", "p2"], batches=["b2"]),
                    _value("100", tier="plausible", support=3,
                           support_label="3+ nodes",
                           origins=["p1"], batches=["b1"])],
                "local_values": [], "disputed": True},
            "genres": {
                "values": [
                    _value("zzz", origins=["p1"], batches=["b1"]),
                    _value("aaa", origins=["p2"], batches=["b2"])],
                "local_values": [], "disputed": True},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        fields = {f["field"]: f for f in view["fields"]}
        # Same tier, same bucket, exact order inverted
        # against value identity: value identity wins.
        self.assertEqual(
            [v["display"] for v in fields["deezer_id"]["values"]],
            ["100", "200"])
        # "3+" leaks a strict order the same way (4 over 3);
        # the bucket sort flattens it too.
        self.assertEqual(
            [v["display"] for v in fields["spotify_id"]["values"]],
            ["100", "200"])
        # The unverified tail (support 0) is NOT re-sorted
        # into value order: its incoming order is
        # apply_display_order's reputation ranking,
        # §9-designed, and the presenter passes it through.
        self.assertEqual(
            [v["display"] for v in fields["genres"]["values"]],
            ["zzz", "aaa"])

    def test_header_name_is_local_else_first_resorted(self):
        # Confirmation-3 MF-1, the header half: merge_subject
        # picks entries[0] in exact-support order — with no
        # local name, the header would name the 2 over the
        # 1. The presenter takes a LOCAL name when the field
        # has one (the operator's own library is not a
        # network count), else the first name of the
        # re-sorted list.
        record = _record()
        record["name"] = ["Beta"]  # the merge's own pick
        record["fields"] = {
            "name": {
                "values": [
                    _value(["Beta"], support=2,
                           support_label="1-2 nodes",
                           origins=["p1", "p2"], batches=["b2"]),
                    _value(["Alpha"], support=1,
                           support_label="1-2 nodes",
                           origins=["p1"], batches=["b1"])],
                "local_values": [], "disputed": True},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["name"], "Alpha")
        # A local name wins outright, wherever it sorts.
        record["fields"]["name"]["values"].append(
            _value(["Zulu"], local=True, origins=["ownkey"],
                   batches=["b0"], sources=["file_tags"]))
        record["fields"]["name"]["local_values"] = [["Zulu"]]
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["name"], "Zulu")

    def test_view_matches_projection_exposure_for_network_values(self):
        # The contrast, pinned against the projection itself:
        # for a network-only value the view's support IS the
        # projection's support (the bucket), while the view
        # keeps its internal-only analysis the projection
        # never carries (gap classes, tally, disambiguation).
        record = _record()
        view = enrichment.record_view(record, own_key="ownkey")
        projected = merge.project_for_depot(record)
        fields = {f["field"]: f for f in view["fields"]}
        network = fields["deezer_id"]["values"][1]  # "999"
        pnetwork = projected["fields"]["deezer_id"]["values"][1]
        self.assertEqual(network["support"], pnetwork["support"])
        self.assertEqual(network["support"], "1-2")
        self.assertEqual(network["support_label"],
                         pnetwork["support_label"])
        self.assertIn("gap_fields", view)
        self.assertIn("verification", view)
        self.assertIn("disambiguation", view)
        self.assertNotIn("gap_fields", projected)

    def test_verification_tally_and_disambiguation(self):
        view = enrichment.record_view(_record())
        # strong = 3: the two strong values PLUS the strong
        # album_track slot — slots count in the tally.
        self.assertEqual(view["verification"],
                         {"strong": 3, "plausible": 1,
                          "unverified": 2, "stale": 0})
        block = view["disambiguation"]
        self.assertEqual(block["settled"], "deezer:3135556")
        self.assertTrue(block["promotion"])
        self.assertTrue(block["settled_at"].endswith("UTC"))

    def test_own_reserved_only_field_classifies_as_own(self):
        # A field whose only non-local assertions are this
        # node's OWN key (its batch, re-served back) is this
        # node's — the summary must not claim the network
        # holds what only we do.
        record = _record()
        record["fields"] = {
            "label": {
                "values": [_value("XL", origins=["ownkey"],
                                  batches=["b0"],
                                  sources=["file_tags"])],
                "local_values": [], "disputed": False},
            "genres": {
                "values": [_value("alternative", origins=["peerkey"],
                                  batches=["b2"])],
                "local_values": [], "disputed": False},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["gap_fields"]["own"], ["label"])
        self.assertEqual(view["gap_fields"]["network"], ["genres"])
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["label"]["gap"], "own")
        # Without knowing our own key the same rows are just
        # the network's — the split needs the key.
        blind = enrichment.record_view(record)
        self.assertEqual(blind["gap_fields"]["network"],
                         ["label", "genres"])

    def test_held_only_year_classifies_as_held(self):
        # No local year, no merged (MusicBrainz) year — only
        # §8-held network values. Not "none": the network DID
        # assert years; they are held, not served.
        record = _record()
        record["fields"] = {
            "year": {"values": [], "local_value": None,
                     "local_values": [], "disputed": False,
                     "held": [2001, 2003]},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["year"]["gap"], "held")
        self.assertEqual(view["gap_fields"]["held"], ["year"])
        self.assertEqual(fields["year"]["held"], ["2001", "2003"])

    def test_held_years_render_in_value_order_not_merge_order(self):
        # Confirmation-4 MF-1: the merge builds `held` from
        # entries in its exact-support order (apply_display_
        # order never re-sorts it), and the page shows NO
        # tier and NO bucket for a held year — so that order
        # discloses tier/support comparisons, across buckets
        # too. The presenter sorts held by value identity
        # alone. The years below arrive in the opposite of
        # value order, as the merge hands them over when
        # 2003 outranks 1997; they must render in value
        # order. (The classification test above passes
        # [2001, 2003], already in value order, and cannot
        # catch this.)
        record = _record()
        record["fields"] = {
            "year": {"values": [], "local_value": None,
                     "local_values": [], "disputed": False,
                     "held": [2003, 1997]},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["year"]["gap"], "held")
        self.assertEqual(fields["year"]["held"], ["1997", "2003"])

    def test_held_years_sort_numbers_numerically(self):
        # Confirmation-5 SF-2 (cosmetic): value identity
        # compares canonical text, under which 999 follows
        # 2003. Held numbers sort by the number itself —
        # readability only; the order still depends on the
        # value alone and discloses nothing.
        record = _record()
        record["fields"] = {
            "year": {"values": [], "local_value": None,
                     "local_values": [], "disputed": False,
                     "held": [2003, 999]},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["year"]["held"], ["999", "2003"])

    def test_tally_headline_counts_fresh_strong_only(self):
        # 2 fresh strong values + 1 fresh strong slot headline;
        # 3 stale strong values count as stale, never in the
        # headline (the brief's shape: "5 strong-verified ·
        # 5 stale" with reliance-grade zero must not recur).
        record = _record()
        record["fields"] = {
            "deezer_id": {
                "values": [
                    _value("1", tier="strong-verified"),
                    _value("2", tier="strong-verified"),
                    _value("3", tier="strong-verified",
                           verification_stale=True),
                    _value("4", tier="strong-verified",
                           verification_stale=True),
                    _value("5", tier="strong-verified",
                           verification_stale=True)],
                "local_values": [], "disputed": True},
            "album_track": {
                "values": [], "local_values": [], "disputed": False,
                "variants": {"standard": {
                    "fingerprint": "fp1", "settled": True,
                    "tracks": [{"track": "soul_t", "disc": 1,
                                "position": 1,
                                "tier": "strong-verified",
                                "support": 3, "local": False}]}}},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["verification"],
                         {"strong": 3, "plausible": 0,
                          "unverified": 0, "stale": 3})

    def test_no_expired_claims_no_boolean(self):
        record = _record()
        record["expired_claims"] = 0
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertFalse(view["expired_network_claims"])

    def test_stale_plausible_counts_once_in_stale(self):
        # The tally is a partition: a stale plausible counts
        # in "stale" alone — not also in "plausible" — so the
        # four numbers add up to the values and slots counted
        # (a stale strong was already stale-only; the two must
        # not be treated differently).
        record = _record()
        record["fields"] = {
            "genres": {
                "values": [
                    _value("a", tier="plausible",
                           verification_stale=True),
                    _value("b", tier="plausible")],
                "local_values": [], "disputed": True},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["verification"],
                         {"strong": 0, "plausible": 1,
                          "unverified": 0, "stale": 1})

    def test_stale_strong_value_wears_the_projections_label(self):
        # A stale strong shows what the depot projection
        # shows for it (SF-10 demotion: tier unverified, the
        # demoted support label) — never "strong-verified"
        # beside the stale pill. The stale flag itself stays.
        record = _record()
        stale = _value("3135556", tier="strong-verified", support=4,
                       support_label="strong-verified", reports=3,
                       origins=["peerkey"], batches=["b1"],
                       sources=["deezer"],
                       verified_at=1_700_000_000.0,
                       verification_stale=True)
        record["fields"] = {
            "deezer_id": {"values": [stale],
                          "local_value": None, "local_values": [],
                          "disputed": False},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        shown = view["fields"][0]["values"][0]
        projected = merge.project_for_depot(record)
        pshown = projected["fields"]["deezer_id"]["values"][0]
        self.assertEqual(shown["tier"], pshown["tier"])
        self.assertEqual(shown["tier"], "unverified")
        self.assertEqual(shown["support_label"],
                         pshown["support_label"])
        self.assertEqual(shown["tier_pill"], "")
        self.assertTrue(shown["verification_stale"])
        # ...while the tally still owes the verdict's true
        # tier: stale, counted once — it is not an unverified.
        self.assertEqual(view["verification"],
                         {"strong": 0, "plausible": 0,
                          "unverified": 0, "stale": 1})

    def test_stale_strong_slot_counts_as_stale(self):
        # §6 staleness rides the merged slot dict now, and the
        # tally reads it: a stale strong slot lands in "stale",
        # not in the strong headline (the slot form of the
        # failure the fresh-only values fix closed).
        record = _record()
        record["fields"] = {
            "album_track": {
                "values": [], "local_values": [], "disputed": False,
                "variants": {"standard": {
                    "fingerprint": "fp1", "settled": True,
                    "tracks": [{"track": "soul_t", "disc": 1,
                                "position": 1,
                                "tier": "strong-verified",
                                "support": 3, "local": False,
                                "verification_stale": True}]}}},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["verification"],
                         {"strong": 0, "plausible": 0,
                          "unverified": 0, "stale": 1})
        slot = view["fields"][0]["variants"][0]["tracks"][0]
        self.assertTrue(slot["verification_stale"])
        # ...and the slot's DISPLAYED tier demotes with the
        # flag (confirmation SF-1): no green "strong-verified"
        # pill beside the stale pill — the page must not
        # contradict itself the way the value path no longer
        # does.
        self.assertEqual(slot["tier"], "unverified")
        self.assertEqual(slot["tier_pill"], "")

    def test_shape_duplicated_slot_counts_once(self):
        # A variant-less slot entry is duplicated by the merge
        # under every asserting origin's shape key — the same
        # entry winning its slot in two shape variants is ONE
        # entry in the tally. A NAMED variant's slot is a
        # distinct entry (its claim names the variant) and
        # counts separately.
        slot = {"track": "soul_t", "disc": 1, "position": 1,
                "tier": "strong-verified", "support": 3,
                "local": False}
        record = _record()
        record["fields"] = {
            "album_track": {
                "values": [], "local_values": [], "disputed": True,
                "variants": {
                    "shape:1x2": {"fingerprint": "f1",
                                  "settled": False,
                                  "tracks": [dict(slot)]},
                    "shape:1x3": {"fingerprint": "f2",
                                  "settled": False,
                                  "tracks": [dict(slot)]},
                    "mb:rel-1": {"fingerprint": "f3",
                                 "settled": True,
                                 "tracks": [dict(slot)]}}},
        }
        view = enrichment.record_view(record, own_key="ownkey")
        self.assertEqual(view["verification"],
                         {"strong": 2, "plausible": 0,
                          "unverified": 0, "stale": 0})

    def test_duration_and_tracklist_views(self):
        view = enrichment.record_view(_record())
        fields = {f["field"]: f for f in view["fields"]}
        self.assertEqual(fields["duration_ms"]["consensus"], "4:44")
        self.assertEqual(fields["duration_ms"]["by_variant"],
                         {"standard": "4:44"})
        variant = fields["album_track"]["variants"][0]
        self.assertTrue(variant["settled"])
        self.assertEqual(variant["tracks"][0]["tier_pill"], "ok")
        # Slot support is the §9 bucket too, never the count.
        self.assertEqual(variant["tracks"][0]["support"], "3+")

    def test_format_value_shapes(self):
        self.assertEqual(
            enrichment.format_value(["Radiohead", "Airbag"]),
            "Radiohead — Airbag")
        self.assertEqual(
            enrichment.format_value(
                {"provider": "deezer", "from_id": "1", "to_kind": "album",
                 "to_id": "2", "relation": "same_entity"}),
            "deezer: 1 → album 2 · same_entity")
        self.assertEqual(
            enrichment.format_value(
                {"track": "soul_t", "disc": 2, "position": 3,
                 "variant": None}),
            "disc 2 · track 3 · soul_t")
        self.assertEqual(
            enrichment.format_value(284000, "duration_ms"),
            "284000 ms · 4:44")
        self.assertEqual(enrichment.format_value(1997, "year"), "1997")

    def test_format_helpers_refuse_non_numbers(self):
        self.assertIsNone(enrichment.format_timestamp("soon"))
        self.assertIsNone(enrichment.format_timestamp(None))
        self.assertIsNone(enrichment.format_timestamp(True))
        self.assertIsNone(enrichment.format_duration_ms(-1))
        self.assertIsNone(enrichment.format_duration_ms("4:44"))
        self.assertEqual(enrichment.format_duration_ms(59_999), "1:00")
        self.assertEqual(enrichment.format_duration_ms(3_661_000),
                         "1:01:01")


class StaleSlotMergeTest(unittest.TestCase):
    """The merge carries §6 staleness through the album_track
    slot dicts (gate MF-3), so the tally above can read it —
    and the depot projection, which builds its slot dicts key
    by key, does not pick the flag up."""

    NOW = 1_800_000_000.0
    DAY = 24 * 3600

    def _record_with_slot(self, verified_at):
        album = "soul_album_stale_slot"
        track = "soul_track_stale_slot"
        row = {"subject": album, "kind": "album",
               "field": "album_track",
               "value": {"track": track, "disc": 1, "position": 1,
                         "variant": "mb:rel-1"},
               "source": "musicbrainz", "origin": "peer",
               "batch_id": "batch-1",
               "asserted_at": self.NOW - 100, "standing": True,
               "verdict": {"tier": "strong-verified",
                           "verified_at": verified_at}}
        return merge.merge_subject(album, "album", [row],
                                   now=self.NOW)

    def test_stale_strong_slot_carries_the_flag(self):
        record = self._record_with_slot(self.NOW - 100 * self.DAY)
        slot = record["fields"]["album_track"]["variants"][
            "mb:rel-1"]["tracks"][0]
        self.assertEqual(slot["tier"], "strong-verified")
        self.assertTrue(slot["verification_stale"])
        # End to end: the presenter's tally counts it stale.
        view = enrichment.record_view(record)
        self.assertEqual(view["verification"]["stale"], 1)
        self.assertEqual(view["verification"]["strong"], 0)

    def test_fresh_strong_slot_carries_no_flag(self):
        record = self._record_with_slot(self.NOW - 100)
        slot = record["fields"]["album_track"]["variants"][
            "mb:rel-1"]["tracks"][0]
        self.assertNotIn("verification_stale", slot)

    def test_projection_slot_surface_unchanged(self):
        record = self._record_with_slot(self.NOW - 100 * self.DAY)
        projected = merge.project_for_depot(record)
        slot = projected["fields"]["album_track"]["variants"][
            "mb:rel-1"]["tracks"][0]
        self.assertNotIn("verification_stale", slot)
        self.assertEqual(slot["tier"], "strong-verified")

    def test_view_demotes_the_slot_the_projection_keeps(self):
        # Confirmation SF-1, the two surfaces side by side on
        # ONE record: the VIEW demotes the stale strong slot
        # (tier + pill, stale flag kept), the depot
        # PROJECTION serves the same slot as strong-verified
        # — unchanged by this fold. The asymmetry is a named
        # known inconsistency (DATA.md §20): whether the
        # depot demotes slots is a depot decision, not the
        # view's to take.
        record = self._record_with_slot(self.NOW - 100 * self.DAY)
        view = enrichment.record_view(record)
        slot = view["fields"][0]["variants"][0]["tracks"][0]
        self.assertEqual(slot["tier"], "unverified")
        self.assertEqual(slot["tier_pill"], "")
        self.assertTrue(slot["verification_stale"])
        projected = merge.project_for_depot(record)
        pslot = projected["fields"]["album_track"]["variants"][
            "mb:rel-1"]["tracks"][0]
        self.assertEqual(pslot["tier"], "strong-verified")


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.identity = NodeIdentity(key="ef" * 32)
        self.service = NodeService(
            Config(state_dir=self.tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=self.store, identity=self.identity,
            soulsync=FakeSoulSync(full_pages()), env={})


class ServiceEnrichmentTest(ServiceCase):
    def test_no_store_node_gets_the_empty_marker_store_free(self):
        out = self.service.meta_enrichment(
            {"kind": "album", "mode": "exact", "query": ALBUM_ID})
        self.assertTrue(out["ok"])
        self.assertFalse(out["store_exists"])
        self.assertEqual(out["records"], [])
        # Viewing created nothing.
        self.assertFalse(
            (Path(self.tmp.name) / META_DB_FILENAME).exists())

    def test_bad_lookups_are_refused(self):
        self.service.rebuild_meta_store(self.service.clock())
        for payload in (["not", "a", "dict"],
                        {"kind": "nope", "mode": "exact", "query": "x"},
                        {"kind": "album", "mode": "exact", "query": "  "},
                        {"kind": "album", "mode": "sideways",
                         "query": ALBUM_ID}):
            out = self.service.meta_enrichment(payload)
            self.assertFalse(out["ok"], payload)

    def test_full_record_where_meta_record_projects(self):
        self.service.rebuild_meta_store(self.service.clock())
        out = self.service.meta_enrichment(
            {"kind": "album", "mode": "exact",
             "query": "deezer:3135556", "network": False})
        self.assertTrue(out["ok"])
        self.assertTrue(out["store_exists"])
        self.assertFalse(out["asked_network"])
        self.assertEqual(out["own_key"],
                         self.identity.public_key_b64url)
        record = out["records"][0]
        self.assertEqual(record["subject"], ALBUM_ID)
        text = json.dumps(record)
        # The full record: provenance internals ride along.
        self.assertIn('"origins"', text)
        self.assertIn('"batches"', text)
        entry = record["fields"]["deezer_id"]["values"][0]
        self.assertIsInstance(entry["support"], int)
        # The SoulSync-facing sibling serves the projection of
        # the same record: buckets, no internals.
        projected = self.service.meta_record("deezer:3135556")
        ptext = json.dumps(projected)
        self.assertNotIn('"origins"', ptext)
        self.assertNotIn('"batches"', ptext)
        pentry = projected["fields"]["deezer_id"]["values"][0]
        self.assertEqual(pentry["support"], "none")

    def test_network_claims_carry_origins_in_the_service_record(self):
        # The SERVICE surface is the internal contract — the
        # full record, origins included; stripping happens in
        # the presenter and at the page's route, not here.
        self.service.rebuild_meta_store(self.service.clock())
        origin = NodeIdentity(key="aa" * 32)
        self.service._meta_querier_obj().admit(
            envelope(origin, claims=track_claims()))
        out = self.service.meta_enrichment(
            {"kind": "track", "mode": "exact", "query": SUBJECT,
             "network": False})
        self.assertTrue(out["ok"])
        self.assertFalse(out["asked_network"])
        record = out["records"][0]
        values = record["fields"]["deezer_id"]["values"]
        network = [v for v in values if v["value"] == "12345"]
        self.assertEqual(len(network), 1)
        self.assertEqual(network[0]["origins"],
                         [origin.public_key_b64url])
        self.assertFalse(network[0]["local"])

    def test_text_lookup_resolves_the_local_library(self):
        self.service.rebuild_meta_store(self.service.clock())
        out = self.service.meta_enrichment(
            {"kind": "album", "mode": "text", "query": "OK Computer",
             "network": False})
        self.assertTrue(out["ok"])
        self.assertEqual(out["records"][0]["subject"], ALBUM_ID)
        self.assertIn("cached_claims", out["querier"])


class LocalScopePurityTest(ServiceCase):
    """Scope "local" is a PURE READ (fold R1-MF-2/R2-SF-1):
    the ask paths cannot keep that promise — query_exact
    schedules verification even with network=False, and
    query_text fires §8 sanity triggers — so the scope rides
    Querier.record() instead. Pinned here with the querier
    instrumented: a recording verifier standing where the
    fetcher-backed one sits, and a counting relay client."""

    def setUp(self):
        super().setUp()
        self.service.rebuild_meta_store(self.service.clock())
        # Unverdicted cached claims on SUBJECT: an exact ASK
        # would schedule their first verification.
        self.origin = NodeIdentity(key="aa" * 32)
        querier = self.service._meta_querier_obj()
        querier.admit(envelope(self.origin, claims=track_claims()))
        self.verifier = RecordingVerifier()
        querier.verifier = self.verifier
        querier.verify_inline = True
        self.relay = FakeRelayClient()
        querier.relay_provider = lambda: self.relay
        self.batches_before = querier.store.counts()["cached_batches"]

    def _assert_pure(self, out):
        self.assertTrue(out["ok"])
        self.assertFalse(out["asked_network"])
        # No verification scheduled (verify_inline makes the
        # ask paths call the verifier synchronously — silence
        # here means no scheduling AND no fetcher entry), no
        # sanity triggers, no relay traffic, nothing admitted.
        self.assertEqual(self.verifier.calls, [])
        self.assertEqual(self.relay.queries, [])
        querier = self.service._meta_querier_obj()
        self.assertEqual(querier.store.counts()["cached_batches"],
                         self.batches_before)

    def test_local_exact_schedules_and_fetches_nothing(self):
        out = self.service.meta_enrichment(
            {"kind": "track", "mode": "exact", "query": SUBJECT,
             "network": False})
        self._assert_pure(out)
        record = out["records"][0]
        self.assertEqual(record["subject"], SUBJECT)
        values = record["fields"]["deezer_id"]["values"]
        self.assertEqual([v["value"] for v in values], ["12345"])

    def test_local_text_schedules_and_fetches_nothing(self):
        out = self.service.meta_enrichment(
            {"kind": "album", "mode": "text", "query": "OK Computer",
             "network": False})
        self._assert_pure(out)
        self.assertEqual(out["records"][0]["subject"], ALBUM_ID)

    def test_local_text_unresolved_is_a_miss_not_an_ask(self):
        out = self.service.meta_enrichment(
            {"kind": "artist", "mode": "text", "query": "NobodyReal",
             "network": False})
        self._assert_pure(out)
        self.assertEqual(out["records"], [])
        self.assertEqual(out["unresolved"], ["NobodyReal"])

    def test_ask_scope_still_schedules_verification(self):
        # The other scope is unchanged: the same subject
        # through the ask path schedules the first check of its
        # unverdicted cached claims.
        out = self.service.meta_enrichment(
            {"kind": "track", "mode": "exact", "query": SUBJECT})
        self.assertTrue(out["ok"])
        self.assertIn(([SUBJECT], False), self.verifier.calls)


class RefreshingVerifier:
    """Drains verification the way a same-or-better re-check
    lands (§7, and set_verdict's own contract): every verdict
    already recorded for the asked subjects is refreshed to
    now through the store's monotonic set_verdict. Claims
    carrying no verdict stay unverdicted — their first checks
    are still the pool's business — so a drain moves the
    STALE count (by exactly the number of stale verdicts it
    refreshed) and no other counter's presence."""

    def __init__(self, store, clock):
        self.store = store
        self.clock = clock
        self.calls = []

    def verify_subjects(self, subjects, force=False):
        self.calls.append((sorted(subjects), force))
        subjects = list(subjects)
        verdicts = self.store.verdicts_for_subjects(subjects)
        rows = {(r["origin_node"], r["subject"], r["field"],
                 value_key(r["value"])): r
                for r in self.store.cached_claims(subjects)}
        for key, verdict in verdicts.items():
            row = rows.get(key)
            if row is None:
                continue
            self.store.set_verdict(
                key[0], key[1], key[2], row["value"],
                verdict["tier"], self.clock(), "re-check")
        return []

    def note_conflict(self, origin, subject, field):
        return False


class EnrichmentPageTest(ServiceCase):
    def setUp(self):
        super().setUp()
        self.service.rebuild_meta_store(self.service.clock())
        from metasync_node.app import create_app
        self.app = create_app(self.service)
        self.client = browser_client(self.app)

    def _login(self):
        self.client.post("/setup", data={
            "password": "test-password", "confirm": "test-password"})

    def test_page_requires_login(self):
        self.assertEqual(self.client.get("/enrichment").status_code, 302)

    def test_page_without_a_store_says_so(self):
        fresh_dir = tempfile.TemporaryDirectory()
        self.addCleanup(fresh_dir.cleanup)
        fresh = NodeService(
            Config(state_dir=fresh_dir.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=Store(fresh_dir.name),
            identity=NodeIdentity(key="ef" * 32),
            soulsync=FakeSoulSync(full_pages()), env={})
        from metasync_node.app import create_app
        client = browser_client(create_app(fresh))
        client.post("/setup", data={
            "password": "test-password", "confirm": "test-password"})
        html = client.get("/enrichment").get_data(as_text=True)
        self.assertIn("No metadata store yet", html)

    def test_lookup_renders_the_record(self):
        self._login()
        html = self.client.get(
            "/enrichment?kind=album&mode=exact"
            "&q=deezer%3A3135556&scope=local").get_data(as_text=True)
        self.assertEqual(self.client.get(
            "/enrichment").status_code, 200)
        self.assertIn("OK Computer", html)
        self.assertIn("deezer_id", html)
        self.assertIn("your library", html)
        # Support renders as the §9 bucket on this page too —
        # the exact count and its phrasing never appear.
        self.assertIn("support bucket", html)
        self.assertNotIn("standing origin", html)

    def test_network_strings_are_escaped(self):
        origin = NodeIdentity(key="aa" * 32)
        claims = track_claims() + [{
            "subject": SUBJECT, "kind": "track", "field": "genres",
            "value": "<script>alert(1)</script>", "source": "deezer",
            "soulid_version": C.SOULID_VERSION}]
        self.service._meta_querier_obj().admit(
            envelope(origin, claims=claims))
        self._login()
        html = self.client.get(
            "/enrichment?kind=track&mode=exact&scope=local&q="
            + SUBJECT).get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        # The network value renders — but its provenance does
        # not: no origin key, no "who asserted this" fold (§9).
        self.assertNotIn("Who asserted this", html)
        self.assertNotIn(origin.public_key_b64url, html)

    def test_page_never_renders_origin_keys_batches_or_counts(self):
        # Two distinct origins assert the SAME deezer value, so
        # the merged record carries both keys and both batch
        # ids behind one value. The rendered page must contain
        # neither key, neither batch id, and no exact count —
        # only the §9 bucket (§19 binds step 9).
        origins = [NodeIdentity(key="aa" * 32),
                   NodeIdentity(key="bb" * 32)]
        querier = self.service._meta_querier_obj()
        batch_ids = []
        for origin in origins:
            env = envelope(origin, claims=track_claims())
            batch_ids.append(C.batch_digest(env["batch"]))
            querier.admit(env)
        self._login()
        html = self.client.get(
            "/enrichment?kind=track&mode=exact&scope=local&q="
            + SUBJECT).get_data(as_text=True)
        self.assertIn("12345", html)  # the value itself renders
        self.assertIn("support bucket", html)
        for origin in origins:
            self.assertNotIn(origin.public_key_b64url, html)
        for batch_id in batch_ids:
            self.assertNotIn(batch_id, html)
        self.assertNotIn("Who asserted this", html)
        self.assertNotIn("standing origin", html)

    def test_unknown_subject_renders_the_empty_state(self):
        self._login()
        html = self.client.get(
            "/enrichment?kind=artist&mode=text&scope=local"
            "&q=NobodyReal").get_data(as_text=True)
        self.assertIn("Nothing known about that yet", html)

    def test_template_context_is_stripped(self):
        # Gate MF-1 + confirmation SF-2: the service result
        # (full records with origins, batches, exact support
        # and reports, the own key string, the whole querier
        # status) must NOT cross into the template context —
        # the route builds the context from the presenter
        # views plus a fixed set of lookup facts. Two origins
        # assert the same value, so everything forbidden
        # genuinely exists behind the page being rendered.
        # The walk is STRICT, in three ways the first version
        # was not: forbidden strings are matched as
        # SUBSTRINGS of every string in the context (a key
        # embedded in a longer string — "origin: <key>" — is
        # a leak, not a pass); BOTH scopes are pinned (the
        # ask path builds the same context out of the same
        # full result); and a value of any type outside
        # dict/list/tuple/str/int/bool/None FAILS the walk —
        # an unfamiliar object in the context is a finding,
        # not a skip. (Flask's own injected g/request stand
        # outside the route's values and outside this walk.)
        from flask import template_rendered
        origins = [NodeIdentity(key="aa" * 32),
                   NodeIdentity(key="bb" * 32)]
        querier = self.service._meta_querier_obj()
        batch_ids = []
        for origin in origins:
            env = envelope(origin, claims=track_claims())
            batch_ids.append(C.batch_digest(env["batch"]))
            querier.admit(env)
        forbidden_strings = [
            self.identity.public_key_b64url,
            *(o.public_key_b64url for o in origins),
            *batch_ids]
        captured = []

        def _capture(sender, template, context, **extra):
            captured.append(context)

        def _check(context):
            # The route's context surface is exactly its
            # fixed kwargs — nothing else rides along.
            route_values = {k: v for k, v in context.items()
                            if k not in ("g", "request")}
            self.assertEqual(
                set(route_values),
                {"kind", "mode", "scope", "query",
                 "store_exists", "result", "views"})
            # The lookup facts are exactly the fixed set, and
            # the backlog pair crosses shaped: BOTH counters
            # as presence only ("none" / "some") — no count,
            # no bucket, nothing numeric for either tile.
            self.assertEqual(set(context["result"]),
                             {"ok", "asked_network", "unresolved",
                              "backlog"})
            self.assertEqual(set(context["result"]["backlog"]),
                             {"unverified_claims",
                              "stale_verdicts"})
            self.assertIn(
                context["result"]["backlog"]["unverified_claims"],
                ("none", "some"))
            self.assertIn(
                context["result"]["backlog"]["stale_verdicts"],
                ("none", "some"))
            keys: set = set()
            strings: list = []
            supports: list = []

            def _walk(node, path="context"):
                if isinstance(node, dict):
                    for k, v in node.items():
                        keys.add(k)
                        if k == "support":
                            supports.append(v)
                        _walk(v, f"{path}.{k}")
                elif isinstance(node, (list, tuple)):
                    for i, item in enumerate(node):
                        _walk(item, f"{path}[{i}]")
                elif isinstance(node, str):
                    strings.append(node)
                elif node is None \
                        or isinstance(node, (int, bool)):
                    pass
                else:
                    self.fail(
                        f"{path}: context value of unexpected "
                        f"type {type(node).__name__} — the route "
                        "hands the template plain data only")

            _walk(route_values)
            forbidden_keys = {"origins", "batches", "reports",
                              "records", "own_key", "querier",
                              "cached_batches", "cached_claims",
                              "expired_claims"}
            self.assertFalse(forbidden_keys & keys)
            # Support crosses only as the §9 bucket string.
            self.assertTrue(supports)
            self.assertTrue(
                all(isinstance(s, str) for s in supports))
            # No key string — ours or a peer's — and no batch
            # id appears ANYWHERE in any context string, not
            # even embedded inside a longer one.
            for s in strings:
                for forbidden in forbidden_strings:
                    self.assertNotIn(forbidden, s)

        self._login()
        with template_rendered.connected_to(_capture, self.app):
            self.client.get(
                "/enrichment?kind=track&mode=exact&scope=local&q="
                + SUBJECT)
            self._instrumented_querier()
            self.client.get(
                "/enrichment?kind=track&mode=exact&scope=ask&q="
                + SUBJECT)
        self.assertEqual(len(captured), 2)
        for context in captured:
            _check(context)

    def _instrumented_querier(self):
        origin = NodeIdentity(key="aa" * 32)
        querier = self.service._meta_querier_obj()
        querier.admit(envelope(origin, claims=track_claims()))
        verifier = RecordingVerifier()
        querier.verifier = verifier
        querier.verify_inline = True
        relay = FakeRelayClient()
        querier.relay_provider = lambda: relay
        return verifier, relay

    def test_missing_scope_defaults_to_local(self):
        # Gate SF: a bare /enrichment?q=... link (a cross-site
        # top-level navigation carries the session, SameSite=
        # Lax) must not run an ask. Missing scope is local;
        # the record still renders from the pure read.
        verifier, relay = self._instrumented_querier()
        self._login()
        html = self.client.get(
            "/enrichment?kind=track&mode=exact&q="
            + SUBJECT).get_data(as_text=True)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(relay.queries, [])
        self.assertIn('value="local" selected', html)
        self.assertIn("12345", html)

    def test_explicit_ask_scope_still_asks(self):
        verifier, _relay = self._instrumented_querier()
        self._login()
        self.client.get(
            "/enrichment?kind=track&mode=exact&scope=ask&q="
            + SUBJECT)
        self.assertIn(([SUBJECT], False), verifier.calls)

    def test_tiles_show_the_backlog_not_cache_counters(self):
        # The cache-size tiles are gone: an exact global
        # counter beside a lookup hands back, by subtraction,
        # the batches that lookup admitted — the number the
        # service keeps out of its result. The backlog pair
        # stays, shaped as PRESENCE only: both counters move
        # for reasons correlated with other nodes' claims
        # (admissions land unverdicted; asks force-recheck
        # stale verdicts away), so neither a count nor a
        # bucket is safe (the next tests pin why). Nothing
        # has been admitted on this node, so both tiles read
        # "none".
        self._login()
        html = self.client.get(
            "/enrichment?kind=album&mode=exact"
            "&q=deezer%3A3135556&scope=local").get_data(as_text=True)
        self.assertIn("Cached claims awaiting a first check", html)
        self.assertIn("Verdicts stale (re-check before reliance)",
                      html)
        self.assertIn(
            '<span class="stat-num">none</span>'
            '<span class="stat-label">'
            "Cached claims awaiting a first check</span>", html)
        self.assertIn(
            '<span class="stat-num">none</span>'
            '<span class="stat-label">'
            "Verdicts stale (re-check before reliance)</span>",
            html)
        self.assertNotIn("Answer batches cached", html)
        self.assertNotIn("Cached claims held", html)

    @staticmethod
    def _tile(html, label):
        marker = f'<span class="stat-label">{label}</span>'
        head = html.split(marker)[0]
        return head.rsplit('<span class="stat-num">', 1)[1] \
            .split("</span>")[0]

    @staticmethod
    def _tile_pair(html):
        return (EnrichmentPageTest._tile(
                    html, "Cached claims awaiting a first check"),
                EnrichmentPageTest._tile(
                    html, "Verdicts stale (re-check before "
                          "reliance)"))

    def _local_tiles(self, client, query):
        html = client.get(
            "/enrichment?kind=track&mode=exact&scope=local&q="
            + query).get_data(as_text=True)
        return self._tile_pair(html)

    def test_unverified_tile_cannot_reveal_admissions(self):
        # Confirmation MF + confirmation-2 SF-1: the kept
        # backlog tile leaked by subtraction. Newly admitted
        # claims land in the unverified queue unverdicted
        # (verification runs on a background pool), so the
        # tile's EXACT count after an ask minus its count
        # before is the number of new origin-and-claim pairs
        # the ask admitted — for one field, a support count
        # §9 hides. The bucket leaked too, at its lower
        # boundary (a drained queue plus a "1-2" queue pins
        # single-origin values). The tile now renders
        # PRESENCE: renders whose exact queue counts DIFFER
        # — 0, then 2, then 6 — flip the tile none -> some
        # exactly once and never move it again.
        querier = self.service._meta_querier_obj()

        def exact_unverified():
            return querier.status()["verification"][
                "unverified_claims"]

        def tile():
            html = self.client.get(
                "/enrichment?kind=album&mode=exact"
                "&q=deezer%3A3135556&scope=local"
            ).get_data(as_text=True)
            return self._tile(
                html, "Cached claims awaiting a first check")

        self._login()
        self.assertEqual(exact_unverified(), 0)
        self.assertEqual(tile(), "none")
        querier.admit(envelope(
            NodeIdentity(key="aa" * 32), claims=track_claims()))
        self.assertEqual(exact_unverified(), 2)
        self.assertEqual(tile(), "some")
        for key in ("bb", "cc"):
            querier.admit(envelope(
                NodeIdentity(key=key * 32), claims=track_claims()))
        self.assertGreater(exact_unverified(), 2)
        self.assertEqual(tile(), "some")

    def _fresh_page(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = NodeService(
            Config(state_dir=tmp.name,
                   musicbrainz_min_interval_s=0.0, http_timeout_s=5),
            store=Store(tmp.name),
            identity=NodeIdentity(key="ef" * 32),
            soulsync=FakeSoulSync(full_pages()), env={})
        service.rebuild_meta_store(service.clock())
        from metasync_node.app import create_app
        client = browser_client(create_app(service))
        client.post("/setup", data={
            "password": "test-password",
            "confirm": "test-password"})
        return service, client

    def _presence_scenario(self, k):
        """One node holding k stale strong verdicts on the
        seeded track (one per origin), run through the ask
        phase: an ask that ADMITS new claims (a subject the
        cache cannot answer, served by the relay) and an ask
        that force-rechecks the seeded record's stale
        verdicts, drained inline by the refreshing verifier.
        Returns the (before, after) tile pairs and the four
        rendered pages (local, both asks, local again). The
        exact
        counters are asserted internally at every step — the
        scenario really moves them by k — but only presence
        may reach the tiles."""
        service, client = self._fresh_page()
        querier = service._meta_querier_obj()
        origins = [NodeIdentity(key=("%02x" % (0xA0 + i)) * 32)
                   for i in range(k)]
        for origin in origins:
            querier.admit(envelope(origin, claims=track_claims()))
        for origin in origins:
            querier.store.set_verdict(
                origin.public_key_b64url, SUBJECT, "deezer_id",
                "12345", "strong-verified",
                querier.clock() - 100 * DAY, "old check")
        exact = querier.status()["verification"]
        self.assertEqual(exact["stale_verdicts"], k)
        self.assertGreater(exact["unverified_claims"], 0)
        local_url = ("/enrichment?kind=track&mode=exact"
                     "&scope=local&q=" + SUBJECT)
        before_html = client.get(local_url).get_data(as_text=True)
        before = self._tile_pair(before_html)
        self.assertEqual(before, ("some", "some"))

        other = S.generate_soul_id("track", "Radiohead", "Creep")
        other_claims = [
            {"subject": other, "kind": "track", "field": "name",
             "value": ["Radiohead", "Creep"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": other, "kind": "track",
             "field": "deezer_id", "value": "777",
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION}]
        drainer = RefreshingVerifier(querier.store, querier.clock)
        querier.verifier = drainer
        querier.verify_inline = True
        relay = FakeRelayClient([envelope(
            NodeIdentity(key="f0" * 32), claims=other_claims)])
        querier.relay_provider = lambda: relay
        # Ask 1 admits: the uncached subject goes to the
        # relay, its claims land unverdicted.
        ask1_html = client.get(
            "/enrichment?kind=track&mode=exact&scope=ask&q="
            + other).get_data(as_text=True)
        self.assertEqual(len(relay.queries), 1)
        self.assertGreater(
            querier.status()["verification"]["unverified_claims"],
            exact["unverified_claims"])
        # Ask 2 force-rechecks: the seeded record's stale
        # verdicts are scheduled FORCED, and the drain
        # refreshes every one of them.
        ask2_html = client.get(
            "/enrichment?kind=track&mode=exact&scope=ask&q="
            + SUBJECT).get_data(as_text=True)
        self.assertIn(([SUBJECT], True), drainer.calls)
        # The drain moved the exact stale count by EXACTLY
        # k — the movement the exact tile handed back, and
        # that separated 1 from 2 inside the bucket.
        self.assertEqual(
            querier.status()["verification"]["stale_verdicts"], 0)
        after_html = client.get(local_url).get_data(as_text=True)
        after = self._tile_pair(after_html)
        self.assertEqual(after, ("some", "none"))
        return before, after, [before_html, ask1_html, ask2_html,
                               after_html]

    def test_tiles_identical_across_different_stale_counts(self):
        # Confirmation-2 MF: the stale-verdicts tile was
        # EXACT, and an ask moves it once per origin whose
        # stale verdict gets force-rechecked — render S, ask,
        # drain, render S-k, and k separates 1 from 2 inside
        # the "1-2" bucket (and counts a "3+" outright). The
        # stale tile and the unverified tile are PRESENCE
        # now: two scenarios whose true stale counts DIFFER
        # (k=1 and k=2, both drained to 0 by the ask phase)
        # render identical tiles at both renders.
        before1, after1, _pages1 = self._presence_scenario(1)
        before2, after2, _pages2 = self._presence_scenario(2)
        self.assertEqual(before1, before2)
        self.assertEqual(after1, after2)

    @staticmethod
    def _normalize(html):
        # Verdict stamps render to the minute and the two
        # scenarios seed and drain seconds apart — the stamp
        # is the page's only wall-clock-shaped string.
        return re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC",
                      "<time>", html)

    def test_whole_page_identical_across_different_stale_counts(self):
        # Confirmation-3 SF-2: the tiles were compared; the
        # PAGE is the surface. Two scenarios whose true stale
        # counts differ (k=1 vs k=2) must render identical
        # pages at every step — the local render, both ask
        # renders, and the post-drain local render — so no
        # signal anywhere on the page (tally, header,
        # buckets, gap lists) separates 1 from 2. VALUE
        # ORDER is not among the signals this pin can speak
        # to (confirmation-4 SF-2): all k origins assert
        # identical claims, so no field has a same-bucket
        # rival and exact-support order cannot differ
        # between the scenarios. Value order is pinned at
        # the presenter level instead —
        # test_values_render_in_bucket_order_not_exact_
        # support for values, test_held_years_render_in_
        # value_order_not_merge_order for held years.
        _b1, _a1, pages1 = self._presence_scenario(1)
        _b2, _a2, pages2 = self._presence_scenario(2)
        self.assertEqual(len(pages1), 4)
        self.assertEqual(len(pages1), len(pages2))
        for one, two in zip(pages1, pages2):
            self.assertEqual(self._normalize(one),
                             self._normalize(two))

    def test_stale_tile_holds_some_across_a_partial_drop(self):
        # Confirmation-3 SF-2, second half: presence must
        # also absorb a PARTIAL drop. Two stale verdicts on
        # two subjects; an ask force-rechecks only the asked
        # subject, so the exact count falls 2 -> 1 — and the
        # tile reads "some" on both sides: the drop happened,
        # and nothing on the page says how far it fell.
        service, client = self._fresh_page()
        querier = service._meta_querier_obj()
        first = NodeIdentity(key="aa" * 32)
        second = NodeIdentity(key="bb" * 32)
        querier.admit(envelope(first, claims=track_claims()))
        other = S.generate_soul_id("track", "Radiohead", "Creep")
        other_claims = [
            {"subject": other, "kind": "track", "field": "name",
             "value": ["Radiohead", "Creep"],
             "source": "file_tags",
             "soulid_version": C.SOULID_VERSION},
            {"subject": other, "kind": "track",
             "field": "deezer_id", "value": "777",
             "source": "deezer",
             "soulid_version": C.SOULID_VERSION}]
        querier.admit(envelope(second, claims=other_claims))
        for origin, subject, value in (
                (first, SUBJECT, "12345"), (second, other, "777")):
            querier.store.set_verdict(
                origin.public_key_b64url, subject, "deezer_id",
                value, "strong-verified",
                querier.clock() - 100 * DAY, "old check")

        def stale():
            return querier.status()["verification"][
                "stale_verdicts"]

        self.assertEqual(stale(), 2)
        self.assertEqual(self._local_tiles(client, SUBJECT),
                         ("some", "some"))
        drainer = RefreshingVerifier(querier.store, querier.clock)
        querier.verifier = drainer
        querier.verify_inline = True
        relay = FakeRelayClient()
        querier.relay_provider = lambda: relay
        client.get("/enrichment?kind=track&mode=exact&scope=ask&q="
                   + SUBJECT)
        self.assertIn(([SUBJECT], True), drainer.calls)
        # The asked subject's verdict refreshed; the other
        # subject's did not — the count fell exactly halfway.
        self.assertEqual(stale(), 1)
        self.assertEqual(self._local_tiles(client, SUBJECT),
                         ("some", "some"))

    def test_backlog_tiles_flip_none_to_some(self):
        # Presence still answers the operator's one question
        # — is there any backlog at all: a quiet node renders
        # none/none; admitted claims flip the queue tile; a
        # stale verdict flips the stale tile.
        querier = self.service._meta_querier_obj()
        self._login()
        html = self.client.get(
            "/enrichment?kind=album&mode=exact"
            "&q=deezer%3A3135556&scope=local").get_data(as_text=True)
        self.assertEqual(
            (self._tile(html,
                        "Cached claims awaiting a first check"),
             self._tile(html,
                        "Verdicts stale (re-check before "
                        "reliance)")),
            ("none", "none"))
        origin = NodeIdentity(key="aa" * 32)
        querier.admit(envelope(origin, claims=track_claims()))
        self.assertEqual(
            self._local_tiles(self.client, SUBJECT),
            ("some", "none"))
        querier.store.set_verdict(
            origin.public_key_b64url, SUBJECT, "deezer_id",
            "12345", "strong-verified",
            querier.clock() - 100 * DAY, "old check")
        self.assertEqual(
            self._local_tiles(self.client, SUBJECT),
            ("some", "some"))


if __name__ == "__main__":
    unittest.main()

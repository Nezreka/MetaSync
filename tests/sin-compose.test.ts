/**
 * Composer tests — chart selection, editions, and MusicBrainz mapping.
 * Pure logic only; the endpoint's DB orchestration is verified live.
 * Run with: npm test   (node's built-in test runner, TS type-stripping)
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  buildEditions,
  decadeLabel,
  genreDisplay,
  selectChartSpecs,
  slugifyGenre,
  type AggregateRow,
  type ChartSpec,
} from "../src/sin/logic.ts";
import {
  artistBundle,
  coverArtUrl,
  pickReleaseId,
  releaseGroupBundle,
} from "../src/sin/mb.ts";

const W = "2026-W43";
const PW = "2026-W42";
const RG = (n: number) => `aaaaaaaa-aaaa-4aaa-8aaa-${String(n).padStart(12, "0")}`;
const ART = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

function rows(): AggregateRow[] {
  const out: AggregateRow[] = [];
  const add = (week: string, signal: AggregateRow["signal"], mbid: string, nodeCount: number) =>
    out.push({ week, signal, mbid, nodeCount });
  add(W, "import_keep", RG(1), 40);
  add(W, "import_keep", RG(2), 30);
  add(W, "recent_add", RG(1), 20);
  add(W, "wishlist_add", RG(1), 25);
  add(PW, "wishlist_add", RG(1), 5);
  add(W, "hunt_demand", RG(3), 22);
  add(W, "watchlist_add", ART, 30);
  add(PW, "watchlist_add", ART, 10);
  return out;
}

describe("selectChartSpecs", () => {
  test("builds the flagship catalog from one week of aggregates", () => {
    const specs = selectChartSpecs(rows(), W, [PW], PW);
    const byKey = new Map(specs.map((s) => [s.key, s]));
    assert.deepEqual(
      [...byKey.keys()].sort(),
      ["breakouts", "just-landed", "loved-and-kept", "most-wanted", "rising", "trending"],
    );
    assert.equal(byKey.get("most-wanted")!.items[0]!.mbid, RG(1)); // 25 wishlist beats 22 hunt-only
    assert.equal(byKey.get("loved-and-kept")!.items[0]!.mbid, RG(1)); // 40 keeps
    assert.equal(byKey.get("just-landed")!.items[0]!.mbid, RG(1));
    assert.equal(byKey.get("breakouts")!.items[0]!.kind, "artist");
    assert.equal(byKey.get("trending")!.items[0]!.mbid, RG(1)); // scored vs prior 5
    assert.ok(byKey.get("rising")!.items.some((i) => i.mbid === RG(1)));
    for (const spec of specs) assert.ok(spec.items.length > 0);
  });

  test("omits week-over-week charts when there is no previous week", () => {
    const specs = selectChartSpecs(rows(), W, [], null);
    const keys = specs.map((s) => s.key);
    assert.ok(!keys.includes("rising"));
    assert.ok(!keys.includes("breakouts"));
    assert.ok(keys.includes("most-wanted"));
  });
});

describe("buildEditions", () => {
  const base: ChartSpec = {
    key: "trending",
    name: "Trending in SoulSync",
    items: Array.from({ length: 9 }, (_, i) => ({
      mbid: RG(i + 1),
      kind: "release-group" as const,
      score: 100 - i,
    })),
  };

  test("genre and decade editions need 8+ items sharing the tag", () => {
    const meta = new Map(
      base.items.map((i) => [i.mbid, { genre: "rock", year: 1994 }]),
    );
    const editions = buildEditions([base], meta);
    const keys = editions.map((e) => e.key).sort();
    assert.deepEqual(keys, ["trending-1990s", "trending-rock"]);
    assert.equal(editions.find((e) => e.key === "trending-rock")!.name, "Trending: Rock");
    assert.equal(editions.find((e) => e.key === "trending-1990s")!.items.length, 9);
  });

  test("thin editions are not built", () => {
    const meta = new Map(
      base.items.map((i, idx) => [i.mbid, { genre: idx < 3 ? "jazz" : "rock", year: 2001 }]),
    );
    const editions = buildEditions([base], meta);
    assert.ok(!editions.some((e) => e.key === "trending-jazz"));
  });

  test("slug and label helpers", () => {
    assert.equal(slugifyGenre("Hip Hop"), "hip-hop");
    assert.equal(slugifyGenre("R&B"), "r-and-b");
    assert.equal(genreDisplay("hip hop"), "Hip Hop");
    assert.equal(genreDisplay("r&b"), "R&B");
    assert.equal(decadeLabel(1994), "1990s");
    assert.equal(decadeLabel(2003), "2000s");
  });
});

describe("musicbrainz mapping", () => {
  const group = {
    id: RG(7),
    title: "Fixture Album",
    "first-release-date": "1994-03-01",
    "artist-credit": [{ name: "The Fixtures", artist: { id: ART, name: "The Fixtures" } }],
    genres: [
      { name: "rock", count: 4 },
      { name: "alternative rock", count: 9 },
    ],
    releases: [
      { id: "rel-new", date: "2020-01-01", status: "Official" },
      { id: "rel-old", date: "1994-03-01", status: "Official" },
      { id: "rel-boot", date: "1990-01-01", status: "Bootleg" },
    ],
  };
  const release = {
    media: [
      {
        tracks: [
          { id: "trk-1", recording: { id: "rec-1", title: "One", length: 61_000 } },
          { id: "trk-2", recording: { id: "rec-2", title: "Two", length: 62_000 } },
        ],
      },
    ],
  };

  test("pickReleaseId chooses the earliest official release", () => {
    assert.equal(pickReleaseId(group), "rel-old");
  });

  test("releaseGroupBundle maps tracks, genre by votes, and year", () => {
    const bundle = releaseGroupBundle(group, release);
    assert.equal(bundle.title, "Fixture Album");
    assert.equal(bundle.artistName, "The Fixtures");
    assert.equal(bundle.artistMbid, ART);
    assert.equal(bundle.genre, "alternative rock");
    assert.equal(bundle.year, 1994);
    assert.equal(bundle.tracks.length, 2);
    assert.equal(bundle.tracks[0]!.id, "rec-1");
    assert.equal(bundle.tracks[0]!.duration_ms, 61_000);
    assert.equal(bundle.tracks[0]!.image_url, coverArtUrl(RG(7)));
  });

  test("artistBundle keeps album groups only", () => {
    const bundle = artistBundle({
      id: ART,
      name: "The Fixtures",
      genres: [{ name: "rock", count: 3 }],
      "release-groups": [
        { id: RG(1), "primary-type": "Album" },
        { id: RG(2), "primary-type": "Single" },
        { id: RG(3), "primary-type": "Album" },
      ],
    });
    assert.deepEqual(bundle.releaseGroupMbids, [RG(1), RG(3)]);
    assert.equal(bundle.genre, "rock");
  });
});

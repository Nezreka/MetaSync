/**
 * Composer tests — chart selection, editions, and MusicBrainz mapping.
 * Pure logic only; the endpoint's DB orchestration is verified live.
 * Run with: npm test   (node's built-in test runner, TS type-stripping)
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  buildEditions,
  editionRepeatsBase,
  decadeLabel,
  genreDisplay,
  selectChartSpecs,
  slugifyGenre,
  type AggregateRow,
  type ChartSpec,
} from "../src/sin/logic.ts";
import {
  artistBundle,
  CHART_TRACKS_PER_ALBUM,
  chartTracksFor,
  coverArtUrl,
  ListenBrainzPopularity,
  pickReleaseId,
  pickTopTrackIds,
  releaseGroupBundle,
  type ReleaseGroupBundle,
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

  test("on-repeat charts listen_album only, and only when someone opted in", () => {
    assert.ok(!selectChartSpecs(rows(), W, [], null).some((s) => s.key === "on-repeat"));
    const withListens = [
      ...rows(),
      { week: W, signal: "listen_album" as const, mbid: RG(2), nodeCount: 18 },
      { week: W, signal: "listen_album" as const, mbid: RG(5), nodeCount: 26 },
      { week: PW, signal: "listen_album" as const, mbid: RG(7), nodeCount: 90 },
    ];
    const onRepeat = selectChartSpecs(withListens, W, [PW], PW).find((s) => s.key === "on-repeat")!;
    assert.deepEqual(onRepeat.items.map((i) => i.mbid), [RG(5), RG(2)]);
    assert.equal(onRepeat.name, "On Repeat");
  });
});

describe("download signals in scoring", () => {
  function downloadRows(): AggregateRow[] {
    const out: AggregateRow[] = [];
    const add = (signal: AggregateRow["signal"], mbid: string, nodeCount: number) =>
      out.push({ week: W, signal, mbid, nodeCount });
    // Trending: RG(10) wishlist 20 + grab 18 must count 20 (max, not 38);
    // RG(11) grab-only 22 must beat it.
    add("wishlist_add", RG(10), 20);
    add("download_grab", RG(10), 18);
    add("download_grab", RG(11), 22);
    // Loved & Kept: net of churn; churn >= keeps drops out entirely.
    add("import_keep", RG(12), 30);
    add("download_churn", RG(12), 12);
    add("import_keep", RG(13), 20);
    add("download_churn", RG(13), 20);
    add("import_keep", RG(14), 25);
    return out;
  }

  test("trending heat is max(wishlist, grab), never the sum", () => {
    const specs = selectChartSpecs(downloadRows(), W, [], null);
    const trending = specs.find((s) => s.key === "trending")!;
    assert.equal(trending.items[0]!.mbid, RG(11));
    assert.equal(trending.items[0]!.score, 22);
    assert.equal(trending.items.find((i) => i.mbid === RG(10))!.score, 20);
  });

  test("loved-and-kept ranks by keeps net of churn and drops net-negative albums", () => {
    const specs = selectChartSpecs(downloadRows(), W, [], null);
    const loved = specs.find((s) => s.key === "loved-and-kept")!;
    assert.equal(loved.items[0]!.mbid, RG(14)); // 25 keeps, no churn
    assert.equal(loved.items[0]!.score, 25);
    assert.equal(loved.items.find((i) => i.mbid === RG(12))!.score, 18); // 30 - 12
    assert.ok(!loved.items.some((i) => i.mbid === RG(13))); // 20 - 20 drops out
  });
});

describe("buildEditions", () => {
  const base: ChartSpec = {
    key: "trending",
    name: "Trending in SoulSync",
    items: Array.from({ length: 10 }, (_, i) => ({
      mbid: RG(i + 1),
      kind: "release-group" as const,
      score: 100 - i,
    })),
  };

  test("genre and decade editions need 8+ items sharing the tag", () => {
    // nine of the ten share both tags; the tenth is jazz from 2001
    const meta = new Map(
      base.items.map((i, idx) => [i.mbid, idx < 9 ? { genre: "rock", year: 1994 } : { genre: "jazz", year: 2001 }]),
    );
    const editions = buildEditions([base], meta);
    const keys = editions.map((e) => e.key).sort();
    assert.deepEqual(keys, ["trending-1990s", "trending-rock"]);
    assert.equal(editions.find((e) => e.key === "trending-rock")!.name, "Trending: Rock");
    assert.equal(editions.find((e) => e.key === "trending-1990s")!.items.length, 9);
  });

  test("an edition that would be its whole chart again is skipped", () => {
    // every Just Landed album is from the 2020s: "Just Landed: 2020s" is noise
    const landed: ChartSpec = { ...base, key: "just-landed", name: "Just Landed" };
    const meta = new Map(base.items.map((i) => [i.mbid, { genre: "pop", year: 2026 }]));
    assert.deepEqual(buildEditions([landed], meta), []);
  });

  test("just-landed never gets a decade edition, even with years missing", () => {
    // two albums lack a release year, so "2020s" holds 8 of 10 on paper;
    // published, it would still be Just Landed again (it was, 42 of 42)
    const landed: ChartSpec = { ...base, key: "just-landed", name: "Just Landed" };
    const meta = new Map(
      base.items.map((i, idx) => [i.mbid, idx < 8 ? { genre: `g${idx}`, year: 2026 } : { genre: `g${idx}` }]),
    );
    assert.deepEqual(buildEditions([landed], meta), []);
  });

  test("editions remember their base chart", () => {
    const meta = new Map(
      base.items.map((i, idx) => [i.mbid, idx < 9 ? { genre: "rock", year: 1994 } : { genre: "jazz", year: 2001 }]),
    );
    assert.ok(buildEditions([base], meta).every((e) => e.base === "trending"));
  });

  test("an edition that publishes mostly its base's tracks repeats it", () => {
    const ids = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `t${from + i}`);
    assert.equal(editionRepeatsBase(ids(0, 42), ids(0, 42)), true); // identical
    assert.equal(editionRepeatsBase(ids(0, 40), ids(0, 46)), true); // 40/46 = 87%
    assert.equal(editionRepeatsBase(ids(0, 34), ids(0, 46)), false); // 34/46 = 74%
    assert.equal(editionRepeatsBase([...ids(0, 20), ...ids(100, 130)], ids(0, 46)), false);
    assert.equal(editionRepeatsBase(ids(0, 10), []), false); // base not published
  });

  test("just-landed gets editions too", () => {
    const landed: ChartSpec = { ...base, key: "just-landed", name: "Just Landed" };
    const meta = new Map(
      base.items.map((i, idx) => [i.mbid, { genre: idx < 8 ? "k-pop" : "metal", year: 2026 }]),
    );
    const editions = buildEditions([landed], meta);
    assert.deepEqual(editions.map((e) => [e.key, e.name]), [["just-landed-k-pop", "Just Landed: K-Pop"]]);
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

describe("popular tracks per album", () => {
  const rec = (n: number) => `bbbbbbbb-bbbb-4bbb-8bbb-${String(n).padStart(12, "0")}`;
  const album = (count: number, topTrackIds?: string[]): ReleaseGroupBundle => ({
    mbid: RG(1),
    kind: "release-group",
    title: "Album",
    artistName: "Artist",
    tracks: Array.from({ length: count }, (_, i) => ({
      id: rec(i + 1),
      name: `Track ${i + 1}`,
      artists: [{ name: "Artist" }],
    })),
    ...(topTrackIds ? { topTrackIds } : {}),
  });

  test("ranks by distinct listeners, then listens, and caps at five", () => {
    const pop = new Map([
      [rec(1), { listens: 900, users: 10 }], // one fan on repeat
      [rec(2), { listens: 300, users: 200 }],
      [rec(3), { listens: 500, users: 200 }],
      [rec(4), { listens: 50, users: 40 }],
      [rec(5), { listens: 40, users: 30 }],
      [rec(6), { listens: 30, users: 20 }],
      [rec(7), { listens: 0, users: 0 }], // no data: never picked
    ]);
    // five picks are cached per album; a playlist uses the top two
    assert.deepEqual(pickTopTrackIds(album(8).tracks, pop), [rec(3), rec(2), rec(4), rec(5), rec(6)]);
    assert.equal(CHART_TRACKS_PER_ALBUM, 2);
  });

  test("no popularity data: the album's first two tracks, in order", () => {
    assert.deepEqual(pickTopTrackIds(album(8).tracks, new Map()), []);
    assert.deepEqual(chartTracksFor(album(8, [])).map((t) => t.id), [1, 2].map(rec));
    assert.deepEqual(chartTracksFor(album(8)).map((t) => t.id), [1, 2].map(rec));
    assert.equal(chartTracksFor(album(1)).length, 1); // a one-track release gives what it has
  });

  test("picked tracks come out in popularity order", () => {
    assert.deepEqual(chartTracksFor(album(8, [rec(6), rec(2)])).map((t) => t.id), [rec(6), rec(2)]);
  });

  test("the ListenBrainz call sends only recording MBIDs, batched", async () => {
    const bodies: string[] = [];
    const lb = new ListenBrainzPopularity(async (url, init) => {
      assert.match(url, /\/1\/popularity\/recording$/);
      bodies.push(init.body);
      const ids = (JSON.parse(init.body) as { recording_mbids: string[] }).recording_mbids;
      return {
        ok: true,
        status: 200,
        json: async () => ids.map((id, i) => ({ recording_mbid: id, total_listen_count: i, total_user_count: i === 0 ? null : 1 })),
      };
    });
    const ids = Array.from({ length: 150 }, (_, i) => rec(i).toUpperCase());
    const pop = await lb.recordings(ids);
    assert.equal(bodies.length, 2);
    assert.deepEqual(Object.keys(JSON.parse(bodies[0]!)), ["recording_mbids"]);
    assert.equal(pop.size, 150);
    assert.deepEqual(pop.get(rec(0)), { listens: 0, users: 0 });
  });

  test("a failed ListenBrainz call throws so the album is retried, not cached empty", async () => {
    const lb = new ListenBrainzPopularity(async () => ({ ok: false, status: 503, json: async () => ({}) }));
    await assert.rejects(lb.recordings([rec(1)]));
  });
});

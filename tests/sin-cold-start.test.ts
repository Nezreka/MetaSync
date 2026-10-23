/**
 * Cold start + build transparency + count retention:
 * - public ListenBrainz items fill only the slots the network cannot,
 *   and the chart's name says so;
 * - /health reports the deployed commit;
 * - private below-k counts are dropped once their week can no longer
 *   change, after everything at or above k was published.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  chartDisplayName,
  fillFromPublic,
  isoWeekOf,
  K_PUBLISH_THRESHOLD,
  type ChartSpec,
} from "../src/sin/logic.ts";
import { ListenBrainzCharts } from "../src/sin/public-charts.ts";
import { ListenBrainzPopularity, MbResolver } from "../src/sin/mb.ts";
import type { SinDb } from "../src/sin/db.ts";
import { FakeSinDb } from "./fake-db.ts";

const RG = (n: number) => `aaaaaaaa-aaaa-4aaa-8aaa-${String(n).padStart(12, "0")}`;
const REC = (n: number) => `bbbbbbbb-bbbb-4bbb-8bbb-${String(n).padStart(12, "0")}`;

describe("public fill", () => {
  test("network items always first; public items take only the empty slots", () => {
    const network: ChartSpec = {
      key: "trending",
      name: "Trending in SoulSync",
      items: [{ mbid: RG(1), kind: "release-group", score: 9 }],
    };
    const filled = fillFromPublic("trending", network, [RG(1), RG(2), RG(3)], 3)!;
    assert.deepEqual(filled.items.map((i) => [i.mbid, i.source ?? "network"]), [
      [RG(1), "network"],
      [RG(2), "public"],
      [RG(3), "public"],
    ]);
  });

  test("a full network chart takes no public items", () => {
    const network: ChartSpec = {
      key: "trending",
      name: "Trending in SoulSync",
      items: [RG(1), RG(2)].map((mbid) => ({ mbid, kind: "release-group" as const, score: 5 })),
    };
    assert.equal(fillFromPublic("trending", network, [RG(3)], 2)!.items.every((i) => !i.source), true);
  });

  test("an empty network still gets a chart; no data at all gets none", () => {
    assert.equal(fillFromPublic("just-landed", undefined, [RG(5)])!.items.length, 1);
    assert.equal(fillFromPublic("just-landed", undefined, []), undefined);
  });

  test("the name says where the music came from", () => {
    assert.equal(chartDisplayName("trending", 25, 0), "Trending in SoulSync");
    assert.equal(chartDisplayName("trending", 10, 15), "Trending · SoulSync + ListenBrainz");
    assert.equal(chartDisplayName("trending", 0, 25), "Trending · from ListenBrainz");
    assert.equal(chartDisplayName("just-landed", 0, 25), "Just Landed · from ListenBrainz");
  });
});

/** A fake ListenBrainz: weekly chart with a duplicate and a missing id,
 *  and a fresh-release list with a Single that must not count. */
function fakeCharts(opts: { failFresh?: boolean; failAll?: boolean } = {}) {
  return new ListenBrainzCharts(async (url) => {
    if (opts.failAll) return { ok: false, status: 503, json: async () => ({}) };
    if (url.includes("/stats/sitewide/release-groups")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          payload: {
            release_groups: [
              { release_group_mbid: RG(1) },
              { release_group_mbid: RG(1) },
              { release_group_mbid: null },
              { release_group_mbid: RG(2) },
              { release_group_mbid: RG(3) },
            ],
          },
        }),
      };
    }
    if (opts.failFresh) return { ok: false, status: 500, json: async () => ({}) };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        payload: {
          releases: [
            { release_group_mbid: RG(2), release_group_primary_type: "Album" },
            { release_group_mbid: RG(3), release_group_primary_type: "Single" },
          ],
        },
      }),
    };
  });
}

describe("ListenBrainz chart seeds", () => {
  test("trending is the deduped weekly top; just-landed its fresh albums", async () => {
    assert.deepEqual(await fakeCharts().seeds(), {
      trending: [RG(1), RG(2), RG(3)],
      "just-landed": [RG(2)],
      recordings: [],
    });
  });

  test("a failed release-date read still fills trending", async () => {
    assert.deepEqual(await fakeCharts({ failFresh: true }).seeds(), {
      trending: [RG(1), RG(2), RG(3)],
      "just-landed": [],
      recordings: [],
    });
  });

  test("ListenBrainz down: no public fill at all, never an error", async () => {
    assert.deepEqual(await fakeCharts({ failAll: true }).seeds(), {
      trending: [],
      "just-landed": [],
      recordings: [],
    });
  });
});

describe("build transparency", () => {
  test("/health names the deployed commit and its source tree", async () => {
    const { buildInfo } = await import("../src/sin/index.ts");
    const sha = "41a4562914341dd3215266ba83d28ab4eccabd3a";
    assert.deepEqual(buildInfo({ SIN_BUILD_COMMIT: sha, SIN_BUILD_DIRTY: "false", SIN_BUILD_TIME: "2026-10-23T12:00:00.000Z" }), {
      commit: sha,
      dirty: false,
      source: `https://github.com/Nezreka/MetaSync/tree/${sha}`,
      deployedAt: "2026-10-23T12:00:00.000Z",
    });
    // Unknown or malformed: null, never a guess.
    assert.equal(buildInfo({ SIN_BUILD_COMMIT: "unknown" }).commit, null);
    assert.equal(buildInfo({}).source, null);
  });
});

describe("count retention", () => {
  test("old below-k counts are dropped after anything at k was published", async () => {
    const mod = await import("../src/sin/index.ts");
    const fake = new FakeSinDb();
    const now = new Date("2026-10-23T12:00:00Z");
    const old = isoWeekOf(new Date(now.getTime() - 5 * 7 * 86_400_000));
    const recent = isoWeekOf(now);
    fake.countRows.push(
      { week: old, signal: "recent_add", mbid: RG(1), nodeCount: 3 },
      { week: old, signal: "recent_add", mbid: RG(2), nodeCount: K_PUBLISH_THRESHOLD },
      { week: recent, signal: "recent_add", mbid: RG(3), nodeCount: 3 },
    );
    await mod.aggregateOnce(fake as unknown as SinDb, { now });
    assert.deepEqual(fake.aggregateRows.map((r) => r.mbid), [RG(2)]); // published first
    assert.deepEqual(fake.countRows.map((r) => r.mbid), [RG(3)]); // old week gone, current kept
  });
});

describe("compose on an empty network (end to end, no network calls)", () => {
  test("publishes honestly named ListenBrainz charts, 5 popular tracks per album", async () => {
    process.env.SIN_ADMIN_KEY = "test-admin-secret-0123456789";
    const mod = await import("../src/sin/index.ts");
    const fake = new FakeSinDb();
    mod.setDbResolverForTests(() => fake as unknown as SinDb);
    mod.resetRateLimiterForTests();
    const tracks = (g: number) =>
      Array.from({ length: 8 }, (_, i) => ({ id: REC(g * 100 + i), name: `T${i}`, artists: [{ name: "A" }] }));
    mod.setComposeSourcesForTests({
      charts: fakeCharts(),
      mb: {
        releaseGroup: async (mbid: string) =>
          mbid === RG(3) ? null : { mbid, kind: "release-group", title: "Album", artistName: "A", tracks: tracks(Number(mbid.slice(-2))) },
        artist: async () => null,
      } as unknown as MbResolver,
      popularity: new ListenBrainzPopularity(async (_url, init) => ({
        ok: true,
        status: 200,
        json: async () =>
          (JSON.parse(init.body) as { recording_mbids: string[] }).recording_mbids.map((id, i) => ({
            recording_mbid: id,
            total_listen_count: 10 * i,
            total_user_count: i, // the album's LAST tracks are the popular ones
          })),
      })),
    });
    try {
      const res = await mod.default.request("/v1/compose", {
        method: "POST",
        headers: { "x-sin-key": "test-admin-secret-0123456789" },
        body: "{}",
      });
      assert.equal(res.status, 200);
      const body = (await res.json()) as { published: string[]; week: string };
      assert.deepEqual(body.published.sort(), ["just-landed", "trending"]);
      const trending = fake.playlistRows.find((p) => p.key === "trending")!;
      assert.equal(trending.name, "Trending · from ListenBrainz");
      // RG(3) has no official release: skipped, not waited for. RG(1) and
      // RG(2) give their two most popular tracks each, laid out round by
      // round: both albums' best first, then both seconds.
      assert.deepEqual(
        trending.tracks.map((t) => (t as { id: string }).id),
        [REC(107), REC(207), REC(106), REC(206)],
      );
      assert.equal(fake.playlistRows.find((p) => p.key === "just-landed")!.name, "Just Landed · from ListenBrainz");
    } finally {
      mod.setComposeSourcesForTests();
      mod.setDbResolverForTests(null);
    }
  });
});

describe("chart shape", () => {
  test("interleave: every album's best first, then every second; no repeats", async () => {
    const { interleaveByRank } = await import("../src/sin/logic.ts");
    const t = (id: string) => ({ id });
    assert.deepEqual(
      interleaveByRank([[t("a1"), t("a2")], [t("b1"), t("b2")], [t("c1")], [t("a1"), t("d2")]]).map((x) => x.id),
      ["a1", "b1", "c1", "a2", "b2", "d2"],
    );
    assert.equal(interleaveByRank([[t("x"), t("y")], [t("z")]], 2).length, 2);
  });

  test("top tracks: public song chart, two per artist, rows without an id skipped", async () => {
    const { topTracksFromRecordings } = await import("../src/sin/logic.ts");
    const row = (n: number, artist: string, mbid: string | null = REC(n)) => ({
      recording_mbid: mbid,
      track_name: `Song ${n}`,
      artist_name: artist,
      artist_mbids: [`${artist}-id`],
      release_name: "Album",
      caa_release_mbid: RG(n),
    });
    const tracks = topTracksFromRecordings([
      row(1, "Band"), row(2, "Band"), row(3, "Band"), row(4, "Solo", null), row(5, "Solo"), row(1, "Band"),
    ]);
    assert.deepEqual(tracks.map((t) => t.id), [REC(1), REC(2), REC(5)]);
    assert.deepEqual(tracks[0]!.album, { name: "Album", images: [{ url: `https://coverartarchive.org/release/${RG(1)}/front-250` }] });
  });

  test("an edition of a public chart says so in its name", async () => {
    const { sourcedName } = await import("../src/sin/logic.ts");
    assert.equal(sourcedName("Trending: Rock", 0, 9), "Trending: Rock · from ListenBrainz");
    assert.equal(sourcedName("Trending: Rock", 4, 5), "Trending: Rock · SoulSync + ListenBrainz");
    assert.equal(sourcedName("Most Wanted: Rock", 9, 0), "Most Wanted: Rock");
  });
});

describe("compose: top tracks and stale charts (end to end, no network calls)", () => {
  function chartsWithRecordings() {
    return new ListenBrainzCharts(async (url) => {
      if (url.includes("/stats/sitewide/recordings")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            payload: {
              recordings: [1, 2, 3].map((n) => ({
                recording_mbid: REC(900 + n),
                track_name: `Hit ${n}`,
                artist_name: `Artist ${n}`,
                release_name: "Single",
              })),
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ payload: { release_groups: [], releases: [] } }) };
    });
  }

  test("publishes Top Tracks, withdraws a chart the pass no longer makes, keeps all when it makes none", async () => {
    process.env.SIN_ADMIN_KEY = "test-admin-secret-0123456789";
    const mod = await import("../src/sin/index.ts");
    const fake = new FakeSinDb();
    const week = isoWeekOf(new Date(Date.now() - 7 * 86_400_000));
    fake.playlistRows.push({ week, key: "trending-rock", name: "Old edition", tracks: [{}], publishedAt: new Date() });
    mod.setDbResolverForTests(() => fake as unknown as SinDb);
    mod.resetRateLimiterForTests();
    const compose = () =>
      mod.default.request("/v1/compose", {
        method: "POST",
        headers: { "x-sin-key": "test-admin-secret-0123456789" },
        body: "{}",
      });
    try {
      mod.setComposeSourcesForTests({ charts: chartsWithRecordings() });
      const res = await compose();
      assert.equal(res.status, 200);
      assert.deepEqual(((await res.json()) as { published: string[] }).published, ["top-tracks"]);
      const top = fake.playlistRows.find((p) => p.key === "top-tracks")!;
      assert.equal(top.name, "Top Tracks · from ListenBrainz");
      assert.equal(top.tracks.length, 3);
      assert.ok(!fake.playlistRows.some((p) => p.key === "trending-rock")); // withdrawn

      // ListenBrainz down on an empty network: nothing made, nothing withdrawn
      mod.setComposeSourcesForTests({ charts: fakeCharts({ failAll: true }) });
      assert.equal((await compose()).status, 200);
      assert.ok(fake.playlistRows.some((p) => p.key === "top-tracks"));
    } finally {
      mod.setComposeSourcesForTests();
      mod.setDbResolverForTests(null);
    }
  });
});

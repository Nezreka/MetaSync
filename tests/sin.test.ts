/**
 * SIN tests — pure logic + route behaviour WITHOUT a live database.
 * Run with: npm test   (node's built-in test runner, TS type-stripping)
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  aggregateRawReports,
  BREAKOUT_MIN_NODES,
  computeBreakouts,
  computeMostWanted,
  computeTrending,
  dedupeRawReports,
  dedupeSignals,
  isIsoWeekString,
  isoWeekOf,
  isUuidShape,
  K_PUBLISH_THRESHOLD,
  keepRateReason,
  MAX_LISTEN_SIGNALS,
  MAX_SIGNALS_PER_REPORT,
  MAX_SIGNALS_PER_TYPE,
  openReportWeeks,
  reportToCountIncrements,
  trendingScore,
  validateReport,
  type AggregateRow,
  type RawReportRow,
} from "../src/sin/logic.ts";

const MBID_A = "11111111-1111-1111-1111-111111111111";
const MBID_B = "22222222-2222-4222-8222-222222222222";
const TOKEN_1 = "a".repeat(64);
const TOKEN_2 = "b".repeat(64);
const WEEK = "2026-W43";

function rawRows(
  count: number,
  overrides: Partial<RawReportRow> = {},
): RawReportRow[] {
  return Array.from({ length: count }, (_, i) => ({
    week: WEEK,
    token: `token-${String(i).padStart(4, "0")}-abcdefghij`,
    signal: "wishlist_add" as const,
    mbid: MBID_A,
    receivedAt: new Date(),
    ...overrides,
  }));
}

describe("validation", () => {
  test("accepts a valid report and dedupes its signals", () => {
    const result = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: [
        { type: "watchlist_add", mbid: MBID_A },
        { type: "watchlist_add", mbid: MBID_A }, // duplicate — binary rule
        { type: "wishlist_add", mbid: MBID_B },
      ],
    });
    assert.equal(result.ok, true);
    if (result.ok) assert.equal(result.value.signals.length, 2);
  });

  test("rejects an unknown signal type", () => {
    const result = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: [{ type: "play_count", mbid: MBID_A }],
    });
    assert.equal(result.ok, false);
  });

  test("rejects a non-UUID mbid", () => {
    const result = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: [{ type: "watchlist_add", mbid: "not-a-uuid" }],
    });
    assert.equal(result.ok, false);
  });

  test("rejects a bad week and a bad token", () => {
    assert.equal(
      validateReport({ week: "2026-43", token: TOKEN_1, signals: [{ type: "watchlist_add", mbid: MBID_A }] }).ok,
      false,
    );
    assert.equal(
      validateReport({ week: WEEK, token: "short", signals: [{ type: "watchlist_add", mbid: MBID_A }] }).ok,
      false,
    );
  });

  test("rejects smuggled never-collected fields", () => {
    const withTitle = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: [{ type: "watchlist_add", mbid: MBID_A, title: "Some Song" }],
    });
    assert.equal(withTitle.ok, false);
    const withExtra = validateReport({
      week: WEEK,
      token: TOKEN_1,
      username: "someone",
      signals: [{ type: "watchlist_add", mbid: MBID_A }],
    });
    assert.equal(withExtra.ok, false);
  });

  test("accepts recent_add; refuses a library-dump-sized report", () => {
    const recent = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: [{ type: "recent_add", mbid: MBID_A }],
    });
    assert.equal(recent.ok, true);
    // A report is one week of events, never a library listing: a type
    // above its cap is a dump and is refused outright.
    const dump = (n: number, type = "import_keep") =>
      Array.from({ length: n }, (_, i) => ({
        type,
        mbid: `aaaaaaaa-aaaa-4aaa-8aaa-${String(i).padStart(12, "0")}`,
      }));
    assert.equal(
      validateReport({ week: WEEK, token: TOKEN_1, signals: dump(MAX_SIGNALS_PER_TYPE) }).ok,
      true,
    );
    const over = validateReport({
      week: WEEK,
      token: TOKEN_1,
      signals: dump(MAX_SIGNALS_PER_TYPE + 1),
    });
    assert.equal(over.ok, false);
    if (!over.ok) assert.match(over.errors.join(" "), /import_keep exceeds max/);
  });

  test("listen_album validates and has its own tighter cap", () => {
    const listens = (n: number) =>
      Array.from({ length: n }, (_, i) => ({
        type: "listen_album",
        mbid: `bbbbbbbb-bbbb-4bbb-8bbb-${String(i).padStart(12, "0")}`,
      }));
    assert.equal(validateReport({ week: WEEK, token: TOKEN_1, signals: listens(1) }).ok, true);
    assert.equal(
      validateReport({ week: WEEK, token: TOKEN_1, signals: listens(MAX_LISTEN_SIGNALS) }).ok,
      true,
    );
    assert.equal(
      validateReport({ week: WEEK, token: TOKEN_1, signals: listens(MAX_LISTEN_SIGNALS + 1) }).ok,
      false,
    );
  });

  test("accepts download_grab and download_churn signals", () => {
    for (const type of ["download_grab", "download_churn"] as const) {
      const res = validateReport({
        week: WEEK,
        token: TOKEN_1,
        signals: [{ type, mbid: MBID_A }],
      });
      assert.equal(res.ok, true, `${type} should validate`);
    }
  });

  test("rejects empty and oversized signal arrays", () => {
    assert.equal(
      validateReport({ week: WEEK, token: TOKEN_1, signals: [] }).ok,
      false,
    );
    const big = Array.from({ length: MAX_SIGNALS_PER_REPORT + 1 }, () => ({
      type: "watchlist_add",
      mbid: MBID_A,
    }));
    assert.equal(
      validateReport({ week: WEEK, token: TOKEN_1, signals: big }).ok,
      false,
    );
  });

  test("shape helpers", () => {
    assert.equal(isUuidShape(MBID_B), true);
    assert.equal(isUuidShape("22222222-2222-4222-8222"), false);
    assert.equal(isIsoWeekString("2026-W01"), true);
    assert.equal(isIsoWeekString("2026-W54"), false);
    assert.equal(isIsoWeekString("2026-W00"), false);
  });
});

describe("dedupe", () => {
  test("dedupeSignals collapses (type, mbid) pairs case-insensitively", () => {
    const out = dedupeSignals([
      { type: "watchlist_add", mbid: MBID_A.toUpperCase() },
      { type: "watchlist_add", mbid: MBID_A },
      { type: "wishlist_add", mbid: MBID_A },
    ]);
    assert.equal(out.length, 2);
    assert.equal(out[0]?.mbid, MBID_A);
  });

  test("dedupeRawReports collapses the binary key", () => {
    const row: RawReportRow = {
      week: WEEK, token: TOKEN_1, signal: "import_keep", mbid: MBID_A,
      receivedAt: new Date(),
    };
    const other: RawReportRow = { ...row, token: TOKEN_2 };
    assert.equal(dedupeRawReports([row, row, other]).length, 2);
  });

  test("reportToCountIncrements: one +1 per distinct signal, and no token", () => {
    const inc = reportToCountIncrements({
      week: WEEK,
      token: TOKEN_1,
      signals: [
        { type: "watchlist_add", mbid: MBID_A },
        { type: "watchlist_add", mbid: MBID_A },
        { type: "listen_album", mbid: MBID_B },
      ],
    });
    assert.equal(inc.length, 2);
    assert.ok(!JSON.stringify(inc).includes(TOKEN_1));
  });
});

describe("report weeks", () => {
  test("isoWeekOf follows ISO-8601 (Monday weeks, week 1 holds Jan 4)", () => {
    assert.equal(isoWeekOf(new Date("2026-10-23T12:00:00Z")), "2026-W43");
    assert.equal(isoWeekOf(new Date("2026-10-19T00:00:00Z")), "2026-W43"); // Monday
    assert.equal(isoWeekOf(new Date("2026-10-18T23:59:59Z")), "2026-W42"); // Sunday
    assert.equal(isoWeekOf(new Date("2021-01-03T12:00:00Z")), "2020-W53");
    assert.equal(isoWeekOf(new Date("2024-12-30T12:00:00Z")), "2025-W01");
  });

  test("only the week that just closed and the current week are open", () => {
    const now = new Date("2026-10-19T00:05:00Z"); // Monday of W43
    assert.deepEqual(openReportWeeks(now), ["2026-W42", "2026-W43"]);
  });
});

describe("aggregation + k threshold", () => {
  test("counts distinct tokens only — a resubmission never inflates", () => {
    const rows = rawRows(K_PUBLISH_THRESHOLD);
    const withDupes = [...rows, rows[0]!, rows[0]!];
    const out = aggregateRawReports(withDupes);
    assert.equal(out.length, 1);
    assert.equal(out[0]?.nodeCount, K_PUBLISH_THRESHOLD);
  });

  test("exactly k publishes, k-1 does not exist publicly", () => {
    assert.equal(aggregateRawReports(rawRows(K_PUBLISH_THRESHOLD)).length, 1);
    assert.equal(aggregateRawReports(rawRows(K_PUBLISH_THRESHOLD - 1)).length, 0);
  });

  test("custom k is honoured", () => {
    assert.equal(aggregateRawReports(rawRows(3), 3).length, 1);
    assert.equal(aggregateRawReports(rawRows(3), 4).length, 0);
  });

});

describe("scoring", () => {
  test("trendingScore = adds / (1 + avg prior adds)", () => {
    assert.equal(trendingScore(20, [10, 10, 10]), 20 / 11);
    assert.equal(trendingScore(20, []), 20);
    assert.equal(trendingScore(0, [5]), 0);
  });

  const agg = (
    week: string, signal: AggregateRow["signal"], mbid: string, nodeCount: number,
  ): AggregateRow => ({ week, signal, mbid, nodeCount });

  test("computeTrending ranks a surging item above a coasting one", () => {
    const rows: AggregateRow[] = [
      agg("2026-W43", "wishlist_add", MBID_A, 30), // surging: no baseline
      agg("2026-W43", "wishlist_add", MBID_B, 60), // coasting: big baseline
      agg("2026-W42", "wishlist_add", MBID_B, 60),
      agg("2026-W41", "wishlist_add", MBID_B, 60),
      agg("2026-W40", "wishlist_add", MBID_B, 60),
      agg("2026-W43", "hunt_demand", MBID_B, 99), // not an "add" — ignored
    ];
    const items = computeTrending(rows, "2026-W43", ["2026-W40", "2026-W41", "2026-W42"]);
    assert.equal(items[0]?.mbid, MBID_A);
    assert.equal(items[0]?.reason, "Added by 30 collectors this week");
    assert.equal(items[1]?.score, 60 / 61);
  });

  test("computeMostWanted sums wishlist + hunt_demand with the hard-to-find reason", () => {
    const rows: AggregateRow[] = [
      agg(WEEK, "wishlist_add", MBID_A, 40),
      agg(WEEK, "hunt_demand", MBID_A, 56),
      agg(WEEK, "wishlist_add", MBID_B, 20),
      agg("2026-W42", "wishlist_add", MBID_B, 999), // wrong week — ignored
    ];
    const items = computeMostWanted(rows, WEEK);
    assert.equal(items[0]?.mbid, MBID_A);
    assert.equal(items[0]?.nodeCount, 96);
    assert.equal(items[0]?.reason, "Wishlisted by 96 collectors, still hard to find");
    assert.equal(items[1]?.reason, "Wishlisted by 20 collectors this week");
  });

  test("computeBreakouts: >=2x growth and >=20 nodes", () => {
    const rows: AggregateRow[] = [
      agg(WEEK, "watchlist_add", MBID_A, 50),
      agg("2026-W42", "watchlist_add", MBID_A, 20), // 2.5x — breakout
      agg(WEEK, "watchlist_add", MBID_B, 30),
      agg("2026-W42", "watchlist_add", MBID_B, 20), // 1.5x — no
    ];
    const items = computeBreakouts(rows, WEEK, "2026-W42");
    assert.equal(items.length, 1);
    assert.equal(items[0]?.mbid, MBID_A);
    assert.match(items[0]?.reason ?? "", /grew 2\.5x/);
  });

  test("computeBreakouts: brand-new artist with no prior week counts, thin ones do not", () => {
    const rows: AggregateRow[] = [
      agg(WEEK, "watchlist_add", MBID_A, BREAKOUT_MIN_NODES),
      agg(WEEK, "watchlist_add", MBID_B, BREAKOUT_MIN_NODES - 1),
    ];
    const items = computeBreakouts(rows, WEEK, "2026-W42");
    assert.equal(items.length, 1);
    assert.equal(items[0]?.growth, null);
  });

  test("keepRateReason stays silent below k", () => {
    assert.equal(keepRateReason(10, 10), null);
    assert.equal(keepRateReason(92, 100), "Kept by 92% of collectors who downloaded it");
  });
});

describe("routes (no live database)", () => {
  test("sin: health is public, writes fail closed without/with wrong key", async () => {
    process.env.SIN_INGEST_KEY = "test-ingest-secret-0123456789";
    delete process.env.DATABASE_URL;
    const openWeek = isoWeekOf(new Date());
    const { default: sinApp } = await import("../src/sin/index.ts");

    const health = await sinApp.request("/health");
    assert.equal(health.status, 200);
    assert.equal((await health.json() as { service: string }).service, "sin");

    const noKey = await sinApp.request("/v1/report", {
      method: "POST",
      body: JSON.stringify({ week: WEEK, token: TOKEN_1, signals: [] }),
    });
    assert.equal(noKey.status, 401);

    const badBody = await sinApp.request("/v1/report", {
      method: "POST",
      headers: { "x-sin-key": "test-ingest-secret-0123456789" },
      body: JSON.stringify({ week: WEEK, token: TOKEN_1, signals: [{ type: "play_count", mbid: MBID_A }] }),
    });
    assert.equal(badBody.status, 400);

    // Valid + authed, but no DATABASE_URL in tests -> 503, never a fake success.
    const valid = await sinApp.request("/v1/report", {
      method: "POST",
      headers: { "x-sin-key": "test-ingest-secret-0123456789" },
      body: JSON.stringify({
        week: openWeek,
        token: TOKEN_1,
        signals: [{ type: "watchlist_add", mbid: MBID_A }],
      }),
    });
    assert.equal(valid.status, 503);

    // Reads are gated since §3A.7: no credential at all -> 401; with a
    // valid key the auth passes and the missing DB answers 503.
    const indexAnon = await sinApp.request("/v1/index/trending");
    assert.equal(indexAnon.status, 401);
    const index = await sinApp.request("/v1/index/trending", {
      headers: { "x-sin-key": "test-ingest-secret-0123456789" },
    });
    assert.equal(index.status, 503); // DB not configured in tests
  });

  test("metasync scaffold: health + 501 placeholder", async () => {
    const { default: metaApp } = await import("../src/metasync/index.ts");
    const health = await metaApp.request("/health");
    assert.equal(health.status, 200);
    assert.equal((await health.json() as { status: string }).status, "scaffold");

    const record = await metaApp.request("/v1/records/soul_abc123");
    assert.equal(record.status, 501);
    assert.equal((await record.json() as { error: string }).error, "not_implemented");
  });
});

/**
 * Operations + Loved & Kept population:
 * - a report may say churn_optin (true only); it is counted per week in
 *   report_populations, once per accepted report;
 * - Loved & Kept scales drops by reporters / opted-in reporters, capped;
 * - compose builds the week that just closed, not the last week that
 *   happened to reach k;
 * - GET /v1/admin/status reports counts and chart keys, admin key only.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  CHURN_SCALE_MAX,
  churnScale,
  isoWeekOf,
  openReportWeeks,
  selectChartSpecs,
  validateReport,
  type AggregateRow,
} from "../src/sin/logic.ts";
import { ListenBrainzCharts } from "../src/sin/public-charts.ts";
import type { SinDb } from "../src/sin/db.ts";
import { FakeSinDb } from "./fake-db.ts";

const RG = (n: number) => `aaaaaaaa-aaaa-4aaa-8aaa-${String(n).padStart(12, "0")}`;
const TOKEN = "t".repeat(64);
const ADMIN = "test-admin-secret-0123456789";

describe("churn_optin on a report", () => {
  const base = { week: "2026-W43", token: TOKEN, signals: [{ type: "import_keep", mbid: RG(1) }] };

  test("true is accepted and carried; absent means off", () => {
    const on = validateReport({ ...base, churn_optin: true });
    assert.ok(on.ok && on.value.churnOptin === true);
    const off = validateReport(base);
    assert.ok(off.ok && off.value.churnOptin === undefined);
  });

  test("anything but true is refused", () => {
    for (const bad of [false, "yes", 1, null]) {
      assert.equal(validateReport({ ...base, churn_optin: bad }).ok, false);
    }
  });
});

describe("Loved & Kept population scaling", () => {
  test("drops scale by reporters / opted-in, capped, and default to 1", () => {
    assert.equal(churnScale(undefined), 1);
    assert.equal(churnScale({ reports: 100, churnOptin: 0 }), 1);
    assert.equal(churnScale({ reports: 100, churnOptin: 50 }), 2);
    assert.equal(churnScale({ reports: 100, churnOptin: 1 }), CHURN_SCALE_MAX);
    assert.equal(churnScale({ reports: 10, churnOptin: 20 }), 1); // never below 1
  });

  test("drops from a small opted-in share count for the whole network", () => {
    const W = "2026-W43";
    const rows: AggregateRow[] = [
      { week: W, signal: "import_keep", mbid: RG(1), nodeCount: 60 },
      { week: W, signal: "import_keep", mbid: RG(2), nodeCount: 30 },
      { week: W, signal: "download_churn", mbid: RG(1), nodeCount: 15 },
    ];
    const loved = (pop?: { reports: number; churnOptin: number }) =>
      selectChartSpecs(rows, W, [], null, pop).find((s) => s.key === "loved-and-kept")!.items.map((i) => i.mbid);
    // Unscaled, 60 - 15 = 45 leads 30 ...
    assert.deepEqual(loved(), [RG(1), RG(2)]);
    // ... but only a quarter of reporters share drops: 15 drops stand
    // for 60, nothing is left of the first album's keeps, and it drops out.
    assert.deepEqual(loved({ reports: 200, churnOptin: 50 }), [RG(2)]);
  });
});

function emptyCharts() {
  return new ListenBrainzCharts(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ payload: { release_groups: [], releases: [], recordings: [] } }),
  }));
}

describe("report populations, compose week and admin status (no live database)", () => {
  test("each accepted report counts once; a repeat counts nothing", async () => {
    const mod = await import("../src/sin/index.ts");
    const db = new FakeSinDb();
    const report = (token: string, churnOptin?: boolean) => ({
      week: "2026-W43",
      token,
      signals: [{ type: "import_keep" as const, mbid: RG(1) }],
      ...(churnOptin ? { churnOptin } : {}),
    });
    await mod.countReport(db as unknown as SinDb, report("a".repeat(64), true));
    await mod.countReport(db as unknown as SinDb, report("b".repeat(64)));
    assert.equal(await mod.countReport(db as unknown as SinDb, report("a".repeat(64), true)), null);
    assert.deepEqual(db.populationRows, [{ week: "2026-W43", reports: 2, churnOptin: 1 }]);
  });

  test("compose builds the week that just closed, even with older counts at k", async () => {
    process.env.SIN_ADMIN_KEY = ADMIN;
    const mod = await import("../src/sin/index.ts");
    const fake = new FakeSinDb();
    // a week long gone reached k; the closed week has nothing yet
    fake.aggregateRows.push({ week: "2026-W30", signal: "recent_add", mbid: RG(9), nodeCount: 20, computedAt: new Date() });
    mod.setDbResolverForTests(() => fake as unknown as SinDb);
    mod.resetRateLimiterForTests();
    mod.setComposeSourcesForTests({ charts: emptyCharts() });
    try {
      const res = await mod.default.request("/v1/compose", {
        method: "POST",
        headers: { "x-sin-key": ADMIN },
        body: "{}",
      });
      assert.equal(res.status, 200);
      assert.equal(((await res.json()) as { week: string }).week, openReportWeeks()[0]);
    } finally {
      mod.setDbResolverForTests(null);
      mod.setComposeSourcesForTests();
    }
  });

  test("status counts reports and lists the closed week's charts; admin key only", async () => {
    process.env.SIN_ADMIN_KEY = ADMIN;
    const mod = await import("../src/sin/index.ts");
    const fake = new FakeSinDb();
    const now = new Date();
    const [closed, current] = openReportWeeks(now) as [string, string];
    // Reports count from the tokens (which predate report_populations);
    // the populations row only supplies the opted-in share.
    for (let i = 0; i < 7; i += 1) fake.reportTokens.add(`${closed}|token-${i}`);
    fake.reportTokens.add(`${current}|token-x`);
    const previous = isoWeekOf(new Date(now.getTime() - 14 * 86_400_000));
    fake.reportTokens.add(`${previous}|token-p1`).add(`${previous}|token-p2`);
    fake.populationRows.push({ week: closed, reports: 3, churnOptin: 2 });
    fake.playlistRows.push(
      { week: closed, key: "trending", name: "T", tracks: [], publishedAt: now },
      { week: closed, key: "top-tracks", name: "Top", tracks: [], publishedAt: now },
      { week: isoWeekOf(new Date(now.getTime() - 21 * 86_400_000)), key: "old", name: "O", tracks: [], publishedAt: now },
    );
    mod.setDbResolverForTests(() => fake as unknown as SinDb);
    mod.resetRateLimiterForTests();
    try {
      assert.equal((await mod.default.request("/v1/admin/status")).status, 401);
      const res = await mod.default.request("/v1/admin/status", { headers: { "x-sin-key": ADMIN } });
      assert.equal(res.status, 200);
      const body = (await res.json()) as {
        reports: { previous: number; closed: number; current: number; closedChurnOptin: number };
        charts: { week: string; count: number; keys: string[] };
      };
      assert.deepEqual(body.reports, { previous: 2, closed: 7, current: 1, closedChurnOptin: 2 });
      assert.deepEqual(body.charts.keys, ["top-tracks", "trending"]);
      assert.equal(body.charts.week, closed);
    } finally {
      mod.setDbResolverForTests(null);
    }
  });
});

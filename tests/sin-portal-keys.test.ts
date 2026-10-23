/**
 * SIN portal-key tests (PORTAL-DESIGN.md §2.2, §3, §4).
 * Same DB-free style as the other suites: routes are driven through the
 * shared in-memory fake DB (tests/fake-db.ts). Since the review fix, a
 * raw portal key is accepted ONLY at POST /v1/auth/exchange (see
 * sin-exchange.test.ts); every data route refuses it, so a copied key
 * cannot report from a machine nobody approved.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  isoWeekOf,
  isPortalKeyValid,
  portalKeyHash,
  RequestRateLimiter,
} from "../src/sin/logic.ts";
import type { SinDb } from "../src/sin/db.ts";
import { FakeSinDb, type PortalRow } from "./fake-db.ts";

const WEEK = isoWeekOf(new Date());
const MBID = "11111111-1111-1111-1111-111111111111";
const SHARED_KEY = "test-shared-secret-0123456789";
const ADMIN_KEY = "test-admin-secret-0123456789";
const PEPPER = "test-pepper-0123456789abcdef";
const RAW_A = `sin_live_${"A".repeat(43)}`;
const RAW_B = `sin_live_${"B".repeat(43)}`;

let tokenCounter = 0;
function reportBody() {
  tokenCounter += 1;
  return {
    week: WEEK,
    token: `tok${tokenCounter}`.padEnd(64, "x"),
    signals: [{ type: "watchlist_add", mbid: MBID }],
  };
}

function portalRow(
  id: string,
  raw: string,
  overrides: Partial<PortalRow> = {},
): PortalRow {
  return {
    id,
    userId: `user_of_${id}`,
    keyHash: portalKeyHash(raw, PEPPER),
    keyPrefix: raw.slice(0, 12),
    label: "test install",
    lastUsedAt: null,
    expiresAt: null,
    createdAt: new Date("2026-10-22T00:00:00Z"),
    revokedAt: null,
    boundNodeId: null,
    boundNodePublicKey: null,
    boundAt: null,
    pendingNodeId: null,
    pendingNodePublicKey: null,
    pendingRequestedAt: null,
    ...overrides,
  };
}

async function setup(rows: PortalRow[]) {
  process.env.SIN_INGEST_KEY = SHARED_KEY;
  process.env.SIN_ADMIN_KEY = ADMIN_KEY;
  process.env.API_KEY_PEPPER = PEPPER;
  const mod = await import("../src/sin/index.ts");
  const fake = new FakeSinDb(rows);
  mod.setDbResolverForTests(() => fake as unknown as SinDb);
  mod.resetRateLimiterForTests();
  return { app: mod.default, fake, mod };
}

function post(
  app: { request: (p: string, i: RequestInit) => Promise<Response> | Response },
  key: string,
) {
  return app.request("/v1/report", {
    method: "POST",
    headers: { "x-sin-key": key },
    body: JSON.stringify(reportBody()),
  });
}

describe("portal key helpers (pure)", () => {
  test("portalKeyHash is a deterministic SHA-256 hex of pepper + key", () => {
    const h1 = portalKeyHash(RAW_A, PEPPER);
    assert.equal(h1, portalKeyHash(RAW_A, PEPPER));
    assert.match(h1, /^[0-9a-f]{64}$/);
    assert.notEqual(h1, portalKeyHash(RAW_B, PEPPER));
    assert.notEqual(h1, portalKeyHash(RAW_A, "another-pepper"));
  });

  test("validity rule: revoked or expired is invalid, no status flag", () => {
    const now = new Date("2026-10-22T12:00:00Z");
    assert.equal(isPortalKeyValid({ revokedAt: null, expiresAt: null }, now), true);
    assert.equal(
      isPortalKeyValid({ revokedAt: new Date("2026-10-21T00:00:00Z"), expiresAt: null }, now),
      false,
    );
    assert.equal(
      isPortalKeyValid({ revokedAt: null, expiresAt: new Date("2026-10-22T11:59:59Z") }, now),
      false,
    );
    assert.equal(
      isPortalKeyValid({ revokedAt: null, expiresAt: new Date("2026-10-23T00:00:00Z") }, now),
      true,
    );
  });

  test("rate limiter: per-bucket fixed windows trip at the cap", () => {
    const limiter = new RequestRateLimiter();
    const now = new Date("2026-10-22T12:00:00Z");
    for (let i = 0; i < 10; i++) {
      assert.equal(limiter.allowReport("portal:k1", now), true);
    }
    assert.equal(limiter.allowReport("portal:k1", now), false); // 11th
    assert.equal(limiter.allowReport("portal:k2", now), true); // other key unaffected
    for (let i = 0; i < 60; i++) {
      assert.equal(limiter.allowWhoami("portal:k1", now), true);
    }
    assert.equal(limiter.allowWhoami("portal:k1", now), false); // 61st
  });
});

describe("routes: the legacy door is env keys only (fake db)", () => {
  test("a valid portal key is refused on report and every read: the identical 401", async () => {
    const valid = portalRow("key-a", RAW_A);
    const revoked = portalRow("key-rev", RAW_B, {
      revokedAt: new Date("2026-10-21T00:00:00Z"),
    });
    const { app, fake } = await setup([valid, revoked]);

    const bodies: unknown[] = [];
    for (const key of [RAW_A, RAW_B, `sin_live_${"Z".repeat(43)}`]) {
      const res = await post(app, key);
      assert.equal(res.status, 401);
      bodies.push(await res.json());
    }
    // No oracle: valid, revoked, and unknown keys are indistinguishable.
    assert.deepEqual(bodies[0], { error: "unauthorized" });
    assert.deepEqual(bodies[1], bodies[0]);
    assert.deepEqual(bodies[2], bodies[0]);
    for (const path of ["/v1/whoami", "/v1/playlists", "/v1/index/most-wanted"]) {
      const res = await app.request(path, { headers: { "x-sin-key": RAW_A } });
      assert.equal(res.status, 401, path);
    }
    assert.equal(fake.countRows.length, 0);
    assert.equal(fake.reportTokens.size, 0);
    // The door never even looked the key up: no touch, no DB read path.
    assert.equal(fake.touchAttempts, 0);
  });

  test("whoami answers the env keys and 401s anything else", async () => {
    const { app } = await setup([portalRow("key-a", RAW_A)]);
    for (const key of [SHARED_KEY, ADMIN_KEY]) {
      const res = await app.request("/v1/whoami", { headers: { "x-sin-key": key } });
      assert.equal(res.status, 200);
      assert.deepEqual(await res.json(), { ok: true });
    }
    const none = await app.request("/v1/whoami");
    assert.equal(none.status, 401);
  });

  test("a shared-key report is counted and stores no key or token beside the counts", async () => {
    const { app, fake } = await setup([]);
    const res = await post(app, SHARED_KEY);
    assert.equal(res.status, 201);
    assert.equal(fake.countRows.length, 1);
    const serialized = JSON.stringify(fake.countRows);
    assert.ok(!serialized.includes(SHARED_KEY));
    assert.ok(!serialized.includes("tok"));
  });

  test("pepper unset: the shared key still works", async () => {
    const { app, fake, mod } = await setup([portalRow("key-a", RAW_A)]);
    const savedPepper = process.env.API_KEY_PEPPER;
    delete process.env.API_KEY_PEPPER;
    try {
      const shared = await post(app, SHARED_KEY);
      assert.equal(shared.status, 201);
      assert.equal(fake.countRows.length, 1);
    } finally {
      process.env.API_KEY_PEPPER = savedPepper;
      mod.setDbResolverForTests(null);
    }
  });

  test("rate limit trips per key (11th report) and does not leak across keys", async () => {
    const { app, mod } = await setup([]);
    try {
      for (let i = 0; i < 10; i++) {
        const res = await post(app, SHARED_KEY);
        assert.equal(res.status, 201, `report ${i + 1} should pass`);
      }
      const tripped = await post(app, SHARED_KEY);
      assert.equal(tripped.status, 429);
      assert.deepEqual(await tripped.json(), { error: "rate_limited" });
      // The admin key has its own bucket.
      const admin = await post(app, ADMIN_KEY);
      assert.equal(admin.status, 201);
    } finally {
      mod.setDbResolverForTests(null);
    }
  });
});

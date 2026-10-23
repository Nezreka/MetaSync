/**
 * SIN portal-key tests (PORTAL-DESIGN.md §2.2, §3, §4).
 * Same DB-free style as the other suites: routes are driven through an
 * in-memory fake DB (wired in via setDbResolverForTests) that implements
 * the exact drizzle chains the portal-key paths use — select by key_hash
 * for the door lookup, insert().onConflictDoNothing().returning() for
 * reports, and update().set().where().returning() for the throttled
 * last_used_at touch, including the WHERE-clause throttle semantics.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  isPortalKeyValid,
  portalKeyHash,
  RequestRateLimiter,
} from "../src/sin/logic.ts";
import type { SinDb } from "../src/sin/db.ts";

const WEEK = "2026-W43";
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

/** Collect the Param values of a drizzle SQL condition, in order. */
function sqlParams(cond: unknown, out: unknown[] = []): unknown[] {
  if (!cond || typeof cond !== "object") return out;
  const obj = cond as Record<string, unknown>;
  if (obj.constructor?.name === "Param") {
    out.push(obj.value);
    return out;
  }
  if (Array.isArray(cond)) {
    for (const c of cond) sqlParams(c, out);
    return out;
  }
  if (obj.constructor?.name === "SQL" && Array.isArray(obj.queryChunks)) {
    for (const c of obj.queryChunks) sqlParams(c, out);
  }
  return out;
}

interface PortalRow {
  id: string;
  userId: string;
  keyHash: string;
  keyPrefix: string;
  label: string;
  lastUsedAt: Date | null;
  expiresAt: Date | null;
  createdAt: Date;
  revokedAt: Date | null;
}

interface RawRow {
  week: string;
  tokenHash: string;
  signal: string;
  mbid: string;
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
    ...overrides,
  };
}

class FakePortalDb {
  portalRows: PortalRow[];
  rawRows: RawRow[] = [];
  touchAttempts = 0;
  touchApplied = 0;

  constructor(rows: PortalRow[]) {
    this.portalRows = rows;
  }

  select() {
    return {
      from: () => ({
        where: (cond: unknown) => ({
          limit: async (n: number) => {
            const [hash] = sqlParams(cond);
            return this.portalRows
              .filter((r) => r.keyHash === hash)
              .slice(0, n)
              .map((r) => ({ ...r }));
          },
        }),
      }),
    };
  }

  insert() {
    return {
      values: (vals: RawRow | RawRow[]) => ({
        onConflictDoNothing: () => ({
          returning: async () => {
            const rows = Array.isArray(vals) ? vals : [vals];
            const inserted: { id: string }[] = [];
            for (const row of rows) {
              const dupe = this.rawRows.some(
                (r) =>
                  r.tokenHash === row.tokenHash &&
                  r.week === row.week &&
                  r.signal === row.signal &&
                  r.mbid === row.mbid,
              );
              if (!dupe) {
                this.rawRows.push({ ...row });
                inserted.push({ id: `raw-${this.rawRows.length}` });
              }
            }
            return inserted;
          },
        }),
      }),
    };
  }

  update() {
    return {
      set: (v: { lastUsedAt: Date }) => ({
        where: (cond: unknown) => ({
          returning: async () => {
            this.touchAttempts += 1;
            const [keyId, threshold] = sqlParams(cond) as [string, Date];
            const row = this.portalRows.find((r) => r.id === keyId);
            if (
              row &&
              (row.lastUsedAt === null || row.lastUsedAt < threshold)
            ) {
              row.lastUsedAt = v.lastUsedAt;
              this.touchApplied += 1;
              return [{ id: row.id }];
            }
            return [];
          },
        }),
      }),
    };
  }
}

async function setup(rows: PortalRow[]) {
  process.env.SIN_INGEST_KEY = SHARED_KEY;
  process.env.SIN_ADMIN_KEY = ADMIN_KEY;
  process.env.API_KEY_PEPPER = PEPPER;
  const mod = await import("../src/sin/index.ts");
  const fake = new FakePortalDb(rows);
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

describe("routes: portal keys (fake db)", () => {
  test("a valid portal key posts a report (201) and the stored row carries no key/account reference", async () => {
    const row = portalRow("key-a", RAW_A);
    const { app, fake } = await setup([row]);

    const res = await post(app, RAW_A);
    assert.equal(res.status, 201);
    const body = (await res.json()) as { accepted: number; week: string };
    assert.equal(body.accepted, 1);
    assert.equal(body.week, WEEK);

    // Privacy (P1/P2): the stored report is exactly {week, tokenHash,
    // signal, mbid} — no portal key id, no user id, no key hash, and the
    // raw key appears nowhere.
    assert.equal(fake.rawRows.length, 1);
    const stored = fake.rawRows[0]!;
    assert.deepEqual(Object.keys(stored).sort(), [
      "mbid",
      "signal",
      "tokenHash",
      "week",
    ]);
    const serialized = JSON.stringify(stored);
    assert.ok(!serialized.includes(row.id));
    assert.ok(!serialized.includes(row.userId));
    assert.ok(!serialized.includes(row.keyHash));
    assert.ok(!serialized.includes(RAW_A));
  });

  test("revoked, expired, and unknown portal keys all get the identical 401", async () => {
    const revoked = portalRow("key-rev", RAW_A, {
      revokedAt: new Date("2026-10-21T00:00:00Z"),
    });
    const expiredRaw = `sin_live_${"C".repeat(43)}`;
    const expired = portalRow("key-exp", expiredRaw, {
      expiresAt: new Date("2026-10-01T00:00:00Z"),
    });
    const { app, fake } = await setup([revoked, expired]);

    const bodies: unknown[] = [];
    for (const key of [RAW_A, expiredRaw, `sin_live_${"Z".repeat(43)}`]) {
      const res = await post(app, key);
      assert.equal(res.status, 401);
      bodies.push(await res.json());
    }
    // No oracle: the three failure modes are indistinguishable.
    assert.deepEqual(bodies[0], { error: "unauthorized" });
    assert.deepEqual(bodies[1], bodies[0]);
    assert.deepEqual(bodies[2], bodies[0]);
    assert.equal(fake.rawRows.length, 0);
  });

  test("last_used_at touch fires once, then the 1-hour throttle blocks the second", async () => {
    const row = portalRow("key-a", RAW_A);
    const { app, fake } = await setup([row]);

    const first = await post(app, RAW_A);
    assert.equal(first.status, 201);
    assert.equal(fake.touchAttempts, 1);
    assert.equal(fake.touchApplied, 1);
    const firstTouch = fake.portalRows[0]!.lastUsedAt;
    assert.ok(firstTouch instanceof Date);

    const second = await post(app, RAW_A);
    assert.equal(second.status, 201);
    // The UPDATE is issued (attempt 2) but the WHERE-clause throttle
    // means it does not apply inside the window.
    assert.equal(fake.touchAttempts, 2);
    assert.equal(fake.touchApplied, 1);
    assert.equal(fake.portalRows[0]!.lastUsedAt?.getTime(), firstTouch!.getTime());
  });

  test("whoami answers each key type and 401s a bad key", async () => {
    const row = portalRow("key-a", RAW_A);
    const { app, fake } = await setup([row]);

    const shared = await app.request("/v1/whoami", {
      headers: { "x-sin-key": SHARED_KEY },
    });
    assert.equal(shared.status, 200);
    assert.deepEqual(await shared.json(), { ok: true });

    const admin = await app.request("/v1/whoami", {
      headers: { "x-sin-key": ADMIN_KEY },
    });
    assert.equal(admin.status, 200);
    assert.deepEqual(await admin.json(), { ok: true });

    const portal = await app.request("/v1/whoami", {
      headers: { "x-sin-key": RAW_A },
    });
    assert.equal(portal.status, 200);
    assert.deepEqual(await portal.json(), {
      ok: true,
      keyPrefix: RAW_A.slice(0, 12),
    });
    // whoami also counts as a check-in (§3.3/§3.4).
    assert.ok(fake.portalRows[0]!.lastUsedAt instanceof Date);

    const bad = await app.request("/v1/whoami", {
      headers: { "x-sin-key": `sin_live_${"Z".repeat(43)}` },
    });
    assert.equal(bad.status, 401);
    assert.deepEqual(await bad.json(), { error: "unauthorized" });

    const none = await app.request("/v1/whoami");
    assert.equal(none.status, 401);
  });

  test("pepper unset: portal path disabled (401), shared key still works", async () => {
    const row = portalRow("key-a", RAW_A);
    const { app, fake, mod } = await setup([row]);
    const savedPepper = process.env.API_KEY_PEPPER;
    delete process.env.API_KEY_PEPPER;
    try {
      const portal = await post(app, RAW_A);
      assert.equal(portal.status, 401);
      assert.equal(fake.rawRows.length, 0);

      const shared = await post(app, SHARED_KEY);
      assert.equal(shared.status, 201);
      assert.equal(fake.rawRows.length, 1);
    } finally {
      process.env.API_KEY_PEPPER = savedPepper;
      mod.setDbResolverForTests(null);
    }
  });

  test("rate limit trips per key (11th report) and does not leak across keys", async () => {
    const a = portalRow("key-a", RAW_A);
    const b = portalRow("key-b", RAW_B);
    const { app, mod } = await setup([a, b]);
    try {
      for (let i = 0; i < 10; i++) {
        const res = await post(app, RAW_A);
        assert.equal(res.status, 201, `report ${i + 1} should pass`);
      }
      const tripped = await post(app, RAW_A);
      assert.equal(tripped.status, 429);
      assert.deepEqual(await tripped.json(), { error: "rate_limited" });

      // A different key has its own bucket.
      const other = await post(app, RAW_B);
      assert.equal(other.status, 201);
      // And so does the shared env key.
      const shared = await post(app, SHARED_KEY);
      assert.equal(shared.status, 201);
    } finally {
      mod.setDbResolverForTests(null);
    }
  });
});

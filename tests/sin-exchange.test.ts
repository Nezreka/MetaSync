/**
 * SIN §3A tests — MetaSync-only access: the key exchange, node
 * signatures, binding, and pool-signed access tokens.
 * Same DB-free style as the other suites: routes are driven through an
 * in-memory fake DB (wired in via setDbResolverForTests) implementing
 * the drizzle chains the §3A paths use — portal select by hash AND by
 * id, the pending-binding UPDATE, the throttled last_used_at touch,
 * nonce insert/delete, plus the report/aggregate/playlist chains the
 * token door protects.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import {
  createHash,
  generateKeyPairSync,
  sign as cryptoSign,
  type KeyObject,
} from "node:crypto";
import { describe, test } from "node:test";
import {
  issueAccessToken,
  loadPoolTokenPrivateKey,
  portalKeyHash,
} from "../src/sin/logic.ts";
import {
  aggregates as aggregatesTable,
  canonicalPlaylists as playlistsTable,
  portalKeys as portalKeysTable,
  rawReports as rawReportsTable,
  sinRequestNonces as noncesTable,
} from "../src/sin/schema.ts";
import type { SinDb } from "../src/sin/db.ts";

const WEEK = "2026-W43";
const MBID = "11111111-1111-1111-1111-111111111111";
const SHARED_KEY = "test-shared-secret-0123456789";
const ADMIN_KEY = "test-admin-secret-0123456789";
const PEPPER = "test-pepper-0123456789abcdef";
const RAW = `sin_live_${"K".repeat(43)}`;

/* ---------------- node + pool key material ---------------- */

interface NodeKeys {
  privateKey: KeyObject;
  publicRaw: Buffer;
  publicB64: string;
  nodeId: string;
}

function makeNode(): NodeKeys {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const jwk = publicKey.export({ format: "jwk" }) as { x: string };
  const publicRaw = Buffer.from(jwk.x, "base64url");
  return {
    privateKey,
    publicRaw,
    publicB64: publicRaw.toString("base64url"),
    nodeId: createHash("sha256").update(publicRaw).digest("hex"),
  };
}

const poolPair = generateKeyPairSync("ed25519");
const poolPrivJwk = poolPair.privateKey.export({ format: "jwk" }) as {
  d: string;
  x: string,
};
const POOL_PRIVATE_B64 = poolPrivJwk.d; // base64url raw seed — the env format
const POOL_PUBLIC_B64 = poolPrivJwk.x;

let nonceCounter = 0;
function nextNonce(): string {
  nonceCounter += 1;
  return `nonce-${String(nonceCounter).padStart(8, "0")}-abcdefgh`;
}

function sha256Hex(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

/** Sign a request exactly per the shared contract's canonical string. */
function nodeHeaders(
  node: NodeKeys,
  method: string,
  path: string,
  body: string,
  opts: { timestamp?: number; nonce?: string; withPublicKey?: boolean } = {},
): Record<string, string> {
  const timestamp = String(opts.timestamp ?? Math.floor(Date.now() / 1000));
  const nonce = opts.nonce ?? nextNonce();
  const canonical = [method, path, timestamp, nonce, sha256Hex(body)].join("\n");
  const signature = cryptoSign(
    null,
    Buffer.from(canonical, "utf8"),
    node.privateKey,
  ).toString("base64url");
  const headers: Record<string, string> = {
    "x-node-id": node.nodeId,
    "x-node-signature": signature,
    "x-node-timestamp": timestamp,
    "x-node-nonce": nonce,
  };
  if (opts.withPublicKey) headers["x-node-public-key"] = node.publicB64;
  return headers;
}

/* ---------------- fake DB ---------------- */

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
  boundNodeId: string | null;
  boundNodePublicKey: string | null;
  boundAt: Date | null;
  pendingNodeId: string | null;
  pendingNodePublicKey: string | null;
  pendingRequestedAt: Date | null;
}

interface RawRow {
  week: string;
  tokenHash: string;
  signal: string;
  mbid: string;
  receivedAt: Date;
}

interface PlaylistRow {
  week: string;
  key: string;
  name: string;
  tracks: unknown[];
  publishedAt: Date;
}

function portalRow(overrides: Partial<PortalRow> = {}): PortalRow {
  return {
    id: "key-1",
    userId: "user-1",
    keyHash: portalKeyHash(RAW, PEPPER),
    keyPrefix: RAW.slice(0, 12),
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

function boundRow(node: NodeKeys): PortalRow {
  return portalRow({
    boundNodeId: node.nodeId,
    boundNodePublicKey: node.publicB64,
    boundAt: new Date("2026-10-22T00:00:00Z"),
  });
}

class FakeQuery {
  private cond: unknown = undefined;
  private fake: FakeExchangeDb;
  private table: unknown;
  private selection: Record<string, unknown> | undefined;
  constructor(
    fake: FakeExchangeDb,
    table: unknown,
    selection: Record<string, unknown> | undefined,
  ) {
    this.fake = fake;
    this.table = table;
    this.selection = selection;
  }
  where(cond: unknown) {
    this.cond = cond;
    return this;
  }
  orderBy() {
    return this;
  }
  limit(n: number) {
    return Promise.resolve(this.rows().slice(0, n));
  }
  then(
    onFulfilled?: (value: unknown[]) => unknown,
    onRejected?: (reason: unknown) => unknown,
  ) {
    return Promise.resolve(this.rows()).then(onFulfilled, onRejected);
  }
  private rows(): Record<string, unknown>[] {
    const params = sqlParams(this.cond);
    if (this.table === portalKeysTable) {
      return this.fake.portalRows
        .filter(
          (r) =>
            params.length === 0 ||
            params.some((p) => p === r.keyHash || p === r.id),
        )
        .map((r) => ({ ...r }));
    }
    if (this.table === rawReportsTable) {
      return this.fake.rawRows
        .filter((r) => params.length === 0 || params.some((p) => p === r.week))
        .map((r) => ({ ...r }));
    }
    if (this.table === aggregatesTable) {
      if (this.selection && "latest" in this.selection) {
        const weeks = this.fake.aggregateRows.map((r) => r.week as string);
        return [{ latest: weeks.length ? weeks.sort().at(-1)! : null }];
      }
      if (this.selection && "week" in this.selection) {
        const weeks = [
          ...new Set(this.fake.aggregateRows.map((r) => r.week as string)),
        ];
        return weeks
          .filter((w) => params.length === 0 || (params[0] as string) > w)
          .sort()
          .reverse()
          .map((week) => ({ week }));
      }
      return this.fake.aggregateRows
        .filter((r) => params.length === 0 || params.includes(r.week))
        .map((r) => ({ ...r }));
    }
    if (this.table === playlistsTable) {
      return this.fake.playlistRows.map((r) => ({ ...r }));
    }
    return [];
  }
}

class InsertQuery {
  private mode: "plain" | "nothing" | "update" = "plain";
  private updateSet: Record<string, unknown> | undefined;
  private fake: FakeExchangeDb;
  private table: unknown;
  private vals: unknown;
  constructor(fake: FakeExchangeDb, table: unknown, vals: unknown) {
    this.fake = fake;
    this.table = table;
    this.vals = vals;
  }
  onConflictDoNothing() {
    this.mode = "nothing";
    return this;
  }
  onConflictDoUpdate(cfg: { set: Record<string, unknown> }) {
    this.mode = "update";
    this.updateSet = cfg.set;
    return this;
  }
  returning() {
    return Promise.resolve(this.execute());
  }
  then(
    onFulfilled?: (value: unknown[]) => unknown,
    onRejected?: (reason: unknown) => unknown,
  ) {
    return Promise.resolve(this.execute()).then(onFulfilled, onRejected);
  }
  private execute(): Record<string, unknown>[] {
    const fake = this.fake;
    if (this.table === rawReportsTable) {
      const rows = (Array.isArray(this.vals) ? this.vals : [this.vals]) as RawRow[];
      const inserted: Record<string, unknown>[] = [];
      for (const row of rows) {
        const dupe = fake.rawRows.some(
          (r) =>
            r.tokenHash === row.tokenHash &&
            r.week === row.week &&
            r.signal === row.signal &&
            r.mbid === row.mbid,
        );
        if (!dupe) {
          fake.rawRows.push({ ...row, receivedAt: row.receivedAt ?? new Date() });
          inserted.push({ id: `raw-${fake.rawRows.length}` });
        }
      }
      return inserted;
    }
    if (this.table === noncesTable) {
      const v = this.vals as { nodeId: string; nonceHash: string; expiresAt: Date };
      const k = `${v.nodeId}|${v.nonceHash}`;
      if (fake.nonceRows.has(k)) return [];
      fake.nonceRows.set(k, { ...v });
      return [{ nodeId: v.nodeId }];
    }
    if (this.table === aggregatesTable) {
      const v = this.vals as Record<string, unknown>;
      const k = `${v.week}|${v.signal}|${v.mbid}`;
      const existing = fake.aggregateRows.find(
        (r) => `${r.week}|${r.signal}|${r.mbid}` === k,
      );
      if (existing && this.updateSet) Object.assign(existing, this.updateSet);
      else if (!existing) fake.aggregateRows.push({ ...v });
      return [];
    }
    if (this.table === playlistsTable) {
      const v = this.vals as PlaylistRow;
      const existing = fake.playlistRows.find(
        (r) => r.week === v.week && r.key === v.key,
      );
      if (existing && this.updateSet) Object.assign(existing, this.updateSet);
      else if (!existing) fake.playlistRows.push({ ...v });
      return [];
    }
    // indexPublications and anything else: accept and forget.
    return [];
  }
}

class FakeExchangeDb {
  portalRows: PortalRow[];
  rawRows: RawRow[] = [];
  aggregateRows: Record<string, unknown>[] = [];
  playlistRows: PlaylistRow[] = [];
  nonceRows = new Map<string, { nodeId: string; nonceHash: string; expiresAt: Date }>();
  touchAttempts = 0;
  touchApplied = 0;

  constructor(rows: PortalRow[]) {
    this.portalRows = rows;
  }

  select(selection?: Record<string, unknown>) {
    return {
      from: (table: unknown) => new FakeQuery(this, table, selection),
    };
  }

  insert(table: unknown) {
    return {
      values: (vals: unknown) => new InsertQuery(this, table, vals),
    };
  }

  update(table: unknown) {
    return {
      set: (v: Record<string, unknown>) => ({
        where: (cond: unknown) => ({
          returning: async () => {
            if (table !== portalKeysTable) return [];
            const [firstParam, secondParam] = sqlParams(cond);
            if ("pendingNodeId" in v) {
              const row = this.portalRows.find((r) => r.id === firstParam);
              if (!row) return [];
              row.pendingNodeId = v.pendingNodeId as string;
              row.pendingNodePublicKey = v.pendingNodePublicKey as string;
              row.pendingRequestedAt = v.pendingRequestedAt as Date;
              return [{ id: row.id }];
            }
            if ("lastUsedAt" in v) {
              this.touchAttempts += 1;
              const threshold = secondParam as Date;
              const row = this.portalRows.find((r) => r.id === firstParam);
              if (row && (row.lastUsedAt === null || row.lastUsedAt < threshold)) {
                row.lastUsedAt = v.lastUsedAt as Date;
                this.touchApplied += 1;
                return [{ id: row.id }];
              }
              return [];
            }
            return [];
          },
        }),
      }),
    };
  }

  delete(table: unknown) {
    return {
      where: (cond: unknown) => {
        const run = (): Record<string, unknown>[] => {
          const [param] = sqlParams(cond);
          if (table === noncesTable) {
            const cutoff = param as Date;
            for (const [k, v] of this.nonceRows) {
              if (v.expiresAt < cutoff) this.nonceRows.delete(k);
            }
            return [];
          }
          if (table === rawReportsTable) {
            const cutoff = param as Date;
            const before = this.rawRows.length;
            this.rawRows = this.rawRows.filter((r) => r.receivedAt >= cutoff);
            return Array.from(
              { length: before - this.rawRows.length },
              (_, i) => ({ id: `del-${i}` }),
            );
          }
          return [];
        };
        return {
          returning: async () => run(),
          then: (
            onFulfilled?: (value: unknown[]) => unknown,
            onRejected?: (reason: unknown) => unknown,
          ) => Promise.resolve(run()).then(onFulfilled, onRejected),
        };
      },
    };
  }
}

/* ---------------- setup + call helpers ---------------- */

async function setup(rows: PortalRow[]) {
  process.env.SIN_INGEST_KEY = SHARED_KEY;
  process.env.SIN_ADMIN_KEY = ADMIN_KEY;
  process.env.API_KEY_PEPPER = PEPPER;
  process.env.SIN_TOKEN_PRIVATE_KEY = POOL_PRIVATE_B64;
  const mod = await import("../src/sin/index.ts");
  const fake = new FakeExchangeDb(rows);
  mod.setDbResolverForTests(() => fake as unknown as SinDb);
  mod.resetRateLimiterForTests();
  return { app: mod.default, fake, mod };
}

type App = {
  request: (p: string, i?: RequestInit) => Promise<Response> | Response;
};

function exchange(
  app: App,
  key: string,
  node: NodeKeys,
  opts: { withPublicKey?: boolean; body?: string } = {},
) {
  const body = opts.body ?? "";
  return app.request("/v1/auth/exchange", {
    method: "POST",
    headers: {
      "x-sin-key": key,
      ...nodeHeaders(node, "POST", "/v1/auth/exchange", body, {
        withPublicKey: opts.withPublicKey,
      }),
    },
    body,
  });
}

async function exchangeForToken(app: App, key: string, node: NodeKeys) {
  // Presenting the public key is harmless for bound portal keys and
  // required for env keys (no binding row exists for them).
  const res = await exchange(app, key, node, { withPublicKey: true });
  assert.equal(res.status, 200);
  const body = (await res.json()) as { access_token: string };
  assert.ok(body.access_token);
  return body.access_token;
}

let reportCounter = 0;
function reportBody(): string {
  reportCounter += 1;
  return JSON.stringify({
    week: WEEK,
    token: `tok${reportCounter}`.padEnd(64, "x"),
    signals: [{ type: "watchlist_add", mbid: MBID }],
  });
}

function tokenReport(app: App, token: string, node: NodeKeys, body: string) {
  return app.request("/v1/report", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      ...nodeHeaders(node, "POST", "/v1/report", body),
    },
    body,
  });
}

function tokenGet(app: App, token: string, node: NodeKeys, path: string) {
  return app.request(path, {
    headers: {
      authorization: `Bearer ${token}`,
      ...nodeHeaders(node, "GET", path, ""),
    },
  });
}

describe("sin §3A: key exchange + access tokens (fake db)", () => {
  test("public-key endpoint serves the pool key; 503 when unconfigured", async () => {
    const { app } = await setup([boundRow(makeNode())]);
    const ok = await app.request("/v1/auth/public-key");
    assert.equal(ok.status, 200);
    assert.deepEqual(await ok.json(), { public_key: POOL_PUBLIC_B64 });

    const saved = process.env.SIN_TOKEN_PRIVATE_KEY;
    delete process.env.SIN_TOKEN_PRIVATE_KEY;
    try {
      const down = await app.request("/v1/auth/public-key");
      assert.equal(down.status, 503);
      assert.deepEqual(await down.json(), { error: "token_not_configured" });
      // Exchange fails closed too.
      const node = makeNode();
      const ex = await exchange(app, RAW, node, { withPublicKey: true });
      assert.equal(ex.status, 503);
    } finally {
      process.env.SIN_TOKEN_PRIVATE_KEY = saved;
    }
  });

  test("unbound first exchange -> 202 binding_pending, no token, pending stored", async () => {
    const node = makeNode();
    const { app, fake } = await setup([portalRow()]);
    const res = await exchange(app, RAW, node, { withPublicKey: true });
    assert.equal(res.status, 202);
    const body = (await res.json()) as Record<string, unknown>;
    assert.deepEqual(body, { status: "binding_pending" });
    assert.ok(!("access_token" in body));
    const row = fake.portalRows[0];
    assert.equal(row.pendingNodeId, node.nodeId);
    assert.equal(row.pendingNodePublicKey, node.publicB64);
    assert.ok(row.pendingRequestedAt instanceof Date);
    assert.equal(row.boundNodePublicKey, null);
  });

  test("pending then approved (row flipped to bound) -> token issued", async () => {
    const node = makeNode();
    const { app, fake } = await setup([portalRow()]);
    const first = await exchange(app, RAW, node, { withPublicKey: true });
    assert.equal(first.status, 202);
    // Simulate the portal approval (§3A.3): pending becomes bound.
    const row = fake.portalRows[0];
    row.boundNodeId = row.pendingNodeId;
    row.boundNodePublicKey = row.pendingNodePublicKey;
    row.boundAt = new Date();
    row.pendingNodeId = null;
    row.pendingNodePublicKey = null;
    row.pendingRequestedAt = null;

    const res = await exchange(app, RAW, node);
    assert.equal(res.status, 200);
    const body = (await res.json()) as {
      access_token: string;
      expires_at: string;
      scopes: string[];
      key_prefix: string;
      node_id: string;
    };
    assert.ok(body.access_token);
    assert.equal(body.key_prefix, RAW.slice(0, 12));
    assert.equal(body.node_id, node.nodeId);
    assert.deepEqual(body.scopes, [
      "report:write",
      "index:read",
      "playlists:read",
      "whoami:read",
    ]);
    // The token is a pool-signed JWS with the contract's claims.
    const [h, p] = body.access_token.split(".");
    assert.deepEqual(JSON.parse(Buffer.from(h, "base64url").toString()), {
      alg: "EdDSA",
      typ: "SIN",
    });
    const claims = JSON.parse(Buffer.from(p, "base64url").toString());
    assert.equal(claims.key_id, "key-1");
    assert.equal(claims.node_id, node.nodeId);
    assert.equal(claims.exp - claims.iat, 86400);
    assert.ok(Math.abs(Date.parse(body.expires_at) / 1000 - claims.exp) < 2);
  });

  test("happy path: bound key -> token -> signed report 201 + gated pulls 200", async () => {
    const node = makeNode();
    const { app, fake, mod } = await setup([boundRow(node)]);
    await mod.publishPlaylists(
      fake as unknown as SinDb,
      {
        week: WEEK,
        playlists: [
          {
            key: "trending",
            name: "Trending in SoulSync",
            tracks: [
              { id: "t1", name: "Track t1", artists: [{ name: "Some Artist" }] },
            ],
          },
        ],
      },
      new Date(),
    );
    const token = await exchangeForToken(app, RAW, node);

    const body = reportBody();
    const reported = await tokenReport(app, token, node, body);
    assert.equal(reported.status, 201);
    assert.deepEqual(await reported.json(), {
      accepted: 1,
      duplicatesIgnored: 0,
      week: WEEK,
    });
    assert.equal(fake.rawRows.length, 1);
    // P7: the stored row carries nothing about the credential, token,
    // signature, or binding.
    assert.deepEqual(Object.keys(fake.rawRows[0]).sort(), [
      "mbid",
      "receivedAt",
      "signal",
      "tokenHash",
      "week",
    ]);

    const playlists = await tokenGet(app, token, node, "/v1/playlists");
    assert.equal(playlists.status, 200);
    const pl = (await playlists.json()) as { week: string; playlists: unknown[] };
    assert.equal(pl.week, WEEK);
    assert.equal(pl.playlists.length, 1);

    const index = await tokenGet(app, token, node, "/v1/index/trending");
    assert.equal(index.status, 200);

    const whoami = await tokenGet(app, token, node, "/v1/whoami");
    assert.equal(whoami.status, 200);
    assert.deepEqual(await whoami.json(), {
      ok: true,
      keyPrefix: RAW.slice(0, 12),
    });
  });

  test("gated reads: token without a node signature, or no credential, gets 401", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    const token = await exchangeForToken(app, RAW, node);
    const unsigned = await app.request("/v1/playlists", {
      headers: { authorization: `Bearer ${token}` },
    });
    assert.equal(unsigned.status, 401);
    const anon = await app.request("/v1/playlists");
    assert.equal(anon.status, 401);
    const anonIndex = await app.request("/v1/index/trending");
    assert.equal(anonIndex.status, 401);
    // Legacy key door still works for reads (official node/transition).
    const legacy = await app.request("/v1/playlists", {
      headers: { "x-sin-key": SHARED_KEY },
    });
    assert.equal(legacy.status, 200);
  });

  test("wrong-node signature: a different node cannot exchange or use the token", async () => {
    const nodeA = makeNode();
    const nodeB = makeNode();
    const { app } = await setup([boundRow(nodeA)]);
    // Binding mismatch at exchange: node B presents node A's bound key.
    const ex = await exchange(app, RAW, nodeB, { withPublicKey: true });
    assert.equal(ex.status, 401);
    assert.deepEqual(await ex.json(), { error: "unauthorized" });
    // And node B cannot spend node A's token even with its own valid
    // signature (token pins node A's id and binding).
    const token = await exchangeForToken(app, RAW, nodeA);
    const body = reportBody();
    const spent = await app.request("/v1/report", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        ...nodeHeaders(nodeB, "POST", "/v1/report", body),
      },
      body,
    });
    assert.equal(spent.status, 401);
  });

  test("a second node cannot displace a pending binding", async () => {
    const nodeA = makeNode();
    const nodeB = makeNode();
    const row = portalRow({
      pendingNodeId: nodeA.nodeId,
      pendingNodePublicKey: nodeA.publicB64,
      pendingRequestedAt: new Date(),
    });
    const { app, fake } = await setup([row]);
    const res = await exchange(app, RAW, nodeB, { withPublicKey: true });
    assert.equal(res.status, 401);
    assert.equal(fake.portalRows[0].pendingNodeId, nodeA.nodeId);
  });

  test("revoked key kills a live token on the next call", async () => {
    const node = makeNode();
    const { app, fake } = await setup([boundRow(node)]);
    const token = await exchangeForToken(app, RAW, node);
    const ok = await tokenReport(app, token, node, reportBody());
    assert.equal(ok.status, 201);
    fake.portalRows[0].revokedAt = new Date();
    const dead = await tokenReport(app, token, node, reportBody());
    assert.equal(dead.status, 401);
    // Unbind kills too (binding no longer matches the token's node).
    fake.portalRows[0].revokedAt = null;
    fake.portalRows[0].boundNodePublicKey = null;
    const unbound = await tokenReport(app, token, node, reportBody());
    assert.equal(unbound.status, 401);
  });

  test("expired token -> 401; tampered-body signature -> 401", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    const privateKey = loadPoolTokenPrivateKey(POOL_PRIVATE_B64);
    assert.ok(privateKey);
    const stale = issueAccessToken(
      privateKey,
      {
        keyId: "key-1",
        nodeId: node.nodeId,
        scopes: ["report:write", "index:read", "playlists:read", "whoami:read"],
      },
      new Date(Date.now() - 25 * 3600 * 1000),
    );
    const expired = await tokenReport(app, stale.accessToken, node, reportBody());
    assert.equal(expired.status, 401);

    const token = await exchangeForToken(app, RAW, node);
    const signedBody = reportBody();
    const tampered = reportBody();
    const res = await app.request("/v1/report", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        ...nodeHeaders(node, "POST", "/v1/report", signedBody),
      },
      body: tampered,
    });
    assert.equal(res.status, 401);
  });

  test("nonce replay -> 401; stale timestamp -> 401", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    const token = await exchangeForToken(app, RAW, node);
    const body = reportBody();
    const headers = {
      authorization: `Bearer ${token}`,
      ...nodeHeaders(node, "POST", "/v1/report", body),
    };
    const first = await app.request("/v1/report", {
      method: "POST",
      headers,
      body,
    });
    assert.equal(first.status, 201);
    const replay = await app.request("/v1/report", {
      method: "POST",
      headers,
      body,
    });
    assert.equal(replay.status, 401);

    const stale = await app.request("/v1/auth/exchange", {
      method: "POST",
      headers: {
        "x-sin-key": RAW,
        ...nodeHeaders(node, "POST", "/v1/auth/exchange", "", {
          timestamp: Math.floor(Date.now() / 1000) - 3600,
        }),
      },
      body: "",
    });
    assert.equal(stale.status, 401);
  });

  test("exchange rate limit: 10 per hour per key/node, then 429", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    for (let i = 0; i < 10; i += 1) {
      const res = await exchange(app, RAW, node);
      assert.equal(res.status, 200, `exchange ${i + 1}`);
    }
    const eleventh = await exchange(app, RAW, node);
    assert.equal(eleventh.status, 429);
    assert.deepEqual(await eleventh.json(), { error: "rate_limited" });
  });

  test("admin exchange -> admin-scope token can aggregate + publish; user token cannot", async () => {
    const node = makeNode();
    const { app, fake } = await setup([boundRow(node)]);
    // 15 distinct nodes' worth of raw rows for aggregateOnce to roll up.
    for (let i = 0; i < 15; i += 1) {
      fake.rawRows.push({
        week: WEEK,
        tokenHash: `tok-${i}`,
        signal: "watchlist_add",
        mbid: MBID,
        receivedAt: new Date(),
      });
    }
    const adminToken = await exchangeForToken(app, ADMIN_KEY, node);
    const aggBody = "{}";
    // Env-key tokens have no binding row, so the node presents its
    // public key on data calls too (the token's node id pins its hash).
    const aggregated = await app.request("/v1/aggregate", {
      method: "POST",
      headers: {
        authorization: `Bearer ${adminToken}`,
        ...nodeHeaders(node, "POST", "/v1/aggregate", aggBody, {
          withPublicKey: true,
        }),
      },
      body: aggBody,
    });
    assert.equal(aggregated.status, 200);
    const agg = (await aggregated.json()) as { aggregatesWritten: number };
    assert.equal(agg.aggregatesWritten, 1);
    assert.equal(fake.aggregateRows.length, 1);
    assert.equal(fake.aggregateRows[0].nodeCount, 15);

    // Admin token can also publish the shared playlists.
    const publishBody = JSON.stringify({
      week: WEEK,
      playlists: [{ key: "trending", name: "Trending in SoulSync", tracks: [] }],
    });
    const published = await app.request("/v1/playlists", {
      method: "POST",
      headers: {
        authorization: `Bearer ${adminToken}`,
        ...nodeHeaders(node, "POST", "/v1/playlists", publishBody, {
          withPublicKey: true,
        }),
      },
      body: publishBody,
    });
    assert.equal(published.status, 201);

    // A user token lacks the admin scopes: 401 on both admin routes.
    const userToken = await exchangeForToken(app, RAW, node);
    const denied = await app.request("/v1/aggregate", {
      method: "POST",
      headers: {
        authorization: `Bearer ${userToken}`,
        ...nodeHeaders(node, "POST", "/v1/aggregate", aggBody),
      },
      body: aggBody,
    });
    assert.equal(denied.status, 401);
  });

  test("exchange with an unknown or revoked key -> undifferentiated 401", async () => {
    const node = makeNode();
    const revoked = boundRow(node);
    revoked.revokedAt = new Date();
    const { app } = await setup([revoked]);
    const unknown = await exchange(app, `sin_live_${"Z".repeat(43)}`, node, {
      withPublicKey: true,
    });
    assert.equal(unknown.status, 401);
    assert.deepEqual(await unknown.json(), { error: "unauthorized" });
    const revokedRes = await exchange(app, RAW, node);
    assert.equal(revokedRes.status, 401);
  });
});

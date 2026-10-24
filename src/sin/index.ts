/**
 * SIN central aggregator — Neon Function, slug `sin`.
 *
 * Routes:
 *   GET  /health                public
 *   GET  /v1/auth/public-key    public — the pool's token-signing key
 *   POST /v1/auth/exchange      X-SIN-Key (raw portal/admin/shared key) +
 *                               node signature → short-lived access token
 *                               (§3A.4); 202 binding_pending when a portal
 *                               key's node is not approved yet
 *   POST /v1/report             legacy X-SIN-Key (admin / shared env
 *                               key only) OR Bearer access token + node
 *                               signature; one report per token per
 *                               open week, counted on arrival
 *   GET  /v1/whoami             same doors as report — connection test
 *   POST /v1/aggregate          X-SIN-Key: admin key ONLY (SIN_ADMIN_KEY),
 *                               or an admin-scope access token
 *   GET  /v1/index/trending     authenticated (legacy key or token +
 *                               signature) since §3A.7 — reads are gated
 *   GET  /v1/index/most-wanted  same
 *   GET  /v1/index/breakouts    same
 *   GET  /v1/playlists          same — latest published canonical playlists
 *   POST /v1/playlists          X-SIN-Key: admin key ONLY — publish the
 *                               week's canonical playlist definitions
 *                               (or an admin-scope access token)
 *   GET  /v1/admin/status       X-SIN-Key: admin key ONLY — report counts
 *                               and chart keys for the daily compose job
 *
 * Key model (DESIGN-NODE.md §9 + PORTAL-DESIGN.md §3A): the same
 * X-SIN-Key header carries both env keys — the VALUE determines the
 * role. The target access path is MetaSync-only (§3A): a node exchanges
 * its raw key, signed with the Ed25519 key it is bound to, for a
 * 24-hour pool-signed token, and routine calls carry the token plus a
 * fresh node signature. The legacy direct-key door is kept for the
 * operator's env keys only (official node, development pools): a raw
 * portal key is accepted nowhere but the exchange, so it is useless
 * on any machine other than the one its owner approved. Only the official MetaSync can run
 * aggregation and publish the shared playlist definitions.
 *
 * Production hardening (neon-functions skill): this function has a public
 * HTTPS URL, so every write route authenticates BEFORE parsing the body or
 * touching Postgres, using a timing-safe comparison. If the relevant key
 * is not configured the write routes fail closed (503), never open.
 *
 * Signals are keyed by MusicBrainz IDs only. SoulID is NOT used by SIN.
 */
import { timingSafeEqual } from "node:crypto";
import { and, desc, eq, gte, inArray, isNotNull, isNull, lt, max, notInArray, or, sql } from "drizzle-orm";
import { Hono } from "hono";
import { getDb, type SinDb } from "./db.ts";
import {
  ADMIN_TOKEN_SCOPES,
  aggregateRawReports,
  base64UrlEncode,
  computeBreakouts,
  computeMostWanted,
  computeTrending,
  decodeBase64Url,
  isIsoWeekString,
  isPortalKeyValid,
  isTimestampWithinWindow,
  issueAccessToken,
  LAST_USED_TOUCH_INTERVAL_MS,
  loadPoolTokenPrivateKey,
  MAX_PLAYLISTS_BODY_BYTES,
  MAX_REPORT_BODY_BYTES,
  nodeIdForPublicKey,
  NODE_TIMESTAMP_WINDOW_SECONDS,
  PLAYLIST_KEYS,
  poolTokenPublicKeyRaw,
  portalKeyHash,
  openReportWeeks,
  reportToCountIncrements,
  REPORT_TOKEN_RETENTION_WEEKS,
  isoWeekOf,
  K_PUBLISH_THRESHOLD,
  RequestRateLimiter,
  sha256Hex,
  USER_TOKEN_SCOPES,
  validatePlaylistsPublish,
  validateReport,
  verifyAccessToken,
  verifyNodeSignature,
  type AccessTokenClaims,
  type AggregateRow,
  type CanonicalPlaylist,
  type PlaylistKey,
  type PlaylistsPublishInput,
  type ReportInput,
} from "./logic.ts";
import {
  aggregateCounts,
  aggregates,
  canonicalPlaylists,
  indexPublications,
  mbResolutions,
  portalKeys,
  rawReports,
  reportPopulations,
  reportTokens,
  sinRequestNonces,
} from "./schema.ts";
import {
  buildEditions,
  CHART_MAX_TRACKS,
  editionRepeatsBase,
  COUNT_RETENTION_WEEKS,
  interleaveByRank,
  sourcedName,
  TOP_TRACKS_KEY,
  TOP_TRACKS_NAME,
  topTracksFromRecordings,
  fillFromPublic,
  PUBLIC_FILL_CHARTS,
  type PublicFillKey,
  selectChartSpecs,
  type ChartSpec,
  type PlaylistTrack,
  type ResolvedMeta,
} from "./logic.ts";
import {
  chartTracksFor,
  ListenBrainzPopularity,
  MbResolver,
  pickTopTrackIds,
  type MbBundle,
} from "./mb.ts";
import { ListenBrainzCharts } from "./public-charts.ts";

/** Timing-safe compare of the provided header value against one expected key. */
function matchesKey(provided: string | null, expected: string | undefined): boolean {
  if (!expected || provided === null) return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.byteLength !== b.byteLength) return false;
  return timingSafeEqual(a, b);
}

/** The subset of a portal_keys row SIN is allowed to see (P3). The
 *  binding fields (§3A.3) are door-check data only (P7): they decide
 *  pass/fail at the exchange and on token calls, and are never written
 *  to any SIN data table. */
export interface PortalKeyRow {
  id: string;
  keyPrefix: string;
  lastUsedAt: Date | null;
  expiresAt: Date | null;
  revokedAt: Date | null;
  boundNodeId: string | null;
  boundNodePublicKey: string | null;
  boundAt: Date | null;
  pendingNodeId: string | null;
  pendingNodePublicKey: string | null;
  pendingRequestedAt: Date | null;
}

const PORTAL_KEY_SELECTION = {
  id: portalKeys.id,
  keyPrefix: portalKeys.keyPrefix,
  lastUsedAt: portalKeys.lastUsedAt,
  expiresAt: portalKeys.expiresAt,
  revokedAt: portalKeys.revokedAt,
  boundNodeId: portalKeys.boundNodeId,
  boundNodePublicKey: portalKeys.boundNodePublicKey,
  boundAt: portalKeys.boundAt,
  pendingNodeId: portalKeys.pendingNodeId,
  pendingNodePublicKey: portalKeys.pendingNodePublicKey,
  pendingRequestedAt: portalKeys.pendingRequestedAt,
};

/**
 * Look up a presented portal key by its peppered hash — one indexed
 * query on portal_keys.key_hash (PORTAL-DESIGN.md §3.1). Returns the row
 * (valid or not; the caller applies the §2.2 validity rule) or null when
 * no such key exists. Every failure mode collapses to the same 401 at
 * the gate — no oracle.
 */
export async function lookupPortalKey(
  db: SinDb,
  presented: string,
  pepper: string,
): Promise<PortalKeyRow | null> {
  const rows = await db
    .select(PORTAL_KEY_SELECTION)
    .from(portalKeys)
    .where(eq(portalKeys.keyHash, portalKeyHash(presented, pepper)))
    .limit(1);
  return rows[0] ?? null;
}

/**
 * Look up a portal key by id — the per-request status check behind an
 * access token (§3A.4): revoke / expiry / unbind must kill a live token
 * on its next call, so token authentication re-reads the CURRENT row
 * instead of trusting what was true at exchange time.
 */
export async function lookupPortalKeyById(
  db: SinDb,
  keyId: string,
): Promise<PortalKeyRow | null> {
  const rows = await db
    .select(PORTAL_KEY_SELECTION)
    .from(portalKeys)
    .where(eq(portalKeys.id, keyId))
    .limit(1);
  return rows[0] ?? null;
}

/**
 * Record (or refresh) a pending node binding from a first exchange
 * (§3A.3). Only ever called with a candidate the caller has already
 * signature-verified and confirmed is not displacing a different
 * pending node. Approval (pending → bound) is a portal action and
 * never happens here.
 */
export async function storePendingNodeBinding(
  db: SinDb,
  keyId: string,
  nodeId: string,
  nodePublicKey: string,
  now: Date = new Date(),
): Promise<boolean> {
  const updated = await db
    .update(portalKeys)
    .set({
      pendingNodeId: nodeId,
      pendingNodePublicKey: nodePublicKey,
      pendingRequestedAt: now,
    })
    .where(eq(portalKeys.id, keyId))
    .returning({ id: portalKeys.id });
  return updated.length > 0;
}

/**
 * Refresh a portal key's last_used_at (§3.3) — the ONLY usage trace SIN
 * keeps (P4). The 1-hour throttle lives in the WHERE clause, so no
 * Redis marker is needed. Returns true when the row was actually
 * updated. Callers treat this as fire-and-forget: its failure must
 * never fail the request it rode in on.
 */
export async function touchPortalKeyLastUsed(
  db: SinDb,
  keyId: string,
  now: Date = new Date(),
): Promise<boolean> {
  const threshold = new Date(now.getTime() - LAST_USED_TOUCH_INTERVAL_MS);
  const updated = await db
    .update(portalKeys)
    .set({ lastUsedAt: now })
    .where(
      and(
        eq(portalKeys.id, keyId),
        or(
          isNull(portalKeys.lastUsedAt),
          lt(portalKeys.lastUsedAt, threshold),
        ),
      ),
    )
    .returning({ id: portalKeys.id });
  return updated.length > 0;
}

/** Which door a request came through, and its rate-limit bucket.
 *  `viaToken` marks the §3A token door: the credential arrived as a
 *  pool-signed access token + node signature rather than a raw key, and
 *  `scopes` (token door only) is what the route checks. */
export type KeyAuth =
  | { kind: "admin"; bucket: string; viaToken?: boolean; scopes?: string[] }
  | { kind: "shared"; bucket: string; viaToken?: boolean; scopes?: string[] }
  | {
      kind: "portal";
      bucket: string;
      portal: PortalKeyRow;
      viaToken?: boolean;
      scopes?: string[];
    };

/**
 * The legacy key door (§3.1): the operator's env keys only — admin,
 * then shared. A raw PORTAL key is deliberately not accepted here: it
 * would skip the node binding entirely, so a copied key could report
 * from any machine. Portal keys go through POST /v1/auth/exchange,
 * where only the approved node gets a token. Every other key — a
 * portal key included — gets the same 401. Fails closed (503) when
 * no verification path is configured at all.
 */
export async function authenticateKey(
  request: Request,
): Promise<{ auth: KeyAuth } | { response: Response }> {
  const ingest = process.env.SIN_INGEST_KEY;
  const admin = process.env.SIN_ADMIN_KEY;
  const pepper = process.env.API_KEY_PEPPER;
  const provided = request.headers.get("x-sin-key");

  if (matchesKey(provided, admin)) {
    return { auth: { kind: "admin", bucket: "env:admin" } };
  }
  if (matchesKey(provided, ingest)) {
    return { auth: { kind: "shared", bucket: "env:shared" } };
  }

  if (!ingest && !admin && !pepper) {
    return {
      response: Response.json(
        { error: "ingest_not_configured" },
        { status: 503 },
      ),
    };
  }
  return {
    response: Response.json({ error: "unauthorized" }, { status: 401 }),
  };
}

/* ------------------------------------------------------------------ */
/* §3A — node signatures, nonces, and access tokens                    */
/*                                                                     */
/* Everything in this section is a door check (P7): signatures, token  */
/* claims and bindings decide pass/fail, feed a rate-limit bucket, and */
/* (portal keys) the last_used_at touch. None of it is passed to a     */
/* route handler's data path or written to a SIN data table.           */
/* ------------------------------------------------------------------ */

const unauthorized = (): Response =>
  Response.json({ error: "unauthorized" }, { status: 401 });

/** The node's signature headers, parsed and shape-checked. */
export interface NodeSignatureHeaders {
  nodeId: string;
  signature: Buffer;
  timestamp: string;
  timestampSeconds: number;
  nonce: string;
  /** Raw public key bytes, when the caller presented X-Node-Public-Key. */
  publicKeyRaw: Buffer | null;
  /** The presented public key exactly as sent (base64url), or null. */
  publicKeyPresented: string | null;
}

const NODE_ID_SHAPE = /^[0-9a-f]{64}$/;
const NONCE_SHAPE = /^[A-Za-z0-9_-]{16,128}$/;

/**
 * Parse X-Node-Id / X-Node-Signature / X-Node-Timestamp / X-Node-Nonce
 * (+ optional X-Node-Public-Key). Returns null for anything missing or
 * malformed — callers collapse it to the same 401 as every other auth
 * failure. Does NOT check the timestamp window or the signature itself;
 * `verifyNodeRequest` does both.
 */
export function parseNodeHeaders(request: Request): NodeSignatureHeaders | null {
  const nodeId = request.headers.get("x-node-id");
  const signatureHeader = request.headers.get("x-node-signature");
  const timestamp = request.headers.get("x-node-timestamp");
  const nonce = request.headers.get("x-node-nonce");
  if (!nodeId || !NODE_ID_SHAPE.test(nodeId)) return null;
  if (!timestamp || !/^\d+$/.test(timestamp)) return null;
  const timestampSeconds = Number(timestamp);
  if (!Number.isSafeInteger(timestampSeconds)) return null;
  if (!nonce || !NONCE_SHAPE.test(nonce)) return null;
  const signature = signatureHeader
    ? decodeBase64Url(signatureHeader, 64)
    : null;
  if (!signature) return null;
  const publicKeyPresented = request.headers.get("x-node-public-key");
  let publicKeyRaw: Buffer | null = null;
  if (publicKeyPresented !== null) {
    publicKeyRaw = decodeBase64Url(publicKeyPresented, 32);
    if (!publicKeyRaw) return null;
  }
  return {
    nodeId,
    signature,
    timestamp,
    timestampSeconds,
    nonce,
    publicKeyRaw,
    publicKeyPresented,
  };
}

/** Path + query exactly as received (the signed canonical form). */
function rawPathAndQuery(requestUrl: string): string {
  try {
    const schemeEnd = requestUrl.indexOf("://");
    if (schemeEnd === -1) return requestUrl;
    const pathStart = requestUrl.indexOf("/", schemeEnd + 3);
    return pathStart === -1 ? "/" : requestUrl.slice(pathStart);
  } catch {
    return requestUrl;
  }
}

/**
 * Verify a node signature over this request: timestamp inside the
 * ±5-minute window, then Ed25519 over the canonical string
 * (method, path+query, timestamp, nonce, SHA-256 of the raw body).
 */
export function verifyNodeRequest(
  request: Request,
  headers: NodeSignatureHeaders,
  publicKeyRaw: Uint8Array,
  bodyText: string,
  now: Date = new Date(),
): boolean {
  if (!isTimestampWithinWindow(headers.timestampSeconds, now)) return false;
  return verifyNodeSignature({
    publicKeyRaw,
    method: request.method,
    pathAndQuery: rawPathAndQuery(request.url),
    timestamp: headers.timestamp,
    nonce: headers.nonce,
    body: bodyText,
    signature: headers.signature,
  });
}

/**
 * Claim a nonce for (node_id, nonce) — single-use inside the signature
 * window (§3A.2). The insert is the claim: onConflictDoNothing means a
 * replay inserts nothing and fails. Expired rows are deleted
 * opportunistically first. Call only AFTER the signature verifies, so
 * an attacker cannot burn a victim's nonces with forged requests.
 */
export async function claimRequestNonce(
  db: SinDb,
  nodeId: string,
  nonce: string,
  now: Date = new Date(),
): Promise<boolean> {
  try {
    await db
      .delete(sinRequestNonces)
      .where(lt(sinRequestNonces.expiresAt, now));
  } catch {
    // Cleanup is opportunistic; a failure here must not fail the claim.
  }
  const inserted = await db
    .insert(sinRequestNonces)
    .values({
      nodeId,
      nonceHash: sha256Hex(nonce),
      expiresAt: new Date(
        now.getTime() + NODE_TIMESTAMP_WINDOW_SECONDS * 1000,
      ),
    })
    .onConflictDoNothing()
    .returning({ nodeId: sinRequestNonces.nodeId });
  return inserted.length > 0;
}

/** The pool's token-signing key, or null when unset/malformed. */
function poolTokenKey() {
  return loadPoolTokenPrivateKey(process.env.SIN_TOKEN_PRIVATE_KEY);
}

const tokenNotConfigured = (): Response =>
  Response.json({ error: "token_not_configured" }, { status: 503 });

/**
 * Authenticate the token door for one data route: Bearer access token
 * + fresh node signature. Steps (§3A.2/§3A.4): token signature + expiry
 * (logic.verifyAccessToken); scope for this route; then, for portal
 * tokens, the CURRENT key row — still valid, still bound to the
 * token's node — and the signature verified against the BOUND public
 * key (never a presented one). For env-key tokens (admin/shared) there
 * is no binding row, so the presented public key is required and the
 * token's node_id pins its hash. Finally the nonce is claimed, making
 * the signed request single-use.
 */
export async function authenticateToken(
  request: Request,
  bodyText: string,
  requiredScope: string,
  resolveDb: () => SinDb | null,
): Promise<{ auth: KeyAuth } | { response: Response }> {
  const privateKey = poolTokenKey();
  if (!privateKey) return { response: tokenNotConfigured() };

  const authorization = request.headers.get("authorization");
  const match = authorization ? /^Bearer\s+(\S+)$/i.exec(authorization) : null;
  if (!match) return { response: unauthorized() };
  const claims = verifyAccessToken(match[1], privateKey);
  if (!claims) return { response: unauthorized() };
  if (!claims.scopes.includes(requiredScope)) {
    return { response: unauthorized() };
  }

  const headers = parseNodeHeaders(request);
  if (!headers || headers.nodeId !== claims.node_id) {
    return { response: unauthorized() };
  }

  const db = resolveDb();
  if (!db) {
    return {
      response: Response.json(
        { error: "database_not_configured" },
        { status: 503 },
      ),
    };
  }

  try {
    if (claims.key_id === "admin" || claims.key_id === "shared") {
      // Env-key token: no binding row exists, so the presented public
      // key is the signature source and the token's node_id pins it.
      if (!headers.publicKeyRaw) return { response: unauthorized() };
      if (nodeIdForPublicKey(headers.publicKeyRaw) !== claims.node_id) {
        return { response: unauthorized() };
      }
      if (
        !verifyNodeRequest(request, headers, headers.publicKeyRaw, bodyText)
      ) {
        return { response: unauthorized() };
      }
      if (!(await claimRequestNonce(db, headers.nodeId, headers.nonce))) {
        return { response: unauthorized() };
      }
      return {
        auth:
          claims.key_id === "admin"
            ? {
                kind: "admin",
                bucket: "env:admin",
                viaToken: true,
                scopes: claims.scopes,
              }
            : {
                kind: "shared",
                bucket: "env:shared",
                viaToken: true,
                scopes: claims.scopes,
              },
      };
    }

    // Portal token: re-check the CURRENT key row on every request —
    // this is what makes revoke / expiry / unbind kill a live token
    // immediately (§3A.4), not at token expiry.
    const row = await lookupPortalKeyById(db, claims.key_id);
    if (!row || !isPortalKeyValid(row)) return { response: unauthorized() };
    if (
      !row.boundNodePublicKey ||
      row.boundNodeId !== claims.node_id ||
      row.boundNodeId !== headers.nodeId
    ) {
      return { response: unauthorized() };
    }
    const boundPublicKeyRaw = decodeBase64Url(row.boundNodePublicKey, 32);
    if (!boundPublicKeyRaw) return { response: unauthorized() };
    if (
      headers.publicKeyRaw &&
      !headers.publicKeyRaw.equals(boundPublicKeyRaw)
    ) {
      return { response: unauthorized() };
    }
    if (!verifyNodeRequest(request, headers, boundPublicKeyRaw, bodyText)) {
      return { response: unauthorized() };
    }
    if (!(await claimRequestNonce(db, headers.nodeId, headers.nonce))) {
      return { response: unauthorized() };
    }
    // Portal token calls are check-ins too (§3.3): throttled touch,
    // fire-and-forget.
    try {
      await touchPortalKeyLastUsed(db, row.id);
    } catch {
      // Never fails the request (§3.3).
    }
    return {
      auth: {
        kind: "portal",
        bucket: `portal:${row.id}`,
        portal: row,
        viaToken: true,
        scopes: claims.scopes,
      },
    };
  } catch {
    return {
      response: Response.json(
        { error: "database_unavailable" },
        { status: 503 },
      ),
    };
  }
}

/**
 * The data-route door: a Bearer token goes through the token door
 * (§3A); anything else falls back to the legacy env-key door
 * (authenticateKey), kept for the official node and development pools.
 * A malformed Bearer header never falls back — presenting a token is
 * a commitment to the token door.
 */
export async function authenticateDataRequest(
  request: Request,
  bodyText: string,
  requiredScope: string,
  resolveDb: () => SinDb | null,
): Promise<{ auth: KeyAuth } | { response: Response }> {
  const authorization = request.headers.get("authorization");
  if (authorization && /^Bearer\b/i.test(authorization)) {
    return authenticateToken(request, bodyText, requiredScope, resolveDb);
  }
  return authenticateKey(request);
}

/**
 * Admin door for POST /v1/aggregate and POST /v1/playlists: the legacy
 * admin key, or a Bearer access token carrying the route's admin scope
 * (the official MetaSync exchanges its admin credential, §3A.4). A node
 * key — raw or token — gets 401.
 */
export async function authorizeAdminRequest(
  request: Request,
  bodyText: string,
  requiredScope: "aggregate:write" | "playlists:publish",
  resolveDb: () => SinDb | null,
): Promise<{ auth: KeyAuth } | { response: Response }> {
  const authorization = request.headers.get("authorization");
  if (authorization && /^Bearer\b/i.test(authorization)) {
    return authenticateToken(request, bodyText, requiredScope, resolveDb);
  }
  const denied = requireAdminKey(request);
  if (denied) return { response: denied };
  return { auth: { kind: "admin", bucket: "env:admin" } };
}

/**
 * Admin gate for POST /v1/aggregate and POST /v1/playlists: ONLY the
 * admin key (SIN_ADMIN_KEY) passes — a node key gets 401. Fails closed
 * (503) when SIN_ADMIN_KEY is not configured.
 */
function requireAdminKey(request: Request): Response | null {
  const admin = process.env.SIN_ADMIN_KEY;
  if (!admin) {
    return Response.json(
      { error: "admin_not_configured" },
      { status: 503 },
    );
  }
  if (matchesKey(request.headers.get("x-sin-key"), admin)) return null;
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

/**
 * The app's DB resolver. Production uses the real handle from db.ts;
 * tests swap in a fake via setDbResolverForTests to drive routes
 * end-to-end without a live Postgres (the same testability pattern as
 * aggregateOnce / publishPlaylists being exported helpers).
 */
let dbResolver: () => SinDb | null = getDb;

/** Test hook: replace the DB resolver (pass null to restore the real one). */
export function setDbResolverForTests(
  resolver: (() => SinDb | null) | null,
): void {
  dbResolver = resolver ?? getDb;
}

function requireDb(): { db: SinDb } | { response: Response } {
  const db = dbResolver();
  if (!db) {
    return {
      response: Response.json(
        { error: "database_not_configured" },
        { status: 503 },
      ),
    };
  }
  return { db };
}

/** Per-isolate request rate limiter (§3.2). */
let rateLimiter = new RequestRateLimiter();

/** Test hook: reset all rate-limit windows. */
export function resetRateLimiterForTests(): void {
  rateLimiter = new RequestRateLimiter();
}

const app = new Hono();

/**
 * The deployed build, set at deploy time by neon.ts from git: the exact
 * commit of the public repository this server runs, and whether the
 * working tree had uncommitted changes. Anyone can compare it with
 * github.com/Nezreka/MetaSync — "trust us" becomes "check us".
 */
export function buildInfo(env: Record<string, string | undefined> = process.env) {
  const commit = /^[0-9a-f]{40}$/.test(env.SIN_BUILD_COMMIT ?? "") ? env.SIN_BUILD_COMMIT! : null;
  return {
    commit,
    dirty: commit ? env.SIN_BUILD_DIRTY === "true" : null,
    source: commit ? `https://github.com/Nezreka/MetaSync/tree/${commit}` : null,
    deployedAt: env.SIN_BUILD_TIME || null,
  };
}

app.get("/health", (c) =>
  c.json({ ok: true, service: "sin", version: "0.1.0", build: buildInfo() }),
);

app.get("/v1/auth/public-key", (c) => {
  // Public (§3A.7): the pool's token-signing public key, so a node can
  // verify the token it was issued. Returns no pool data. Fails closed
  // (503) when the signing key is not configured.
  const privateKey = poolTokenKey();
  const publicRaw = privateKey ? poolTokenPublicKeyRaw(privateKey) : null;
  if (!privateKey || !publicRaw) return tokenNotConfigured();
  return c.json({ public_key: base64UrlEncode(publicRaw) });
});

app.post("/v1/auth/exchange", async (c) => {
  // The key exchange (§3A.4): raw key in (X-SIN-Key, once, over TLS),
  // short-lived pool-signed access token out. The request is signed by
  // the node the key is bound to; an unbound portal key records its
  // candidate node as pending and gets NO token until a person
  // approves the binding in the portal (§3A.3).
  const privateKey = poolTokenKey();
  if (!privateKey) return tokenNotConfigured();

  const request = c.req.raw;
  const provided = request.headers.get("x-sin-key");
  const headers = parseNodeHeaders(request);
  const now = new Date();

  // Exchange tripwires (§3A.8): per key fingerprint AND per node id.
  // Both buckets count every attempt, good or bad.
  const pepper = process.env.API_KEY_PEPPER;
  const keyBucket = provided
    ? pepper &&
      !matchesKey(provided, process.env.SIN_ADMIN_KEY) &&
      !matchesKey(provided, process.env.SIN_INGEST_KEY)
      ? `key:${portalKeyHash(provided, pepper)}`
      : `key:${sha256Hex(provided)}`
    : "key:missing";
  const nodeBucket = `node:${headers?.nodeId ?? "unknown"}`;
  if (
    !rateLimiter.allowExchange(keyBucket, now) ||
    !rateLimiter.allowExchange(nodeBucket, now)
  ) {
    return c.json({ error: "rate_limited" }, 429);
  }

  if (!headers) return unauthorized();

  const bodyText = await c.req.text();

  // Identify the credential. Env keys are operator/bootstrap doors;
  // anything else must resolve to a valid portal key.
  const isAdmin = matchesKey(provided, process.env.SIN_ADMIN_KEY);
  const isShared = !isAdmin && matchesKey(provided, process.env.SIN_INGEST_KEY);
  let portalRow: PortalKeyRow | null = null;
  let db: SinDb | null = null;
  if (!isAdmin && !isShared) {
    if (!provided) return unauthorized();
    if (!pepper) {
      if (!process.env.SIN_INGEST_KEY && !process.env.SIN_ADMIN_KEY) {
        return c.json({ error: "ingest_not_configured" }, 503);
      }
      return unauthorized();
    }
    const found = requireDb();
    if ("response" in found) return found.response;
    db = found.db;
    try {
      portalRow = await lookupPortalKey(db, provided, pepper);
    } catch {
      return c.json({ error: "database_unavailable" }, 503);
    }
    if (!portalRow || !isPortalKeyValid(portalRow)) return unauthorized();
  }

  // The exchange needs the nonce store even for env credentials.
  if (!db) {
    const found = requireDb();
    if ("response" in found) return found.response;
    db = found.db;
  }

  try {
    if (portalRow && !portalRow.boundNodePublicKey) {
      // Unbound portal key: the presented public key is the candidate.
      // A different already-pending node is never displaced — the same
      // generic 401 as any other failure (no oracle, §3A.3).
      if (!headers.publicKeyRaw || !headers.publicKeyPresented) {
        return unauthorized();
      }
      const candidateNodeId = nodeIdForPublicKey(headers.publicKeyRaw);
      if (candidateNodeId !== headers.nodeId) return unauthorized();
      if (
        portalRow.pendingNodePublicKey &&
        (portalRow.pendingNodePublicKey !== headers.publicKeyPresented ||
          portalRow.pendingNodeId !== headers.nodeId)
      ) {
        return unauthorized();
      }
      if (
        !verifyNodeRequest(request, headers, headers.publicKeyRaw, bodyText, now)
      ) {
        return unauthorized();
      }
      if (!(await claimRequestNonce(db, headers.nodeId, headers.nonce, now))) {
        return unauthorized();
      }
      await storePendingNodeBinding(
        db,
        portalRow.id,
        headers.nodeId,
        headers.publicKeyPresented,
        now,
      );
      // No token until a person approves this node in the portal.
      return c.json({ status: "binding_pending" }, 202);
    }

    // Bound (portal) or operator (env) exchange: verify the signature
    // against the key the credential is tied to.
    let expectedPublicRaw: Buffer;
    if (portalRow) {
      if (
        portalRow.boundNodeId !== headers.nodeId ||
        !portalRow.boundNodePublicKey
      ) {
        return unauthorized();
      }
      const boundRaw = decodeBase64Url(portalRow.boundNodePublicKey, 32);
      if (!boundRaw) return unauthorized();
      if (headers.publicKeyRaw && !headers.publicKeyRaw.equals(boundRaw)) {
        return unauthorized();
      }
      expectedPublicRaw = boundRaw;
    } else {
      if (!headers.publicKeyRaw) return unauthorized();
      if (nodeIdForPublicKey(headers.publicKeyRaw) !== headers.nodeId) {
        return unauthorized();
      }
      expectedPublicRaw = headers.publicKeyRaw;
    }
    if (!verifyNodeRequest(request, headers, expectedPublicRaw, bodyText, now)) {
      return unauthorized();
    }
    if (!(await claimRequestNonce(db, headers.nodeId, headers.nonce, now))) {
      return unauthorized();
    }

    const scopes = isAdmin
      ? ADMIN_TOKEN_SCOPES
      : USER_TOKEN_SCOPES;
    const issued = issueAccessToken(
      privateKey,
      {
        keyId: portalRow ? portalRow.id : isAdmin ? "admin" : "shared",
        nodeId: headers.nodeId,
        scopes,
      },
      now,
    );
    if (portalRow) {
      // A successful exchange is the node's check-in (§1 step 6).
      try {
        await touchPortalKeyLastUsed(db, portalRow.id, now);
      } catch {
        // Fire-and-forget (§3.3).
      }
    }
    return c.json({
      access_token: issued.accessToken,
      token_type: "Bearer",
      expires_at: issued.expiresAt.toISOString(),
      scopes: [...scopes],
      key_prefix: portalRow ? portalRow.keyPrefix : null,
      node_id: headers.nodeId,
    });
  } catch {
    return c.json({ error: "database_unavailable" }, 503);
  }
});

app.get("/v1/whoami", async (c) => {
  // Connection test for the node (§3.4): any valid key type passes,
  // via the legacy key door or the token door. Reveals nothing the
  // caller doesn't already know.
  const gate = await authenticateDataRequest(c.req.raw, "", "whoami:read", dbResolver);
  if ("response" in gate) return gate.response;
  const { auth } = gate;
  if (!rateLimiter.allowWhoami(auth.bucket, new Date())) {
    return c.json({ error: "rate_limited" }, 429);
  }
  if (auth.kind === "portal") {
    return c.json({ ok: true, keyPrefix: auth.portal.keyPrefix });
  }
  return c.json({ ok: true });
});

app.post("/v1/report", async (c) => {
  // Auth first — before body parsing or any DB data work. The token
  // door needs the raw body to verify the node signature over it; the
  // text is read (not parsed) for that, and the same text is what gets
  // validated below. The gate's result feeds pass/fail, the rate-limit
  // bucket, and (portal keys only) the last_used_at touch. Nothing
  // about the credential enters the insert (P2/P7).
  const authorization = c.req.header("authorization");
  const tokenBodyText =
    authorization && /^Bearer\b/i.test(authorization)
      ? await c.req.text()
      : null;
  const gate = await authenticateDataRequest(
    c.req.raw,
    tokenBodyText ?? "",
    "report:write",
    dbResolver,
  );
  if ("response" in gate) return gate.response;
  const { auth } = gate;
  if (!rateLimiter.allowReport(auth.bucket, new Date())) {
    return c.json({ error: "rate_limited" }, 429);
  }

  const bodyText = tokenBodyText ?? (await c.req.text());
  if (new TextEncoder().encode(bodyText).byteLength > MAX_REPORT_BODY_BYTES) {
    return c.json({ error: "payload_too_large", maxBytes: MAX_REPORT_BODY_BYTES }, 413);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    return c.json({ error: "invalid_json" }, 400);
  }
  const validation = validateReport(parsed);
  if (!validation.ok) {
    return c.json({ error: "invalid_report", details: validation.errors }, 400);
  }
  const report = validation.value;
  // Only the week that just closed (what a node sends) or the current
  // one: nobody can backfill an old week or pre-stuff a future one.
  if (!openReportWeeks().includes(report.week)) {
    return c.json({ error: "week_not_open", openWeeks: openReportWeeks() }, 400);
  }

  const found = requireDb();
  if ("response" in found) return found.response;

  // Count on arrival (migration 0008): claim the week's token, then add
  // +1 per distinct signal to the private running counts — one
  // transaction, so a report is either fully counted or not at all.
  // Nothing per-node is stored: the token row and the counts are never
  // linked. The rows are built ONLY from the validated body, so no
  // key/account reference exists anywhere in them (P1/P2/P7).
  const accepted = await countReport(found.db, report);
  if (accepted === null) {
    return c.json({ accepted: 0, duplicate: true, week: report.week }, 200);
  }
  return c.json({ accepted, duplicate: false, week: report.week }, 201);
});

/**
 * Count one validated report: claim (week, token) in report_tokens and,
 * if the claim is new, increment aggregate_counts once per distinct
 * signal. Returns the number of signals counted, or null when this
 * token already reported this week (binary per node per week: the
 * second report counts nothing). Exported for tests.
 */
export async function countReport(
  db: SinDb,
  report: ReportInput,
): Promise<number | null> {
  const increments = reportToCountIncrements(report);
  return db.transaction(async (tx) => {
    const claimed = await tx
      .insert(reportTokens)
      .values({ week: report.week, tokenHash: report.token })
      .onConflictDoNothing()
      .returning({ week: reportTokens.week });
    if (claimed.length === 0) return null;
    const optin = report.churnOptin ? 1 : 0;
    await tx
      .insert(reportPopulations)
      .values({ week: report.week, reports: 1, churnOptin: optin })
      .onConflictDoUpdate({
        target: reportPopulations.week,
        set: {
          reports: sql`${reportPopulations.reports} + 1`,
          churnOptin: sql`${reportPopulations.churnOptin} + ${optin}`,
        },
      });
    await addToCounts(tx, increments.map((i) => ({ ...i, nodeCount: 1 })));
    return increments.length;
  });
}

/** Add node counts into aggregate_counts (chunked multi-row upserts). */
async function addToCounts(
  db: Pick<SinDb, "insert">,
  rows: { week: string; signal: string; mbid: string; nodeCount: number }[],
): Promise<void> {
  const CHUNK = 500;
  for (let i = 0; i < rows.length; i += CHUNK) {
    await db
      .insert(aggregateCounts)
      .values(rows.slice(i, i + CHUNK))
      .onConflictDoUpdate({
        target: [aggregateCounts.week, aggregateCounts.signal, aggregateCounts.mbid],
        set: { nodeCount: sql`${aggregateCounts.nodeCount} + excluded.node_count` },
      });
  }
}

/**
 * Run one aggregation (publication) pass:
 *  1. drain raw_reports rows left by the pre-0008 ingest path into the
 *     running counts (distinct tokens per item), deleting them;
 *  2. publish every running count at or above k into `aggregates`, the
 *     only table the public routes read;
 *  3. prune report tokens for weeks that can no longer be reported;
 *  4. log the run.
 * Counts are exact however reports are spread over the week: they were
 * added as reports arrived and are never recounted from a subset.
 * Exported for tests / a future Function Trigger (schedule) to call.
 */
export async function aggregateOnce(
  db: SinDb,
  opts: { week?: string; now?: Date; trigger?: string } = {},
): Promise<{ aggregatesWritten: number; rawDeleted: number; week: string | null }> {
  const now = opts.now ?? new Date();

  const drained = await db
    .delete(rawReports)
    .where(opts.week ? eq(rawReports.week, opts.week) : isNotNull(rawReports.id))
    .returning();
  if (drained.length > 0) {
    const legacy = aggregateRawReports(
      drained.map((r) => ({
        week: r.week,
        token: r.tokenHash,
        signal: r.signal as AggregateRow["signal"],
        mbid: r.mbid,
        receivedAt: r.receivedAt,
      })),
      1,
    );
    await addToCounts(db, legacy);
  }

  const publishable = await db
    .select()
    .from(aggregateCounts)
    .where(
      opts.week
        ? and(eq(aggregateCounts.week, opts.week), gte(aggregateCounts.nodeCount, K_PUBLISH_THRESHOLD))
        : gte(aggregateCounts.nodeCount, K_PUBLISH_THRESHOLD),
    );

  const AGG_CHUNK = 500;
  for (let i = 0; i < publishable.length; i += AGG_CHUNK) {
    await db
      .insert(aggregates)
      .values(
        publishable.slice(i, i + AGG_CHUNK).map((a) => ({
          week: a.week,
          signal: a.signal,
          mbid: a.mbid,
          nodeCount: a.nodeCount,
          computedAt: now,
        })),
      )
      .onConflictDoUpdate({
        target: [aggregates.week, aggregates.signal, aggregates.mbid],
        set: {
          nodeCount: sql`excluded.node_count`,
          computedAt: sql`excluded.computed_at`,
        },
      });
  }

  const oldestKeptWeek = isoWeekOf(
    new Date(now.getTime() - REPORT_TOKEN_RETENTION_WEEKS * 7 * 86_400_000),
  );
  await db.delete(reportTokens).where(lt(reportTokens.week, oldestKeptWeek));
  // Below-k counts for weeks that can no longer change are useless and
  // are the only per-item data that is not public: drop them. Everything
  // at or above k was copied to `aggregates` above, in this same pass.
  const oldestCountWeek = isoWeekOf(
    new Date(now.getTime() - COUNT_RETENTION_WEEKS * 7 * 86_400_000),
  );
  await db.delete(aggregateCounts).where(lt(aggregateCounts.week, oldestCountWeek));

  const week =
    opts.week ??
    publishable.reduce<string | null>((w, a) => (w === null || a.week > w ? a.week : w), null);
  await db.insert(indexPublications).values({
    week: week ?? "none",
    trigger: opts.trigger ?? "manual",
    aggregateCount: publishable.length,
    rawDeleted: drained.length,
    publishedAt: now,
  });

  return { aggregatesWritten: publishable.length, rawDeleted: drained.length, week };
}

app.post("/v1/aggregate", async (c) => {
  // Admin only since the §9 key split: user nodes must not be able to
  // trigger aggregation with the shared node key. The token door
  // requires the aggregate:write scope (§3A.4).
  const bodyText = await c.req.text();
  const gate = await authorizeAdminRequest(
    c.req.raw,
    bodyText,
    "aggregate:write",
    dbResolver,
  );
  if ("response" in gate) return gate.response;

  const found = requireDb();
  if ("response" in found) return found.response;

  let week: string | undefined;
  const text = bodyText;
  if (text.trim().length > 0) {
    try {
      const body = JSON.parse(text) as { week?: unknown };
      if (body.week !== undefined) {
        if (!isIsoWeekString(body.week)) {
          return c.json({ error: "week must be an ISO week string" }, 400);
        }
        week = body.week;
      }
    } catch {
      return c.json({ error: "invalid_json" }, 400);
    }
  }

  const result = await aggregateOnce(found.db, { week, trigger: "manual" });
  return c.json({ ok: true, ...result });
});

/**
 * What the daily scheduled job (.github/workflows/compose.yml) checks
 * after it composes: how many installs reported the week that closed and
 * the current one, and which charts are published for the closed week.
 * Counts and chart keys only, nothing per install. Exported for tests.
 */
export async function adminStatus(db: SinDb, now: Date = new Date()) {
  const [closed, current] = openReportWeeks(now) as [string, string];
  const previous = isoWeekOf(new Date(now.getTime() - 14 * 86_400_000));
  // Reports: one report_tokens row per accepted report (kept 3 weeks, so
  // the week before the closed one is there too: "reports stopped" means
  // it had some and the closed week has none). report_populations only began
  // counting with migration 0010; it supplies the opted-in share.
  const tokens = (
    await db.select({ week: reportTokens.week }).from(reportTokens).where(inArray(reportTokens.week, [previous, closed, current]))
  ).filter((r) => r.week === previous || r.week === closed || r.week === current);
  const reportsFor = (week: string) => tokens.filter((r) => r.week === week).length;
  const populations = (
    await db.select().from(reportPopulations).where(inArray(reportPopulations.week, [closed, current]))
  ).filter((r) => r.week === closed || r.week === current);
  const pop = (week: string) => populations.find((r) => r.week === week);
  const charts = (
    await db
      .select({ week: canonicalPlaylists.week, key: canonicalPlaylists.key, publishedAt: canonicalPlaylists.publishedAt })
      .from(canonicalPlaylists)
      .where(eq(canonicalPlaylists.week, closed))
  ).filter((r) => r.week === closed);
  const lastPublished = charts.reduce<Date | null>(
    (latest, r) => (r.publishedAt && (!latest || r.publishedAt > latest) ? r.publishedAt : latest),
    null,
  );
  return {
    now: now.toISOString(),
    weeks: { previous, closed, current },
    reports: {
      previous: reportsFor(previous),
      closed: reportsFor(closed),
      current: reportsFor(current),
      closedChurnOptin: pop(closed)?.churnOptin ?? 0,
    },
    charts: {
      week: closed,
      count: charts.length,
      keys: charts.map((r) => r.key).sort(),
      lastPublishedAt: lastPublished ? lastPublished.toISOString() : null,
    },
  };
}

app.get("/v1/admin/status", async (c) => {
  // The operator's env admin key only: this is for the scheduled job.
  const denied = requireAdminKey(c.req.raw);
  if (denied) return denied;
  const found = requireDb();
  if ("response" in found) return found.response;
  return c.json({ ok: true, ...(await adminStatus(found.db)) });
});

/* ------------------------------------------------------------------ */
/* Composer: weekly auto-publish (see logic.ts selectChartSpecs).      */
/* One pass: aggregate fresh, select chart specs, resolve up to        */
/* `budget` missing MBIDs through MusicBrainz (cached in               */
/* mb_resolutions), publish every spec whose items all resolved.       */
/* Resumable by construction: call again to continue where it left.    */
/* ------------------------------------------------------------------ */

let mbResolver = new MbResolver();
let lbPopularity = new ListenBrainzPopularity();
let lbCharts = new ListenBrainzCharts();

/** Test hook: swap the composer's outside sources (pass nothing to
 *  restore the real ones), so compose runs end-to-end with no network. */
export function setComposeSourcesForTests(sources: {
  mb?: MbResolver;
  popularity?: ListenBrainzPopularity;
  charts?: ListenBrainzCharts;
} = {}): void {
  mbResolver = sources.mb ?? new MbResolver();
  lbPopularity = sources.popularity ?? new ListenBrainzPopularity();
  lbCharts = sources.charts ?? new ListenBrainzCharts();
}
const COMPOSE_DEFAULT_BUDGET = 40;
const COMPOSE_MAX_BUDGET = 200;

/** The release groups a breakout artist contributes: ones the network
 *  itself charted this week first, then the artist's best-known. */
function artistGroupPicks(
  bundle: Extract<MbBundle, { kind: "artist" }>,
  chartedMbids: Set<string>,
): string[] {
  const charted = bundle.releaseGroupMbids.filter((g) => chartedMbids.has(g));
  const rest = bundle.releaseGroupMbids.filter((g) => !chartedMbids.has(g));
  return [...charted, ...rest].slice(0, 2);
}

app.post("/v1/compose", async (c) => {
  // Admin only, same trust as aggregation (aggregate:write scope).
  const bodyText = await c.req.text();
  const gate = await authorizeAdminRequest(c.req.raw, bodyText, "aggregate:write", dbResolver);
  if ("response" in gate) return gate.response;
  const found = requireDb();
  if ("response" in found) return found.response;
  const { db } = found;

  let budget = COMPOSE_DEFAULT_BUDGET;
  let weekArg: string | undefined;
  if (bodyText.trim().length > 0) {
    try {
      const body = JSON.parse(bodyText) as { week?: unknown; budget?: unknown };
      if (body.week !== undefined) {
        if (!isIsoWeekString(body.week)) {
          return c.json({ error: "week must be an ISO week string" }, 400);
        }
        weekArg = body.week;
      }
      if (typeof body.budget === "number" && Number.isFinite(body.budget)) {
        budget = Math.max(0, Math.min(COMPOSE_MAX_BUDGET, Math.floor(body.budget)));
      }
    } catch {
      return c.json({ error: "invalid_json" }, 400);
    }
  }

  await aggregateOnce(db, { trigger: "compose" });
  // The week that just closed: nodes report it from Monday on, and a
  // daily compose picks up late reports. (Defaulting to the latest week
  // with published counts would pin the charts to that old week for as
  // long as no newer week reaches k.)
  const week = weekArg ?? openReportWeeks()[0]!;

  const priors = await priorWeeksOf(db, week);
  const previousWeek = priors[priors.length - 1] ?? null;
  const rows = await loadAggregates(db, [week, ...priors]);
  const [population] = await db
    .select()
    .from(reportPopulations)
    .where(eq(reportPopulations.week, week));
  const networkSpecs = selectChartSpecs(rows, week, priors, previousWeek, population);
  const chartedMbids = new Set(rows.filter((r) => r.week === week).map((r) => r.mbid));

  // Cold start (logic.ts fillFromPublic): Trending and Just Landed take
  // public ListenBrainz items in the slots the network cannot fill yet.
  // Network items always rank first; a failed public read fills nothing.
  const seeds = await lbCharts.seeds();
  const specs: ChartSpec[] = networkSpecs.filter((s) => !(s.key in PUBLIC_FILL_CHARTS));
  for (const key of Object.keys(PUBLIC_FILL_CHARTS) as PublicFillKey[]) {
    const filled = fillFromPublic(key, networkSpecs.find((s) => s.key === key), seeds[key]);
    if (filled) specs.push(filled);
  }

  // Everything this week's specs reference, first-order.
  const needed = new Map<string, "release-group" | "artist">();
  for (const spec of specs) {
    for (const item of spec.items) needed.set(item.mbid, item.kind);
  }

  const bundles = new Map<string, MbBundle>();
  if (needed.size > 0) {
    const cached = await db
      .select()
      .from(mbResolutions)
      .where(inArray(mbResolutions.mbid, [...needed.keys()]));
    for (const row of cached) bundles.set(row.mbid, row.bundle);
  }

  let fetched = 0;
  const fetchOne = async (mbid: string, kind: "release-group" | "artist") => {
    if (bundles.has(mbid) || fetched >= budget) return;
    fetched += 1;
    try {
      const bundle =
        kind === "artist" ? await mbResolver.artist(mbid) : await mbResolver.releaseGroup(mbid);
      if (bundle) {
        bundles.set(mbid, bundle);
        await db
          .insert(mbResolutions)
          .values({ mbid, kind, bundle })
          .onConflictDoNothing();
      }
    } catch {
      // A failed lookup is retried on the next pass; never cached, never fatal.
    }
  };
  for (const [mbid, kind] of needed) await fetchOne(mbid, kind);
  // Second order: breakout artists' chosen release groups.
  for (const spec of specs) {
    for (const item of spec.items) {
      if (item.kind !== "artist") continue;
      const artist = bundles.get(item.mbid);
      if (!artist || artist.kind !== "artist") continue;
      for (const g of artistGroupPicks(artist, chartedMbids)) {
        await fetchOne(g, "release-group");
      }
    }
  }

  // Each charted album contributes its most popular tracks (up to
  // CHART_TRACKS_PER_ALBUM), ranked by ListenBrainz's public listen data.
  // Looked up once per album and kept in its cached bundle; same pass
  // budget as MusicBrainz. A failed lookup leaves the album on its first
  // tracks for now and is retried on the next pass.
  let popularityFetched = 0;
  for (const [mbid, bundle] of bundles) {
    if (bundle.kind !== "release-group" || bundle.topTrackIds !== undefined) continue;
    if (popularityFetched >= budget) break;
    popularityFetched += 1;
    try {
      const popularity = await lbPopularity.recordings(bundle.tracks.map((t) => t.id));
      const updated = { ...bundle, topTrackIds: pickTopTrackIds(bundle.tracks, popularity) };
      bundles.set(mbid, updated);
      await db
        .insert(mbResolutions)
        .values({ mbid, kind: "release-group", bundle: updated })
        .onConflictDoUpdate({ target: mbResolutions.mbid, set: { bundle: updated } });
    } catch {
      // Not cached: the next pass asks again.
    }
  }

  const meta = new Map<string, ResolvedMeta>();
  for (const [mbid, bundle] of bundles) {
    if (bundle.kind === "release-group") {
      meta.set(mbid, { genre: bundle.genre, year: bundle.year });
    }
  }
  // Genre/decade editions of Trending, Most Wanted and Just Landed. An
  // edition built from public fill items carries the same honest name as
  // its base ("Trending: Rock · from ListenBrainz").
  const allSpecs: ChartSpec[] = [...specs, ...buildEditions(specs, meta)];

  const published: string[] = [];
  const pending: Record<string, number> = {};
  const now = new Date();
  // Each published chart's track ids, so an edition that would replay its
  // base (bases come first in allSpecs) is skipped, and so withdrawn.
  const publishedIds = new Map<string, string[]>();
  for (const spec of allSpecs) {
    // Each item's tracks, best first; laid out round by round below.
    const perItem: PlaylistTrack[][] = [];
    let missing = 0;
    let networkIncluded = 0;
    let publicIncluded = 0;
    for (const item of spec.items) {
      const bundle = bundles.get(item.mbid);
      if (!bundle) {
        // A public fill item MusicBrainz has not resolved (yet, or ever:
        // no official release) is skipped, not waited for — the network's
        // own items are the ones a chart must never publish without.
        if (item.source !== "public") missing += 1;
        continue;
      }
      const groups =
        bundle.kind === "release-group"
          ? [bundle]
          : artistGroupPicks(bundle, chartedMbids)
              .map((g) => bundles.get(g))
              .filter((b): b is Extract<MbBundle, { kind: "release-group" }> => b?.kind === "release-group");
      if (bundle.kind === "artist" && groups.length === 0) {
        missing += 1;
        continue;
      }
      // An artist item (Breakouts) interleaves its albums' picks too.
      perItem.push(interleaveByRank(groups.map((g) => chartTracksFor(g))));
      if (item.source === "public") publicIncluded += 1;
      else networkIncluded += 1;
    }
    const capped = interleaveByRank(perItem, CHART_MAX_TRACKS);
    if (missing > 0 || capped.length === 0) {
      pending[spec.key] = missing;
      continue;
    }
    const ids = capped.map((t) => t.id);
    if (spec.base && editionRepeatsBase(ids, publishedIds.get(spec.base) ?? [])) continue;
    publishedIds.set(spec.key, ids);
    const name = sourcedName(
      spec.key in PUBLIC_FILL_CHARTS ? PUBLIC_FILL_CHARTS[spec.key as PublicFillKey] : spec.name,
      networkIncluded,
      publicIncluded,
    );
    await db
      .insert(canonicalPlaylists)
      .values({ week, key: spec.key, name, tracks: capped, publishedAt: now })
      .onConflictDoUpdate({
        target: [canonicalPlaylists.week, canonicalPlaylists.key],
        set: { name, tracks: capped, publishedAt: now },
      });
    published.push(spec.key);
  }

  // Top Tracks · from ListenBrainz: the public weekly song chart, no
  // lookups needed (the rows carry titles, artists and covers).
  const topTracks = topTracksFromRecordings(seeds.recordings);
  if (topTracks.length > 0) {
    await db
      .insert(canonicalPlaylists)
      .values({ week, key: TOP_TRACKS_KEY, name: TOP_TRACKS_NAME, tracks: topTracks, publishedAt: now })
      .onConflictDoUpdate({
        target: [canonicalPlaylists.week, canonicalPlaylists.key],
        set: { name: TOP_TRACKS_NAME, tracks: topTracks, publishedAt: now },
      });
    published.push(TOP_TRACKS_KEY);
  }

  // A chart this week's pass no longer produces (an edition whose genre
  // fell under 8 albums, say) is withdrawn rather than served stale.
  // Pending charts keep their last good version until they resolve, and a
  // pass that produced nothing at all (ListenBrainz down on an empty
  // network) withdraws nothing: it says nothing about what is stale.
  const keep = [...published, ...Object.keys(pending)];
  if (keep.length > 0) {
    await db
      .delete(canonicalPlaylists)
      .where(and(eq(canonicalPlaylists.week, week), notInArray(canonicalPlaylists.key, keep)));
  }

  return c.json({ week, published, pending, resolved: bundles.size, fetched });
});

async function loadAggregates(
  db: SinDb,
  weeks: string[],
): Promise<AggregateRow[]> {
  if (weeks.length === 0) return [];
  const rows = await db
    .select()
    .from(aggregates)
    .where(inArray(aggregates.week, weeks));
  return rows.map((r) => ({
    week: r.week,
    signal: r.signal as AggregateRow["signal"],
    mbid: r.mbid,
    nodeCount: r.nodeCount,
  }));
}

/** Resolve ?week= or fall back to the latest week present in aggregates. */
async function resolveWeek(
  db: SinDb,
  requested: string | undefined,
): Promise<string | null> {
  if (requested) {
    if (!isIsoWeekString(requested)) return null;
    return requested;
  }
  const [row] = await db
    .select({ latest: max(aggregates.week) })
    .from(aggregates);
  return row?.latest ?? null;
}

/** The up-to-3 distinct weeks immediately before `week` that have data. */
async function priorWeeksOf(db: SinDb, week: string): Promise<string[]> {
  const rows = await db
    .selectDistinct({ week: aggregates.week })
    .from(aggregates)
    .where(lt(aggregates.week, week))
    .orderBy(desc(aggregates.week))
    .limit(3);
  return rows.map((r) => r.week).reverse();
}

app.get("/v1/index/trending", async (c) => {
  // Reads are gated (§3A.7): legacy key or token + node signature.
  const gate = await authenticateDataRequest(c.req.raw, "", "index:read", dbResolver);
  if ("response" in gate) return gate.response;
  const found = requireDb();
  if ("response" in found) return found.response;
  const { db } = found;
  const week = await resolveWeek(db, c.req.query("week"));
  if (!week) return c.json({ week: null, items: [] });
  const priors = await priorWeeksOf(db, week);
  const rows = await loadAggregates(db, [week, ...priors]);
  return c.json({ week, items: computeTrending(rows, week, priors) });
});

app.get("/v1/index/most-wanted", async (c) => {
  const gate = await authenticateDataRequest(c.req.raw, "", "index:read", dbResolver);
  if ("response" in gate) return gate.response;
  const found = requireDb();
  if ("response" in found) return found.response;
  const { db } = found;
  const week = await resolveWeek(db, c.req.query("week"));
  if (!week) return c.json({ week: null, items: [] });
  const rows = await loadAggregates(db, [week]);
  return c.json({ week, items: computeMostWanted(rows, week) });
});

app.get("/v1/index/breakouts", async (c) => {
  const gate = await authenticateDataRequest(c.req.raw, "", "index:read", dbResolver);
  if ("response" in gate) return gate.response;
  const found = requireDb();
  if ("response" in found) return found.response;
  const { db } = found;
  const week = await resolveWeek(db, c.req.query("week"));
  if (!week) return c.json({ week: null, items: [] });
  const priors = await priorWeeksOf(db, week);
  const previousWeek = priors[priors.length - 1];
  if (!previousWeek) return c.json({ week, items: [] });
  const rows = await loadAggregates(db, [week, previousWeek]);
  return c.json({ week, items: computeBreakouts(rows, week, previousWeek) });
});

/** Shape of a published week as served by GET /v1/playlists. */
export interface PublishedPlaylists {
  week: string | null;
  playlists: CanonicalPlaylist[];
}

/** Canonical key order for serving a week's playlists: flagship keys in
 *  PLAYLIST_KEYS order first, then composer edition keys alphabetically. */
function sortPlaylists(playlists: CanonicalPlaylist[]): CanonicalPlaylist[] {
  const order = (key: string) => {
    const i = (PLAYLIST_KEYS as readonly string[]).indexOf(key);
    return i === -1 ? PLAYLIST_KEYS.length : i;
  };
  return [...playlists].sort(
    (a, b) => order(a.key) - order(b.key) || a.key.localeCompare(b.key),
  );
}

/**
 * Upsert one week's canonical playlist definitions (one row per key).
 * Exported for tests, like aggregateOnce. Returns what was stored.
 */
export async function publishPlaylists(
  db: SinDb,
  input: PlaylistsPublishInput,
  now: Date = new Date(),
): Promise<PublishedPlaylists> {
  for (const playlist of input.playlists) {
    await db
      .insert(canonicalPlaylists)
      .values({
        week: input.week,
        key: playlist.key,
        name: playlist.name,
        tracks: playlist.tracks,
        publishedAt: now,
      })
      .onConflictDoUpdate({
        target: [canonicalPlaylists.week, canonicalPlaylists.key],
        set: {
          name: playlist.name,
          tracks: playlist.tracks,
          publishedAt: now,
        },
      });
  }
  return loadPlaylistsForWeek(db, input.week);
}

/** All playlists stored for one week, in canonical key order. */
export async function loadPlaylistsForWeek(
  db: SinDb,
  week: string,
): Promise<PublishedPlaylists> {
  // The table is tiny by design (at most 3 rows per week), so read +
  // filter in JS rather than pushing a WHERE through the query builder.
  const rows = await db.select().from(canonicalPlaylists);
  const playlists = rows
    .filter((r) => r.week === week)
    .map((r) => ({
      key: r.key as PlaylistKey,
      name: r.name,
      tracks: r.tracks,
    }));
  return { week, playlists: sortPlaylists(playlists) };
}

/**
 * The latest published week and its playlists. Before any publish:
 * `{ week: null, playlists: [] }` — an honest empty, never a fake week.
 */
export async function loadLatestPlaylists(
  db: SinDb,
): Promise<PublishedPlaylists> {
  const rows = await db.select().from(canonicalPlaylists);
  if (rows.length === 0) return { week: null, playlists: [] };
  let latest = rows[0]!.week;
  for (const r of rows) if (r.week > latest) latest = r.week;
  const playlists = rows
    .filter((r) => r.week === latest)
    .map((r) => ({
      key: r.key as PlaylistKey,
      name: r.name,
      tracks: r.tracks,
    }));
  return { week: latest, playlists: sortPlaylists(playlists) };
}

app.get("/v1/playlists", async (c) => {
  // Reads are gated (§3A.7): legacy key or token + node signature.
  const gate = await authenticateDataRequest(c.req.raw, "", "playlists:read", dbResolver);
  if ("response" in gate) return gate.response;
  const found = requireDb();
  if ("response" in found) return found.response;
  return c.json(await loadLatestPlaylists(found.db));
});

app.post("/v1/playlists", async (c) => {
  // Admin only: publishing the shared playlist definitions is reserved
  // for the official MetaSync. A node key — raw or token — gets 401
  // here, by design. The token door requires playlists:publish.
  const bodyText = await c.req.text();
  const gate = await authorizeAdminRequest(
    c.req.raw,
    bodyText,
    "playlists:publish",
    dbResolver,
  );
  if ("response" in gate) return gate.response;
  if (
    new TextEncoder().encode(bodyText).byteLength > MAX_PLAYLISTS_BODY_BYTES
  ) {
    return c.json(
      { error: "payload_too_large", maxBytes: MAX_PLAYLISTS_BODY_BYTES },
      413,
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    return c.json({ error: "invalid_json" }, 400);
  }
  const validation = validatePlaylistsPublish(parsed);
  if (!validation.ok) {
    return c.json(
      { error: "invalid_playlists", details: validation.errors },
      400,
    );
  }

  const found = requireDb();
  if ("response" in found) return found.response;

  const stored = await publishPlaylists(found.db, validation.value);
  return c.json(stored, 201);
});

// 404 for anything else so unknown routes never fall through silently.
app.notFound((c) => c.json({ error: "not_found" }, 404));

export default app;

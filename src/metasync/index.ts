/**
 * MetaSync metadata service — Neon Function, slug `metasync`.
 *
 * The metadata network's ingest (DESIGN-METADATA.md §5/§6/§12):
 *
 *   POST /v1/meta/claims   Bearer pool token (scope meta:write) +
 *                          §3A.2 node request signature + a signed
 *                          claim batch. Contributor-gated (§12).
 *   GET  /health           readiness of the ingest doors.
 *
 * This function is deliberately NOT part of SIN: separate tables, and
 * the one place account resolution (token → key → account, for the
 * contributor gate and the account blind) is allowed to happen — the
 * §12 amendment scopes that exception to this service's ingest path.
 * SIN's P-rules are untouched: no SIN table is read or written here
 * (sin_request_nonces is shared §3A.2 replay infrastructure, not SIN
 * data).
 *
 * What ingest does NOT do: publish anything. Claims land raw in
 * meta_claims; the Python compiler worker re-validates (SoulID
 * recompute included), verifies against providers, merges, and
 * publishes snapshots. Stored ≠ trusted ≠ published.
 *
 * The Oct 22 scaffold's GET /v1/records/:soulId placeholder is
 * deleted (design step 10): SoulIDs are computed, never registered,
 * so there is no per-record lookup to serve.
 */
import { and, eq, gte, lt, or, sql } from "drizzle-orm";
import { Hono } from "hono";
import {
  decodeBase64Url,
  isPortalKeyValid,
  loadPoolTokenPrivateKey,
  NODE_TIMESTAMP_WINDOW_SECONDS,
  sha256Hex,
  verifyAccessToken,
} from "../sin/logic.ts";
import { getDb, type MetaDb } from "./db.ts";
import {
  accountBlind,
  batchDigest,
  ClaimError,
  dailyAllowance,
  parseNodeHeaders,
  RETENTION_MS,
  validateBatch,
  valueHash,
  verifyBatchSignature,
  verifyNodeRequest,
  type Batch,
} from "./logic.ts";
import {
  metaBatches,
  metaClaims,
  portalKeys,
  portalUsers,
  sinRequestNonces,
} from "./schema.ts";

const MAX_BODY_BYTES = 32 * 1024 * 1024; // a 50k-claim backfill batch
const INSERT_CHUNK = 500;

const app = new Hono();

let dbResolver: () => MetaDb | null = getDb;
/** Test seam, same pattern as src/sin/index.ts. */
export function setDbResolverForTests(
  resolver: (() => MetaDb | null) | null,
): void {
  dbResolver = resolver ?? getDb;
}

const unauthorized = () => Response.json({ error: "unauthorized" }, { status: 401 });

app.get("/health", (c) =>
  c.json({
    ok: true,
    service: "metasync",
    status: "live",
    doors: {
      database: dbResolver() !== null,
      token: loadPoolTokenPrivateKey(process.env.SIN_TOKEN_PRIVATE_KEY) !== null,
      blind: Boolean(process.env.META_BLIND_SECRET),
    },
  }),
);

/** Claim a §3A.2 nonce — same rule as SIN's (insert is the claim;
 *  call only AFTER the request signature verifies). The nonce must
 *  outlive the REQUEST's validity, not just the arrival instant: a
 *  timestamp up to `window` in the future keeps its request valid
 *  until ts + window, so expiry is max(now, ts) + window — with
 *  now + window a replay could slip in after the nonce row aged out
 *  while its signature was still good. (SIN's copy has the same
 *  shape; fixing it there is a separate follow-up.) */
async function claimRequestNonce(
  db: MetaDb,
  nodeId: string,
  nonce: string,
  timestampSeconds: number,
  now: Date = new Date(),
): Promise<boolean> {
  try {
    await db
      .delete(sinRequestNonces)
      .where(lt(sinRequestNonces.expiresAt, now));
  } catch {
    // Opportunistic cleanup; never fails the claim.
  }
  const expiresAt = new Date(
    (Math.max(Math.floor(now.getTime() / 1000), timestampSeconds) +
      NODE_TIMESTAMP_WINDOW_SECONDS) * 1000,
  );
  const inserted = await db
    .insert(sinRequestNonces)
    .values({ nodeId, nonceHash: sha256Hex(nonce), expiresAt })
    .onConflictDoNothing()
    .returning({ nodeId: sinRequestNonces.nodeId });
  return inserted.length > 0;
}

interface DoorResult {
  keyRow: typeof portalKeys.$inferSelect;
  user: typeof portalUsers.$inferSelect;
  blind: string;
  nodeId: string;
  boundPublicKey: string;
  boundPublicKeyRaw: Buffer;
}

/**
 * The ingest door, in order: token (signature, expiry, meta:write
 * scope) → node headers match the token → CURRENT portal key row
 * (valid, bound to this node) → node request signature against the
 * BOUND key → nonce → contributor gate. Every auth failure collapses
 * to the same 401 (no oracle); the contributor gate is the one 403,
 * because an authenticated-but-unapproved contributor should learn
 * exactly that and nothing else.
 */
async function authenticateClaimsRequest(
  request: Request,
  bodyText: string,
  db: MetaDb,
): Promise<{ door: DoorResult } | { response: Response }> {
  const privateKey = loadPoolTokenPrivateKey(process.env.SIN_TOKEN_PRIVATE_KEY);
  if (!privateKey) {
    return { response: Response.json({ error: "token_not_configured" }, { status: 503 }) };
  }
  const blindSecret = process.env.META_BLIND_SECRET;
  if (!blindSecret) {
    return { response: Response.json({ error: "blind_not_configured" }, { status: 503 }) };
  }
  const authorization = request.headers.get("authorization");
  const match = authorization ? /^Bearer\s+(\S+)$/i.exec(authorization) : null;
  if (!match) return { response: unauthorized() };
  const claims = verifyAccessToken(match[1], privateKey);
  if (!claims) return { response: unauthorized() };
  if (!claims.scopes.includes("meta:write")) return { response: unauthorized() };
  if (claims.key_id === "admin" || claims.key_id === "shared") {
    // Env-key tokens have no account, so no contributor gate could
    // ever pass: contributing is a portal-account act (§12).
    return { response: unauthorized() };
  }
  const headers = parseNodeHeaders(request);
  if (!headers || headers.nodeId !== claims.node_id) {
    return { response: unauthorized() };
  }
  const rows = await db
    .select()
    .from(portalKeys)
    .where(eq(portalKeys.id, claims.key_id))
    .limit(1);
  const row = rows[0];
  if (!row || !isPortalKeyValid(row)) return { response: unauthorized() };
  if (
    !row.boundNodePublicKey ||
    row.boundNodeId !== claims.node_id ||
    row.boundNodeId !== headers.nodeId
  ) {
    return { response: unauthorized() };
  }
  const boundRaw = decodeBase64Url(row.boundNodePublicKey, 32);
  if (!boundRaw) return { response: unauthorized() };
  if (headers.publicKeyRaw && !headers.publicKeyRaw.equals(boundRaw)) {
    return { response: unauthorized() };
  }
  if (!verifyNodeRequest(request, headers, boundRaw, bodyText)) {
    return { response: unauthorized() };
  }
  if (
    !(await claimRequestNonce(
      db, headers.nodeId, headers.nonce, headers.timestampSeconds,
    ))
  ) {
    return { response: unauthorized() };
  }
  const users = await db
    .select()
    .from(portalUsers)
    .where(eq(portalUsers.id, row.userId))
    .limit(1);
  const user = users[0];
  if (!user) return { response: unauthorized() };
  if (!user.contributorApproved) {
    return {
      response: Response.json(
        { error: "contributor_not_approved" },
        { status: 403 },
      ),
    };
  }
  return {
    door: {
      keyRow: row,
      user,
      blind: accountBlind(blindSecret, row.userId),
      nodeId: headers.nodeId,
      boundPublicKey: row.boundNodePublicKey,
      boundPublicKeyRaw: boundRaw,
    },
  };
}

app.post("/v1/meta/claims", async (c) => {
  const db = dbResolver();
  if (!db) {
    return c.json({ error: "database_not_configured" }, 503);
  }
  const bodyText = await c.req.text();
  if (Buffer.byteLength(bodyText, "utf-8") > MAX_BODY_BYTES) {
    return c.json({ error: "batch_too_large" }, 413);
  }

  let door: DoorResult;
  try {
    const authed = await authenticateClaimsRequest(c.req.raw, bodyText, db);
    if ("response" in authed) return authed.response;
    door = authed.door;
  } catch {
    return c.json({ error: "database_unavailable" }, 503);
  }

  let envelope: { batch?: unknown; sig?: unknown };
  try {
    envelope = JSON.parse(bodyText);
  } catch {
    return c.json({ error: "invalid_json" }, 400);
  }
  let batch: Batch;
  try {
    batch = validateBatch(envelope.batch);
  } catch (e) {
    if (e instanceof ClaimError) {
      return c.json({ error: "invalid_batch", detail: e.message }, 400);
    }
    throw e;
  }
  // The batch must be signed by the same bound node key that signed
  // the request, and its header must say so. Compared as decoded raw
  // bytes: a stored key with base64 padding must not fail the match
  // (Python's b64url_encode is always unpadded).
  const headerRaw = decodeBase64Url(batch.header.node, 32);
  if (!headerRaw || !headerRaw.equals(door.boundPublicKeyRaw)) {
    return c.json({ error: "batch_node_mismatch" }, 401);
  }
  if (
    typeof envelope.sig !== "string" ||
    !verifyBatchSignature(door.boundPublicKey, envelope.batch, envelope.sig)
  ) {
    return c.json({ error: "invalid_batch_signature" }, 401);
  }

  const digest = batchDigest(envelope.batch);
  const now = new Date();
  try {
    // The whole write path is ONE transaction: batch row, claim
    // chunks, the accepted count, and retractions commit together or
    // not at all. A failure halfway (a wedged value, a dropped
    // connection) must never leave a batch row behind that makes the
    // node's honest retry read as a duplicate while its claims and
    // retractions silently vanish.
    const outcome = await db.transaction(async (tx) => {
      // Serialize contributors per account inside the transaction so
      // the allowance check and the inserts it guards are atomic:
      // without the lock, N parallel batches each read the same
      // "used today" and all pass. TRY-lock, not lock: the pool has
      // 5 connections, and queued 50k-claim batches would tie them
      // all up waiting. Contention just means "retry in a moment".
      const lockRows = (await tx.execute(
        sql`select pg_try_advisory_xact_lock(hashtext(${door.blind})) as locked`,
      )) as unknown as { rows?: { locked: boolean }[] } | { locked: boolean }[];
      const locked = Array.isArray(lockRows)
        ? lockRows[0]?.locked
        : lockRows.rows?.[0]?.locked;
      if (!locked) return { kind: "busy" as const };

      const existing = await tx
        .select({ digest: metaBatches.digest })
        .from(metaBatches)
        .where(eq(metaBatches.digest, digest))
        .limit(1);
      if (existing.length > 0) return { kind: "duplicate" as const };

      // Daily allowance (§6): backfill window vs steady state,
      // counted on the claims a batch DECLARES (claim_count), so
      // splitting or re-sending cannot mint extra allowance.
      const startOfDay = new Date(now);
      startOfDay.setUTCHours(0, 0, 0, 0);
      const todays = await tx
        .select({ claimCount: metaBatches.claimCount })
        .from(metaBatches)
        .where(
          and(
            eq(metaBatches.accountBlind, door.blind),
            gte(metaBatches.receivedAt, startOfDay),
          ),
        );
      const usedToday = todays.reduce((sum, r) => sum + r.claimCount, 0);
      const allowance = dailyAllowance(door.user.contributorApprovedAt, now);
      if (usedToday + batch.claims.length > allowance) {
        return { kind: "allowance" as const, allowance };
      }

      const insertedBatch = await tx
        .insert(metaBatches)
        .values({
          digest,
          accountBlind: door.blind,
          claimCount: batch.claims.length,
          accepted: 0,
        })
        .onConflictDoNothing()
        .returning({ digest: metaBatches.digest });
      if (insertedBatch.length === 0) return { kind: "duplicate" as const };

      let accepted = 0;
      for (let i = 0; i < batch.claims.length; i += INSERT_CHUNK) {
        const chunk = batch.claims.slice(i, i + INSERT_CHUNK);
        const inserted = await tx
          .insert(metaClaims)
          .values(
            chunk.map((claim) => ({
              subject: claim.subject,
              kind: claim.kind,
              field: claim.field,
              value: claim.value as unknown,
              valueHash: valueHash(claim.value),
              source: claim.source,
              accountBlind: door.blind,
              batchDigest: digest,
            })),
          )
          .onConflictDoNothing()
          .returning({ id: metaClaims.id });
        accepted += inserted.length;
      }
      await tx
        .update(metaBatches)
        .set({ accepted })
        .where(eq(metaBatches.digest, digest));

      // Retractions reach back 90 days (§9); older claims are frozen
      // counts and cannot be retracted individually. Deleted in
      // chunked set-based statements, not one round trip each — the
      // account lock is held for the whole transaction.
      const cutoff = new Date(now.getTime() - RETENTION_MS);
      let retracted = 0;
      for (let i = 0; i < batch.retractions.length; i += INSERT_CHUNK) {
        const chunk = batch.retractions.slice(i, i + INSERT_CHUNK);
        const deleted = await tx
          .delete(metaClaims)
          .where(
            and(
              eq(metaClaims.accountBlind, door.blind),
              gte(metaClaims.receivedAt, cutoff),
              or(
                ...chunk.map((r) =>
                  and(
                    eq(metaClaims.subject, r.subject),
                    eq(metaClaims.field, r.field),
                    eq(metaClaims.valueHash, valueHash(r.value)),
                  ),
                ),
              ),
            ),
          )
          .returning({ id: metaClaims.id });
        retracted += deleted.length;
      }
      return { kind: "stored" as const, accepted, retracted };
    });

    if (outcome.kind === "busy") {
      return c.json({ error: "batch_in_progress" }, 409);
    }
    if (outcome.kind === "duplicate") {
      return c.json({ accepted: 0, duplicates: batch.claims.length, retracted: 0, batch: digest, duplicate: true }, 200);
    }
    if (outcome.kind === "allowance") {
      return c.json(
        { error: "daily_claim_allowance_exceeded", allowance: outcome.allowance },
        429,
      );
    }
    return c.json(
      {
        accepted: outcome.accepted,
        duplicates: batch.claims.length - outcome.accepted,
        retracted: outcome.retracted,
        batch: digest,
      },
      201,
    );
  } catch {
    return c.json({ error: "database_unavailable" }, 503);
  }
});

app.notFound((c) => c.json({ error: "not_found" }, 404));

export default app;

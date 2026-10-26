/**
 * Metadata claims ingest tests (DESIGN-METADATA §6/§9/§12).
 *
 * The centrepiece is the Python-generated fixture
 * (tests/fixtures/meta-batch-fixture.json): the TypeScript ingest
 * must reproduce its digest and verify its signature byte-for-byte,
 * or the two implementations of the claim format have diverged.
 * Everything else drives the real Hono app against FakeMetaDb.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import {
  createPrivateKey,
  generateKeyPairSync,
  sign as cryptoSign,
  type KeyObject,
} from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import {
  canonicalNodeRequest,
  issueAccessToken,
  USER_TOKEN_SCOPES,
} from "../src/sin/logic.ts";
import type { MetaDb } from "../src/metasync/db.ts";
import app, { setDbResolverForTests } from "../src/metasync/index.ts";
import {
  accountBlind,
  batchDigest,
  canonicalBytes,
  CLAIM_FIELDS,
  CLAIM_KINDS,
  CLAIM_SOURCES,
  valueHash,
  verifyBatchSignature,
} from "../src/metasync/logic.ts";
import { FakeMetaDb } from "./fake-meta-db.ts";

/* ---------------- key material ---------------- */

const pool = generateKeyPairSync("ed25519");
const POOL_PRIVATE_JWK = JSON.stringify(
  pool.privateKey.export({ format: "jwk" }),
);
const BLIND_SECRET = "test-blind-secret-0123456789";
process.env.SIN_TOKEN_PRIVATE_KEY = POOL_PRIVATE_JWK;
process.env.META_BLIND_SECRET = BLIND_SECRET;

const fixture = JSON.parse(
  readFileSync(
    new URL("./fixtures/meta-batch-fixture.json", import.meta.url),
    "utf-8",
  ),
) as {
  seed_hex: string;
  public_key_b64: string;
  node_id: string;
  envelope: { batch: unknown; sig: string };
  expected_digest: string;
  contract: {
    kinds: string[];
    fields: string[];
    sources: string[];
    reserved_sources: string[];
  };
};

const PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
const nodePrivate: KeyObject = createPrivateKey({
  key: Buffer.concat([
    PKCS8_PREFIX,
    Buffer.from(fixture.seed_hex, "hex"),
  ]),
  format: "der",
  type: "pkcs8",
});

const KEY_ID = "key-1";
const USER_ID = "user-1";
const BLIND = accountBlind(BLIND_SECRET, USER_ID);

function makeDb(opts: { approved?: boolean; approvedAt?: Date | null } = {}) {
  const fake = new FakeMetaDb();
  fake.keys.set(KEY_ID, {
    id: KEY_ID,
    userId: USER_ID,
    expiresAt: null,
    revokedAt: null,
    boundNodeId: fixture.node_id,
    boundNodePublicKey: fixture.public_key_b64,
  });
  fake.users.set(USER_ID, {
    id: USER_ID,
    contributorApproved: opts.approved ?? true,
    contributorApprovedAt:
      opts.approvedAt === undefined ? new Date() : opts.approvedAt,
  });
  setDbResolverForTests(() => fake as unknown as MetaDb);
  return fake;
}

let nonceCounter = 0;
function signedRequest(
  body: string,
  opts: { scopes?: readonly string[]; nodeId?: string; keyId?: string } = {},
): Request {
  const token = issueAccessToken(pool.privateKey, {
    keyId: opts.keyId ?? KEY_ID,
    nodeId: fixture.node_id,
    scopes: opts.scopes ?? USER_TOKEN_SCOPES,
  }).accessToken;
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = `nonce-${++nonceCounter}-abcdefgh`;
  const canonical = canonicalNodeRequest(
    "POST",
    "/v1/meta/claims",
    timestamp,
    nonce,
    body,
  );
  const sig = cryptoSign(null, Buffer.from(canonical, "utf-8"), nodePrivate);
  return new Request("http://localhost/v1/meta/claims", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "x-node-id": opts.nodeId ?? fixture.node_id,
      "x-node-timestamp": timestamp,
      "x-node-nonce": nonce,
      "x-node-signature": sig.toString("base64url"),
      "content-type": "application/json",
    },
    body,
  });
}

function envelopeFor(batch: unknown): string {
  const digest = Buffer.from(batchDigest(batch), "hex");
  const sig = cryptoSign(null, digest, nodePrivate);
  return JSON.stringify({ batch, sig: sig.toString("base64url") });
}

let subjectCounter = 0;
function freshSubject(): string {
  return `soul_${(++subjectCounter).toString(16).padStart(16, "0")}`;
}
function nameClaim(subject: string) {
  return {
    subject, kind: "track", field: "name",
    value: ["Some Artist", "Some Title"], source: "deezer",
    soulid_version: "kinded_v2",
  };
}
let batchCounter = 0;
function batchOf(claims: unknown[], retractions: unknown[] = []) {
  return {
    header: {
      version: 1,
      node: fixture.public_key_b64,
      claim_count: (claims as unknown[]).length,
      nonce: `batch-nonce-${++batchCounter}`,
    },
    claims,
    retractions,
  };
}

/* ---------------- cross-language pinning ---------------- */

describe("python fixture pinning", () => {
  test("TS digest reproduces the Python batch digest exactly", () => {
    assert.equal(batchDigest(fixture.envelope.batch), fixture.expected_digest);
  });

  test("TS verifies the Python batch signature", () => {
    assert.equal(
      verifyBatchSignature(
        fixture.public_key_b64,
        fixture.envelope.batch,
        fixture.envelope.sig,
      ),
      true,
    );
  });

  test("tampered fixture claim fails verification", () => {
    const tampered = structuredClone(fixture.envelope.batch) as {
      claims: { value: unknown }[];
    };
    tampered.claims[3].value = 999;
    assert.equal(
      verifyBatchSignature(fixture.public_key_b64, tampered, fixture.envelope.sig),
      false,
    );
  });

  test("canonical form pins key order and raw unicode", () => {
    const s = canonicalBytes({ b: 1, a: { y: [1, 2], x: "周" } }).toString("utf-8");
    assert.equal(s, '{"a":{"x":"周","y":[1,2]},"b":1}');
  });

  test("TS claim enums match the Python contract exactly", () => {
    assert.deepEqual([...CLAIM_KINDS].sort(), fixture.contract.kinds);
    assert.deepEqual([...CLAIM_FIELDS], fixture.contract.fields);
    assert.deepEqual([...CLAIM_SOURCES], fixture.contract.sources);
    assert.deepEqual(fixture.contract.reserved_sources, ["user_edit"]);
  });
});

/* ---------------- the door ---------------- */

describe("claims ingest door", () => {
  test("health reports live doors", async () => {
    makeDb();
    const res = await app.fetch(new Request("http://localhost/health"));
    assert.equal(res.status, 200);
    const body = (await res.json()) as { doors: Record<string, boolean> };
    assert.deepEqual(body.doors, { database: true, token: true, blind: true });
  });

  test("happy path: fixture batch accepted, claims stored under the blind", async () => {
    const fake = makeDb();
    const body = JSON.stringify(fixture.envelope);
    const res = await app.fetch(signedRequest(body));
    assert.equal(res.status, 201);
    const out = (await res.json()) as { accepted: number; batch: string };
    assert.equal(out.accepted, 6);
    assert.equal(out.batch, fixture.expected_digest);
    assert.equal(fake.claims.length, 6);
    assert.ok(fake.claims.every((c) => c.accountBlind === BLIND));
    assert.ok(!JSON.stringify(fake.claims).includes(USER_ID));
  });

  test("same batch again is a duplicate, never double support", async () => {
    const fake = makeDb();
    const body = JSON.stringify(fixture.envelope);
    await app.fetch(signedRequest(body));
    const res = await app.fetch(signedRequest(body));
    assert.equal(res.status, 200);
    const out = (await res.json()) as { accepted: number; duplicate?: boolean };
    assert.equal(out.accepted, 0);
    assert.equal(out.duplicate, true);
    assert.equal(fake.claims.length, 6);
  });

  test("unapproved contributor gets the one honest 403", async () => {
    makeDb({ approved: false });
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope)),
    );
    assert.equal(res.status, 403);
    const out = (await res.json()) as { error: string };
    assert.equal(out.error, "contributor_not_approved");
  });

  test("token without meta:write scope is a 401", async () => {
    makeDb();
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope), {
        scopes: ["report:write"],
      }),
    );
    assert.equal(res.status, 401);
  });

  test("no token is a 401", async () => {
    makeDb();
    const res = await app.fetch(
      new Request("http://localhost/v1/meta/claims", {
        method: "POST",
        body: JSON.stringify(fixture.envelope),
      }),
    );
    assert.equal(res.status, 401);
  });

  test("replayed nonce is a 401", async () => {
    makeDb();
    const body = JSON.stringify(fixture.envelope);
    const req = signedRequest(body);
    const first = await app.fetch(req);
    assert.equal(first.status, 201);
    const freshNonce = await app.fetch(signedRequest(body));
    assert.equal(freshNonce.status, 200); // fresh nonce: fine, duplicate batch
    // Rebuild a request reusing the FIRST request's nonce + signature.
    const ts = req.headers.get("x-node-timestamp")!;
    const nonce = req.headers.get("x-node-nonce")!;
    const sig = req.headers.get("x-node-signature")!;
    const token = issueAccessToken(pool.privateKey, {
      keyId: KEY_ID,
      nodeId: fixture.node_id,
      scopes: USER_TOKEN_SCOPES,
    }).accessToken;
    const reused = new Request("http://localhost/v1/meta/claims", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "x-node-id": fixture.node_id,
        "x-node-timestamp": ts,
        "x-node-nonce": nonce,
        "x-node-signature": sig,
      },
      body,
    });
    const res = await app.fetch(reused);
    assert.equal(res.status, 401);
  });

  test("batch signed by a different key is refused", async () => {
    makeDb();
    const other = generateKeyPairSync("ed25519");
    const batch = batchOf([nameClaim(freshSubject())]);
    const digest = Buffer.from(batchDigest(batch), "hex");
    const sig = cryptoSign(null, digest, other.privateKey);
    const body = JSON.stringify({ batch, sig: sig.toString("base64url") });
    const res = await app.fetch(signedRequest(body));
    assert.equal(res.status, 401);
  });
});

/* ---------------- ingest rules ---------------- */

describe("claims ingest rules", () => {
  test("daily allowance: steady state caps at 5000 declared claims", async () => {
    const fake = makeDb({ approvedAt: new Date(Date.now() - 40 * 86400_000) });
    fake.batches.set("seeded", {
      digest: "seeded",
      accountBlind: BLIND,
      claimCount: 4999,
      accepted: 4999,
      receivedAt: new Date(),
    });
    const two = batchOf([nameClaim(freshSubject()), nameClaim(freshSubject())]);
    const res = await app.fetch(signedRequest(envelopeFor(two)));
    assert.equal(res.status, 429);
    const one = batchOf([nameClaim(freshSubject())]);
    const ok = await app.fetch(signedRequest(envelopeFor(one)));
    assert.equal(ok.status, 201);
  });

  test("reserved user_edit source is refused at the door", async () => {
    makeDb();
    const claim = { ...nameClaim(freshSubject()), source: "user_edit" };
    const res = await app.fetch(signedRequest(envelopeFor(batchOf([claim]))));
    assert.equal(res.status, 400);
  });

  test("float claim value is refused (canonical form has no floats)", async () => {
    makeDb();
    const claim = {
      subject: freshSubject(), kind: "track", field: "duration_ms",
      value: 1.5, source: "file_tags", soulid_version: "kinded_v2",
    };
    // An honest client cannot even sign this batch (its own canonical
    // form refuses floats), so the sig here is a placeholder: batch
    // validation runs before signature verification and must 400.
    const body = JSON.stringify({
      batch: batchOf([claim]),
      sig: Buffer.alloc(64).toString("base64url"),
    });
    const res = await app.fetch(signedRequest(body));
    assert.equal(res.status, 400);
  });

  test("retraction removes a fresh claim; a frozen one stays", async () => {
    const fake = makeDb();
    const subj = freshSubject();
    const isrc = {
      subject: subj, kind: "track", field: "isrc",
      value: "GBAYE9700182", source: "deezer", soulid_version: "kinded_v2",
    };
    const first = await app.fetch(
      signedRequest(envelopeFor(batchOf([nameClaim(subj), isrc]))),
    );
    assert.equal(first.status, 201);
    // A claim received 100 days ago is a frozen count (§9).
    const frozenSubject = freshSubject();
    fake.claims.push({
      id: 9999,
      subject: frozenSubject,
      kind: "track",
      field: "isrc",
      value: "USRC10000001",
      valueHash: valueHash("USRC10000001"),
      source: "deezer",
      accountBlind: BLIND,
      batchDigest: "old-batch",
      receivedAt: new Date(Date.now() - 100 * 86400_000),
    });
    const frozenRetraction = {
      subject: frozenSubject, kind: "track", field: "isrc",
      value: "USRC10000001", source: "deezer", soulid_version: "kinded_v2",
    };
    const second = await app.fetch(
      signedRequest(
        envelopeFor(batchOf([nameClaim(freshSubject())], [isrc, frozenRetraction])),
      ),
    );
    assert.equal(second.status, 201);
    const out = (await second.json()) as { retracted: number };
    assert.equal(out.retracted, 1);
    assert.ok(!fake.claims.some((c) => c.subject === subj && c.field === "isrc"));
    assert.ok(fake.claims.some((c) => c.subject === frozenSubject));
  });

  test("a retraction cannot touch another account's claim", async () => {
    const fake = makeDb();
    const subj = freshSubject();
    fake.claims.push({
      id: 4242,
      subject: subj,
      kind: "track",
      field: "isrc",
      value: "XX0000000001",
      valueHash: valueHash("XX0000000001"),
      source: "deezer",
      accountBlind: "a-different-blind",
      batchDigest: "their-batch",
      receivedAt: new Date(),
    });
    const retraction = {
      subject: subj, kind: "track", field: "isrc",
      value: "XX0000000001", source: "deezer", soulid_version: "kinded_v2",
    };
    const res = await app.fetch(
      signedRequest(envelopeFor(batchOf([nameClaim(freshSubject())], [retraction]))),
    );
    assert.equal(res.status, 201);
    const out = (await res.json()) as { retracted: number };
    assert.equal(out.retracted, 0);
    assert.ok(fake.claims.some((c) => c.id === 4242));
  });

  test("backfill window: 5001 claims pass in the first 30 days", async () => {
    makeDb({ approvedAt: new Date() });
    const claims = Array.from({ length: 5001 }, () =>
      nameClaim(freshSubject()));
    const res = await app.fetch(signedRequest(envelopeFor(batchOf(claims))));
    assert.equal(res.status, 201);
    const out = (await res.json()) as { accepted: number };
    assert.equal(out.accepted, 5001);
  });

  test("mid-write failure rolls the whole batch back", async () => {
    const fake = makeDb();
    const claims = Array.from({ length: 501 }, () =>
      nameClaim(freshSubject()));
    const body = envelopeFor(batchOf(claims));
    fake.failAtClaimInsertCall = 2; // second chunk (claim 501) throws
    const res = await app.fetch(signedRequest(body));
    assert.equal(res.status, 503);
    assert.equal(fake.batches.size, 0);
    assert.equal(fake.claims.length, 0);
    // The honest retry is NOT a duplicate: it lands in full.
    fake.failAtClaimInsertCall = 0;
    const retry = await app.fetch(signedRequest(body));
    assert.equal(retry.status, 201);
    const out = (await retry.json()) as { accepted: number };
    assert.equal(out.accepted, 501);
  });
});

/* ---------------- gate hardening cases ---------------- */

describe("claims ingest gate hardening", () => {
  test("an env-key (admin) token can never contribute", async () => {
    makeDb();
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope), { keyId: "admin" }),
    );
    assert.equal(res.status, 401);
  });

  test("token node_id that differs from x-node-id is a 401", async () => {
    makeDb();
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope), {
        nodeId: "0".repeat(64),
      }),
    );
    assert.equal(res.status, 401);
  });

  test("revoked portal key is a 401 even with a live token", async () => {
    const fake = makeDb();
    fake.keys.get(KEY_ID)!.revokedAt = new Date();
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope)),
    );
    assert.equal(res.status, 401);
  });

  test("unbound portal key is a 401", async () => {
    const fake = makeDb();
    fake.keys.get(KEY_ID)!.boundNodePublicKey = null;
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope)),
    );
    assert.equal(res.status, 401);
  });

  test("batch header naming a different node is refused", async () => {
    makeDb();
    const other = generateKeyPairSync("ed25519");
    const otherB64 = Buffer.from(
      (other.publicKey.export({ format: "jwk" }) as { x: string }).x,
      "base64url",
    ).toString("base64url");
    const batch = batchOf([nameClaim(freshSubject())]);
    (batch.header as { node: string }).node = otherB64;
    const res = await app.fetch(signedRequest(envelopeFor(batch)));
    assert.equal(res.status, 401);
    const out = (await res.json()) as { error: string };
    assert.equal(out.error, "batch_node_mismatch");
  });

  test("padded stored key still matches (byte comparison)", async () => {
    const fake = makeDb();
    fake.keys.get(KEY_ID)!.boundNodePublicKey = fixture.public_key_b64 + "=";
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope)),
    );
    assert.equal(res.status, 201);
  });

  test("advisory lock is taken before any write in the transaction", async () => {
    const fake = makeDb();
    const res = await app.fetch(
      signedRequest(JSON.stringify(fixture.envelope)),
    );
    assert.equal(res.status, 201);
    // events[0] is the auth-phase nonce insert; the transaction's
    // first act must be the lock (execute), before the batch insert.
    assert.equal(fake.events[0], "insert");
    assert.equal(fake.events[1], "execute");
    assert.equal(fake.events[2], "insert");
  });
});

/* ---------- canonical parity with python (gate S4) ---------- */

describe("claims ingest canonical parity", () => {
  test("NUL and lone-surrogate claim values are refused", async () => {
    makeDb();
    for (const bad of ["a\u0000b", "a\ud800b"]) {
      const claim = {
        subject: freshSubject(), kind: "album", field: "label",
        value: bad, source: "discogs", soulid_version: "kinded_v2",
      };
      const body = JSON.stringify({
        batch: batchOf([claim]),
        sig: Buffer.alloc(64).toString("base64url"),
      });
      const res = await app.fetch(signedRequest(body));
      assert.equal(res.status, 400);
    }
  });

  test("python whitespace rules: \\x1c name blank, \\ufeff name kept", async () => {
    makeDb();
    const blank = nameClaim(freshSubject());
    blank.value = ["\u001c", "Some Title"];
    const refused = await app.fetch(signedRequest(envelopeFor(batchOf([blank]))));
    assert.equal(refused.status, 400);
    const kept = nameClaim(freshSubject());
    kept.value = ["\ufeff", "Some Title"];
    const ok = await app.fetch(signedRequest(envelopeFor(batchOf([kept]))));
    assert.equal(ok.status, 201);
  });

  test("512 code point astral name is accepted (not 512 utf-16 units)", async () => {
    makeDb();
    const claim = nameClaim(freshSubject());
    claim.value = ["🎵".repeat(512), "Some Title"];
    const res = await app.fetch(signedRequest(envelopeFor(batchOf([claim]))));
    assert.equal(res.status, 201);
  });

  test("missing batch nonce is a 400", async () => {
    makeDb();
    const batch = batchOf([nameClaim(freshSubject())]);
    delete (batch.header as { nonce?: string }).nonce;
    const res = await app.fetch(signedRequest(envelopeFor(batch)));
    assert.equal(res.status, 400);
  });

  test("over-cap retractions and partial retraction objects are 400s", async () => {
    makeDb();
    const over = batchOf(
      [nameClaim(freshSubject())],
      Array.from({ length: 5001 }, () => nameClaim(freshSubject())),
    );
    const res1 = await app.fetch(signedRequest(envelopeFor(over)));
    assert.equal(res1.status, 400);
    const partial = batchOf(
      [nameClaim(freshSubject())],
      [{ subject: freshSubject(), field: "isrc" }],
    );
    const res2 = await app.fetch(signedRequest(envelopeFor(partial)));
    assert.equal(res2.status, 400);
  });

  test("send, retract, re-send: the claim lands again", async () => {
    const fake = makeDb();
    const claim = nameClaim(freshSubject());
    const first = await app.fetch(signedRequest(envelopeFor(batchOf([claim]))));
    assert.equal(first.status, 201);
    const retract = await app.fetch(
      signedRequest(envelopeFor(batchOf([], [claim]))),
    );
    assert.equal(retract.status, 201);
    assert.equal(((await retract.json()) as { retracted: number }).retracted, 1);
    assert.equal(fake.claims.length, 0);
    const resend = await app.fetch(signedRequest(envelopeFor(batchOf([claim]))));
    assert.equal(resend.status, 201);
    assert.equal(((await resend.json()) as { accepted: number }).accepted, 1);
    assert.equal(fake.claims.length, 1);
  });
});

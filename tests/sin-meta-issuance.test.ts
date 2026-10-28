/**
 * SIN metadata issuance tests (DESIGN-METADATA.md §9/§10): binding
 * attestations on the token exchange and the issuer-signed revocation
 * feed — the portal halves of the metadata network.
 *
 * The verifiers below REIMPLEMENT the consumers, they do not call the
 * issuer's own helpers: the attestation check mirrors
 * node/metasync_node/attestation.py (shape rules + Ed25519 over the
 * six-line canonical text + the attestation naming THIS node's key),
 * and the list check mirrors relay/metasync_relay/revocations.py
 * (shape + signature over list_signing_text). The signing-text unit
 * tests pin the literal conformance vectors from
 * node/tests/test_meta_conformance.py byte for byte.
 *
 * Same DB-free style as the other suites (fake DB via
 * setDbResolverForTests). Run with: npm test
 */
import assert from "node:assert/strict";
import {
  createHash,
  createPublicKey,
  generateKeyPairSync,
  sign as cryptoSign,
  verify as cryptoVerify,
  type KeyObject,
} from "node:crypto";
import { describe, test } from "node:test";
import {
  attestationSigningText,
  portalKeyHash,
  revocationListSigningText,
} from "../src/sin/logic.ts";
import type { SinDb } from "../src/sin/db.ts";
import { FakeSinDb, type PortalRow } from "./fake-db.ts";

const SHARED_KEY = "test-shared-secret-0123456789";
const ADMIN_KEY = "test-admin-secret-0123456789";
const PEPPER = "test-pepper-0123456789abcdef";
const RAW = `sin_live_${"K".repeat(43)}`;
const RAW2 = `sin_live_${"L".repeat(43)}`;
const BOUND_AT = new Date("2026-10-22T00:00:00Z");
const BOUND_AT_EPOCH = Math.floor(BOUND_AT.getTime() / 1000);
const SEVEN_DAYS = 7 * 24 * 3600;

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
  x: string;
};
const POOL_PRIVATE_B64 = poolPrivJwk.d;
const POOL_PUBLIC_B64 = poolPrivJwk.x;

const poolPublicKeyObject = createPublicKey({
  key: { kty: "OKP", crv: "Ed25519", x: POOL_PUBLIC_B64 },
  format: "jwk",
});

let nonceCounter = 0;
function nextNonce(): string {
  nonceCounter += 1;
  return `nonce-${String(nonceCounter).padStart(8, "0")}-abcdefgh`;
}

function sha256Hex(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

function nodeHeaders(
  node: NodeKeys,
  method: string,
  path: string,
  body: string,
  opts: { withPublicKey?: boolean } = {},
): Record<string, string> {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = nextNonce();
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

/* ---------------- consumer verifiers (reimplemented) ---------------- */

function decodeB64UrlStrict(value: unknown, expectedLength: number): Buffer | null {
  if (typeof value !== "string") return null;
  const unpadded = value.replace(/=+$/, "");
  const bytes = Buffer.from(unpadded, "base64url");
  if (bytes.length !== expectedLength) return null;
  if (bytes.toString("base64url") !== unpadded) return null;
  return bytes;
}

/**
 * The node's attestation verification, reimplemented from
 * node/metasync_node/attestation.py: shape (int epoch times,
 * bound_at <= issued_at, window <= 14 days), the attestation naming
 * THIS node's key (raw-byte comparison), and the Ed25519 signature
 * over the six newline-terminated lines.
 */
function verifyAttestationNodeStyle(
  att: unknown,
  ownNodeKeyB64: string,
): boolean {
  if (typeof att !== "object" || att === null) return false;
  const a = att as Record<string, unknown>;
  if (typeof a.issuer !== "string" || a.issuer.length === 0) return false;
  const nodeKeyRaw = decodeB64UrlStrict(a.node_key, 32);
  const signature = decodeB64UrlStrict(a.sig, 64);
  if (!nodeKeyRaw || !signature) return false;
  for (const field of ["bound_at", "issued_at", "expires"]) {
    if (typeof a[field] !== "number" || !Number.isInteger(a[field])) {
      return false;
    }
  }
  const boundAt = a.bound_at as number;
  const issuedAt = a.issued_at as number;
  const expires = a.expires as number;
  if (boundAt < 0 || issuedAt < 0 || expires <= issuedAt) return false;
  if (expires - issuedAt > 14 * 24 * 3600) return false;
  if (boundAt > issuedAt) return false;
  const ownRaw = decodeB64UrlStrict(ownNodeKeyB64, 32);
  if (!ownRaw || !nodeKeyRaw.equals(ownRaw)) return false;
  const text =
    `metasync-attestation\n${a.issuer}\n${a.node_key}\n` +
    `${boundAt}\n${issuedAt}\n${expires}\n`;
  try {
    return cryptoVerify(null, Buffer.from(text, "utf8"), poolPublicKeyObject, signature);
  } catch {
    return false;
  }
}

const NODE_ID_SHAPE = /^[0-9a-f]{64}$/;

interface RevocationListBody {
  issuer: string;
  seq: number;
  issued_at: number;
  keys: string[];
  sig: string;
}

/**
 * The relay's revocation-list verification, reimplemented from
 * relay/metasync_relay/revocations.py: shape, then the Ed25519
 * signature over list_signing_text (context, issuer, seq, issued_at,
 * sorted de-duplicated keys, newline-terminated).
 */
function verifyRevocationListRelayStyle(list: unknown): boolean {
  if (typeof list !== "object" || list === null) return false;
  const l = list as Record<string, unknown>;
  if (typeof l.issuer !== "string" || l.issuer.length === 0) return false;
  if (typeof l.seq !== "number" || !Number.isInteger(l.seq) || l.seq < 0) {
    return false;
  }
  if (
    typeof l.issued_at !== "number" ||
    !Number.isInteger(l.issued_at) ||
    l.issued_at < 0
  ) {
    return false;
  }
  if (
    !Array.isArray(l.keys) ||
    !l.keys.every((k) => typeof k === "string" && NODE_ID_SHAPE.test(k))
  ) {
    return false;
  }
  const signature = decodeB64UrlStrict(l.sig, 64);
  if (!signature) return false;
  const sorted = [...new Set(l.keys as string[])].sort();
  const text =
    ["metasync-revocations", l.issuer, String(l.seq), String(l.issued_at), ...sorted].join(
      "\n",
    ) + "\n";
  try {
    return cryptoVerify(null, Buffer.from(text, "utf8"), poolPublicKeyObject, signature);
  } catch {
    return false;
  }
}

/* ---------------- fake DB rows + setup ---------------- */

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

function boundRow(node: NodeKeys, overrides: Partial<PortalRow> = {}): PortalRow {
  return portalRow({
    boundNodeId: node.nodeId,
    boundNodePublicKey: node.publicB64,
    boundAt: BOUND_AT,
    ...overrides,
  });
}

async function setup(rows: PortalRow[]) {
  process.env.SIN_INGEST_KEY = SHARED_KEY;
  process.env.SIN_ADMIN_KEY = ADMIN_KEY;
  process.env.API_KEY_PEPPER = PEPPER;
  process.env.SIN_TOKEN_PRIVATE_KEY = POOL_PRIVATE_B64;
  const mod = await import("../src/sin/index.ts");
  const fake = new FakeSinDb(rows);
  mod.setDbResolverForTests(() => fake as unknown as SinDb);
  mod.resetRateLimiterForTests();
  mod.resetRevocationFeedMemoForTests();
  return { app: mod.default, fake, mod };
}

type App = {
  request: (p: string, i?: RequestInit) => Promise<Response> | Response;
};

function exchange(
  app: App,
  key: string,
  node: NodeKeys,
  opts: { withPublicKey?: boolean } = {},
) {
  return app.request("/v1/auth/exchange", {
    method: "POST",
    headers: {
      "x-sin-key": key,
      ...nodeHeaders(node, "POST", "/v1/auth/exchange", "", opts),
    },
    body: "",
  });
}

/* ---------------- signing-text conformance vectors ---------------- */

describe("sin metadata issuance: pinned signing texts", () => {
  test("attestation signing text matches the node conformance vector byte for byte", () => {
    // node/tests/test_meta_conformance.py, literal.
    assert.equal(
      attestationSigningText("pool-1", "NODEKEY", 1700000000, 1700000100, 1700604800),
      "metasync-attestation\n" +
        "pool-1\n" +
        "NODEKEY\n" +
        "1700000000\n" +
        "1700000100\n" +
        "1700604800\n",
    );
  });

  test("revocation list signing text matches the relay/node vector byte for byte", () => {
    // Keys are sorted and deduplicated inside the signed text.
    assert.equal(
      revocationListSigningText("issuer-1", 42, 1700000000, [
        "b".repeat(64),
        "a".repeat(64),
        "a".repeat(64),
      ]),
      "metasync-revocations\n" +
        "issuer-1\n" +
        "42\n" +
        "1700000000\n" +
        "a".repeat(64) +
        "\n" +
        "b".repeat(64) +
        "\n",
    );
  });
});

/* ---------------- attestation issuance ---------------- */

describe("sin metadata issuance: attestation on exchange (§9)", () => {
  test("bound-key exchange carries an attestation that verifies the node way", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    const res = await exchange(app, RAW, node);
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    const att = body.attestation as Record<string, unknown>;
    assert.ok(att, "attestation present");
    assert.equal(att.issuer, "metasync");
    assert.equal(att.node_key, node.publicB64);
    // bound_at is INTEGER epoch seconds derived from the row's
    // timestamptz — never a timestamp string, never "now".
    assert.equal(typeof att.bound_at, "number");
    assert.ok(Number.isInteger(att.bound_at));
    assert.equal(att.bound_at, BOUND_AT_EPOCH);
    assert.ok(Number.isInteger(att.issued_at));
    assert.equal((att.expires as number) - (att.issued_at as number), SEVEN_DAYS);
    assert.equal(verifyAttestationNodeStyle(att, node.publicB64), true);
    // The same 200 also carries the current revocation list (§10),
    // which verifies the relay way.
    assert.equal(verifyRevocationListRelayStyle(body.revocations), true);
  });

  test("node_key is rebuilt from the decoded raw bytes, not copied from the stored column", async () => {
    const node = makeNode();
    // The stored column holds the binding-time header string exactly
    // as sent — here a PADDED base64url variant of the same key. A
    // verbatim copy would fail the node's byte-for-byte comparison.
    const row = boundRow(node, { boundNodePublicKey: `${node.publicB64}=` });
    const { app } = await setup([row]);
    const res = await exchange(app, RAW, node, { withPublicKey: true });
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    const att = body.attestation as Record<string, unknown>;
    assert.equal(att.node_key, node.publicB64);
    assert.equal(verifyAttestationNodeStyle(att, node.publicB64), true);
  });

  test("unbound key -> 202 binding_pending, no attestation; env-credential exchange -> 200, no attestation field", async () => {
    const node = makeNode();
    const { app } = await setup([portalRow()]);
    const pending = await exchange(app, RAW, node, { withPublicKey: true });
    assert.equal(pending.status, 202);
    const pendingBody = (await pending.json()) as Record<string, unknown>;
    assert.ok(!("attestation" in pendingBody));

    const env = await exchange(app, ADMIN_KEY, node, { withPublicKey: true });
    assert.equal(env.status, 200);
    const envBody = (await env.json()) as Record<string, unknown>;
    assert.ok(envBody.access_token);
    assert.ok(!("attestation" in envBody));

    const shared = await exchange(app, SHARED_KEY, node, { withPublicKey: true });
    assert.equal(shared.status, 200);
    const sharedBody = (await shared.json()) as Record<string, unknown>;
    assert.ok(!("attestation" in sharedBody));
  });

  test("a tampered attestation fails node-style verification", async () => {
    const node = makeNode();
    const { app } = await setup([boundRow(node)]);
    const res = await exchange(app, RAW, node);
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    const att = body.attestation as Record<string, unknown>;
    assert.equal(verifyAttestationNodeStyle(att, node.publicB64), true);

    // Forged standing: an earlier bound_at (shape stays legal, the
    // signature no longer covers the bytes).
    const forgedTime = { ...att, bound_at: (att.bound_at as number) - 3600 };
    assert.equal(verifyAttestationNodeStyle(forgedTime, node.publicB64), false);
    // A corrupted signature.
    const sig = att.sig as string;
    const forgedSig = {
      ...att,
      sig: (sig.startsWith("A") ? "B" : "A") + sig.slice(1),
    };
    assert.equal(verifyAttestationNodeStyle(forgedSig, node.publicB64), false);
    // An attestation naming a DIFFERENT node's key is dropped even
    // with a pool-shaped signature field.
    const other = makeNode();
    const foreign = { ...att, node_key: other.publicB64 };
    assert.equal(verifyAttestationNodeStyle(foreign, node.publicB64), false);
  });
});

/* ---------------- revocation feed ---------------- */

describe("sin metadata issuance: revocation feed (§10)", () => {
  test("bare signed object; seq stable, then increments on revoke, unbind, and rebind", async () => {
    const nodeA = makeNode();
    const nodeB = makeNode();
    const rowA = boundRow(nodeA);
    const rowB = boundRow(nodeB, {
      id: "key-2",
      keyHash: portalKeyHash(RAW2, PEPPER),
      keyPrefix: RAW2.slice(0, 12),
    });
    const { app, fake, mod } = await setup([rowA, rowB]);

    const first = await app.request("/v1/meta/revocations");
    assert.equal(first.status, 200);
    const list1 = (await first.json()) as RevocationListBody;
    // The BARE object — exactly the five wire fields, no envelope.
    assert.deepEqual(Object.keys(list1).sort(), [
      "issued_at",
      "issuer",
      "keys",
      "seq",
      "sig",
    ]);
    assert.equal(list1.issuer, "metasync");
    assert.deepEqual(list1.keys, []);
    assert.equal(verifyRevocationListRelayStyle(list1), true);

    // Nothing changed: same seq, same issued_at.
    const again = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.equal(again.seq, list1.seq);
    assert.equal(again.issued_at, list1.issued_at);
    assert.equal(again.sig, list1.sig);

    // Revoking node A's key (this codebase's key deletion) puts A's
    // node id — the lowercase-hex SHA-256 of its public key — on the
    // list and advances the sequence by exactly one. (The feed memo
    // is reset after each mutation: within its 30s TTL the route
    // would otherwise serve the pre-mutation list.)
    fake.portalRows[0]!.revokedAt = new Date();
    mod.resetRevocationFeedMemoForTests();
    const list2 = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.equal(list2.seq, list1.seq + 1);
    assert.deepEqual(list2.keys, [nodeA.nodeId]);
    assert.match(list2.keys[0]!, /^[0-9a-f]{64}$/);
    assert.equal(
      list2.keys[0],
      createHash("sha256").update(nodeA.publicRaw).digest("hex"),
    );
    assert.equal(verifyRevocationListRelayStyle(list2), true);
    // The materialized set in the (fake) DB matches what was served.
    assert.deepEqual(fake.revokedNodeRows.map((r) => r.nodeId), [nodeA.nodeId]);
    assert.equal(fake.revocationState?.seq, list2.seq);

    // Unbinding node B clears its bound_* on the row; the seen
    // record is what puts B on the list.
    fake.portalRows[1]!.boundNodeId = null;
    fake.portalRows[1]!.boundNodePublicKey = null;
    fake.portalRows[1]!.boundAt = null;
    mod.resetRevocationFeedMemoForTests();
    const list3 = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.equal(list3.seq, list2.seq + 1);
    assert.deepEqual(list3.keys, [nodeA.nodeId, nodeB.nodeId].sort());
    assert.equal(verifyRevocationListRelayStyle(list3), true);

    // Re-binding B (a fresh approval after the unbind) takes it back
    // off the list; the sequence advances again so holders notice.
    fake.portalRows[1]!.boundNodeId = nodeB.nodeId;
    fake.portalRows[1]!.boundNodePublicKey = nodeB.publicB64;
    fake.portalRows[1]!.boundAt = new Date();
    mod.resetRevocationFeedMemoForTests();
    const list4 = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.equal(list4.seq, list3.seq + 1);
    assert.deepEqual(list4.keys, [nodeA.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(list4), true);
  });

  test("a sync racing on a stale state read can never reuse a seq for a different set", async () => {
    const nodeA = makeNode();
    const nodeB = makeNode();
    const rowA = boundRow(nodeA);
    const rowB = boundRow(nodeB, {
      id: "key-2",
      keyHash: portalKeyHash(RAW2, PEPPER),
      keyPrefix: RAW2.slice(0, 12),
    });
    const { fake, mod } = await setup([rowA, rowB]);
    const build = () =>
      mod.buildRevocationList(
        fake as unknown as SinDb,
        poolPair.privateKey,
      ) as Promise<RevocationListBody>;

    const list1 = await build();
    assert.deepEqual(list1.keys, []);

    // Revoke A: the set changes and the sequence advances.
    fake.portalRows[0]!.revokedAt = new Date();
    const list2 = await build();
    assert.equal(list2.seq, list1.seq + 1);
    assert.deepEqual(list2.keys, [nodeA.nodeId]);

    // The race: portal state moves on (A un-revoked, B revoked), but
    // a racing sync still holds the PRE-A state snapshot (seq of
    // list1, the empty set's hash). Its naive read-modify-write
    // advance would land on list2's seq — signing a DIFFERENT set
    // under a seq holders have already applied list2 at, so they
    // would ignore it forever. The fake's one-shot stale-read hook
    // simulates the snapshot; the CAS advance must refuse (zero rows
    // on the hash it read), recompute against the stored state, and
    // land on the NEXT sequence.
    fake.portalRows[0]!.revokedAt = null;
    fake.portalRows[1]!.revokedAt = new Date();
    fake.staleRevocationStateReads.push({
      issuer: "metasync",
      seq: list1.seq,
      issuedAt: list1.issued_at,
      keysHash: sha256Hex(""),
    });
    const list3 = await build();
    assert.equal(list3.seq, list2.seq + 1);
    assert.deepEqual(list3.keys, [nodeB.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(list3), true);
    // The served list is the stored state: same seq, and the stored
    // hash names exactly the served set.
    assert.equal(fake.revocationState?.seq, list3.seq);
    assert.equal(
      fake.revocationState?.keysHash,
      sha256Hex([nodeB.nodeId].join("\n")),
    );

    // One seq, one set: across everything served, a sequence number
    // never repeats with a different key set.
    const bySeq = new Map<number, string>();
    for (const l of [list1, list2, list3]) {
      const prev = bySeq.get(l.seq);
      if (prev !== undefined) assert.equal(prev, l.keys.join(","));
      bySeq.set(l.seq, l.keys.join(","));
    }

    // A no-change sync does not advance the sequence and re-serves
    // the stored list byte-for-byte (same sig).
    const list4 = await build();
    assert.equal(list4.seq, list3.seq);
    assert.equal(list4.issued_at, list3.issued_at);
    assert.equal(list4.sig, list3.sig);
  });

  test("an ABA return to the same set cannot let a stale reader regress the seq", async () => {
    const nodeA = makeNode();
    const nodeB = makeNode();
    const rowA = boundRow(nodeA);
    const rowB = boundRow(nodeB, {
      id: "key-2",
      keyHash: portalKeyHash(RAW2, PEPPER),
      keyPrefix: RAW2.slice(0, 12),
    });
    const { fake, mod } = await setup([rowA, rowB]);
    const build = () =>
      mod.buildRevocationList(
        fake as unknown as SinDb,
        poolPair.privateKey,
      ) as Promise<RevocationListBody>;
    const served: RevocationListBody[] = [];

    const list1 = await build();
    served.push(list1);
    assert.deepEqual(list1.keys, []);

    // Revoke A: the sequence advances and names the {A} set.
    fake.portalRows[0]!.revokedAt = new Date();
    const list2 = await build();
    served.push(list2);
    assert.equal(list2.seq, list1.seq + 1);
    assert.deepEqual(list2.keys, [nodeA.nodeId]);

    // Un-revoke A: the EMPTY set returns — the ABA state. The live
    // row now carries the same keys hash as list1's snapshot, but at
    // a strictly higher seq.
    fake.portalRows[0]!.revokedAt = null;
    const list3 = await build();
    served.push(list3);
    assert.equal(list3.seq, list2.seq + 1);
    assert.deepEqual(list3.keys, []);
    assert.ok(list3.seq > list1.seq);
    assert.equal(fake.revocationState?.keysHash, sha256Hex(""));

    // Portal state moves on (B revoked), but a stale reader still
    // holds list1's snapshot: an OLD seq with a keys hash that
    // legitimately matches the live row. A hash-only CAS would pass
    // its predicate against the live row and write seq = stale + 1,
    // regressing the sequence and reusing list2's seq for a
    // different set. The full-snapshot CAS must refuse, recompute
    // against the live state, and land ABOVE the live seq.
    fake.portalRows[1]!.revokedAt = new Date();
    fake.staleRevocationStateReads.push({
      issuer: "metasync",
      seq: list1.seq,
      issuedAt: list1.issued_at,
      keysHash: sha256Hex(""),
    });
    const list4 = await build();
    served.push(list4);
    assert.ok(list4.seq > list3.seq);
    assert.equal(list4.seq, list3.seq + 1);
    assert.deepEqual(list4.keys, [nodeB.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(list4), true);

    // Stored state is consistent with what was served.
    assert.equal(fake.revocationState?.seq, list4.seq);
    assert.equal(
      fake.revocationState?.keysHash,
      sha256Hex([nodeB.nodeId].join("\n")),
    );
    assert.deepEqual(fake.revokedNodeRows.map((r) => r.nodeId), [
      nodeB.nodeId,
    ]);

    // One seq, one set: across everything served, a sequence number
    // never repeats with a different key set.
    const bySeq = new Map<number, string>();
    for (const l of served) {
      const prev = bySeq.get(l.seq);
      if (prev !== undefined) assert.equal(prev, l.keys.join(","));
      bySeq.set(l.seq, l.keys.join(","));
    }
  });

  test("a tampered revocation list fails relay-style verification", async () => {
    const node = makeNode();
    const row = boundRow(node);
    row.revokedAt = new Date();
    const { app } = await setup([row]);
    const list = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.deepEqual(list.keys, [node.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(list), true);
    const forged = { ...list, keys: [] };
    assert.equal(verifyRevocationListRelayStyle(forged), false);
    const bumped = { ...list, seq: list.seq + 1 };
    assert.equal(verifyRevocationListRelayStyle(bumped), false);
  });

  test("feed fails closed (503) when the signing key is not configured", async () => {
    const { app } = await setup([boundRow(makeNode())]);
    const saved = process.env.SIN_TOKEN_PRIVATE_KEY;
    delete process.env.SIN_TOKEN_PRIVATE_KEY;
    try {
      const res = await app.request("/v1/meta/revocations");
      assert.equal(res.status, 503);
      assert.deepEqual(await res.json(), { error: "token_not_configured" });
    } finally {
      process.env.SIN_TOKEN_PRIVATE_KEY = saved;
    }
  });
});

/* ---------------- gate fold: seen-before-signing, sync serialization ---------------- */

function captureConsoleError(): { lines: string[]; restore: () => void } {
  const lines: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  return {
    lines,
    restore: () => {
      console.error = original;
    },
  };
}

describe("sin metadata issuance: gate fold (seen-before-signing, serialized sync)", () => {
  test("unbind between the exchange's key read and its revocation sync: the attestation still issues, and the node still lands on the revoked list", async () => {
    const node = makeNode();
    const { app, fake, mod } = await setup([boundRow(node)]);
    // The interleaving the gate found: the exchange reads its
    // portal row (bound), then the user unbinds — clearing bound_*
    // — and only then does the exchange's revocation sync read
    // portal_keys. The hook fires at the sync's own portal read
    // (the exchange's lookup goes through limit() and never
    // consumes it), so the sync NEVER sees this binding. Without
    // the pre-signing seen insert, the node would hold a valid
    // 7-day attestation yet exist in no bookkeeping — unrevocable
    // for its whole validity window.
    fake.beforePortalReadHooks.push(async () => {
      fake.portalRows[0]!.boundNodeId = null;
      fake.portalRows[0]!.boundNodePublicKey = null;
      fake.portalRows[0]!.boundAt = null;
    });
    const res = await exchange(app, RAW, node);
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    // The attestation issued and verifies — the node really is
    // holding standing that must be revocable.
    const att = body.attestation as Record<string, unknown>;
    assert.ok(att, "attestation present");
    assert.equal(verifyAttestationNodeStyle(att, node.publicB64), true);
    // The seen record exists even though the sync never saw the
    // binding: the exchange's pre-signing insert is the only writer
    // that could have put it there.
    assert.deepEqual(fake.seenBindingRows, [
      { nodeId: node.nodeId, keyId: "key-1" },
    ]);
    // And the node is on the revoked list — already in the
    // exchange's own carriage, whose sync ran after the unbind.
    const carried = body.revocations as RevocationListBody;
    assert.deepEqual(carried.keys, [node.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(carried), true);
    // The feed agrees.
    mod.resetRevocationFeedMemoForTests();
    const list = (await (
      await app.request("/v1/meta/revocations")
    ).json()) as RevocationListBody;
    assert.deepEqual(list.keys, [node.nodeId]);
    assert.equal(verifyRevocationListRelayStyle(list), true);
  });

  test("seen-record failure omits the attestation; the exchange still succeeds", async () => {
    const node = makeNode();
    const { app, fake } = await setup([boundRow(node)]);
    // Every meta_seen_bindings insert fails (the sync's own seen
    // recording included, so the carriage is omitted too — both
    // fail-soft paths ride the same 200).
    fake.failSeenBindingInserts = true;
    const captured = captureConsoleError();
    let res: Response;
    try {
      res = await exchange(app, RAW, node);
    } finally {
      captured.restore();
    }
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    assert.ok(body.access_token, "the exchange itself succeeds");
    assert.ok(!("attestation" in body), "no attestation without the seen record");
    assert.ok(!("revocations" in body), "the failed sync's list is omitted, not served");
    // The omission is logged with its reason category, like the
    // other omission paths.
    assert.ok(
      captured.lines.some((l) =>
        l.includes("attestation omitted (seen_record_failed)"),
      ),
      `expected a seen_record_failed omission log, got: ${captured.lines.join(" | ")}`,
    );
  });

  test("a bound_node_id that disagrees with the bound key omits the attestation", async () => {
    const node = makeNode();
    const other = makeNode();
    // A corrupt binding row: the stored node id names a DIFFERENT
    // key than the stored public key. The exchange's door checks
    // pass (the claimed id matches the stored id; the presented key
    // matches the stored key; the signature verifies) — only the
    // pre-issuance consistency check notices that the attestation
    // would name node's key while revocation tracks other's id.
    const row = boundRow(node, { boundNodeId: other.nodeId });
    const { app } = await setup([row]);
    const headers = nodeHeaders(node, "POST", "/v1/auth/exchange", "", {
      withPublicKey: true,
    });
    headers["x-node-id"] = other.nodeId;
    const captured = captureConsoleError();
    let res: Response;
    try {
      res = await app.request("/v1/auth/exchange", {
        method: "POST",
        headers: { "x-sin-key": RAW, ...headers },
        body: "",
      });
    } finally {
      captured.restore();
    }
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    assert.ok(body.access_token, "the exchange itself succeeds");
    assert.ok(!("attestation" in body), "no attestation for a mismatched binding");
    assert.ok(
      captured.lines.some((l) =>
        l.includes("attestation omitted (bound_node_id_mismatch)"),
      ),
      `expected a bound_node_id_mismatch omission log, got: ${captured.lines.join(" | ")}`,
    );
  });

  test("a sync paused mid-attempt cannot un-revoke: the next sync waits for the advisory lock and serves the fresher set at the next seq", async () => {
    const nodeX = makeNode();
    const { fake, mod } = await setup([boundRow(nodeX)]);
    const build = () =>
      mod.buildRevocationList(
        fake as unknown as SinDb,
        poolPair.privateKey,
      ) as Promise<RevocationListBody>;

    // Baseline sync: X bound, empty list, and X recorded as seen
    // (what later makes its unbind a revocation).
    const list1 = await build();
    assert.deepEqual(list1.keys, []);
    assert.deepEqual(fake.seenBindingRows, [
      { nodeId: nodeX.nodeId, keyId: "key-1" },
    ]);
    const acquisitionsBefore = fake.advisoryLockAcquisitions;

    // Sync A pauses AFTER capturing its portal snapshot (X still
    // bound) but BEFORE any of its writes — the exact posture of
    // the gate's stale sync. The after-read hook is that pause.
    let releaseA!: () => void;
    let signalPaused!: () => void;
    const aPaused = new Promise<void>((r) => {
      signalPaused = r;
    });
    const aGate = new Promise<void>((r) => {
      releaseA = r;
    });
    fake.afterPortalReadHooks.push(async () => {
      signalPaused();
      await aGate;
    });
    const pA = build();
    await aPaused;

    // The unbind lands while A is paused, and sync B starts. In the
    // pre-fold code B ran to completion here (revoking X at the
    // next seq), then A's stale writes deleted X from the
    // materialized set and A won a higher seq WITHOUT X — holders
    // accepted it and X stayed un-revoked. With the advisory lock,
    // B cannot even begin its attempt until A's transaction ends.
    fake.portalRows[0]!.boundNodeId = null;
    fake.portalRows[0]!.boundNodePublicKey = null;
    fake.portalRows[0]!.boundAt = null;
    let bSettled = false;
    const pB = build().then((l) => {
      bSettled = true;
      return l;
    });
    for (let i = 0; i < 3; i++) {
      await new Promise((r) => setImmediate(r));
    }
    try {
      assert.equal(
        bSettled,
        false,
        "sync B must wait for the advisory lock A holds",
      );
      assert.equal(
        fake.advisoryLockAcquisitions,
        acquisitionsBefore + 1,
        "only A has acquired the lock; B is still waiting",
      );
    } finally {
      releaseA();
    }

    // A completes from its (legitimately older) snapshot: X was
    // bound when A read, so A's list is the unchanged empty one —
    // serving it is correct; the invariant is that it lands BEFORE
    // B's fresher list in seq order, never after it.
    const listA = await pA;
    assert.equal(listA.seq, list1.seq);
    assert.deepEqual(listA.keys, []);

    // B now runs against post-unbind state and revokes X at the
    // next sequence — the final served word, which no stale sync
    // can overwrite.
    const listB = await pB;
    assert.deepEqual(listB.keys, [nodeX.nodeId]);
    assert.equal(listB.seq, listA.seq + 1);
    assert.equal(verifyRevocationListRelayStyle(listB), true);
    assert.equal(fake.revocationState?.seq, listB.seq);
    assert.equal(fake.revocationState?.keysHash, sha256Hex(nodeX.nodeId));
    assert.deepEqual(fake.revokedNodeRows.map((r) => r.nodeId), [
      nodeX.nodeId,
    ]);
    assert.equal(fake.advisoryLockAcquisitions, acquisitionsBefore + 2);
  });
});

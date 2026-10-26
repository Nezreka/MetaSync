/**
 * MetaSync metadata service — real schema (DESIGN-METADATA.md).
 *
 * This file replaces the Oct 22 scaffold sketch. The sketch's shape
 * (one `metadata_records` table, per-record signatures, a proposed
 * corroboration N) is superseded by the design doc: the unit of data
 * is the CLAIM (§6), stored raw at ingest and merged by the compiler
 * worker — there is no records table, no publisher_node, and
 * METASYNC_PROPOSED_CORROBORATION_N is deleted (design step 10).
 *
 * Tables owned by this service: meta_batches, meta_claims
 * (migration 0011). Tables it READS for door checks only:
 * portal_keys / portal_users (the contributor gate — the §12
 * amendment scopes account resolution to THIS service's ingest path;
 * SIN code never selects user_id, and no SIN table is joined here).
 * sin_request_nonces is shared §3A.2 replay infrastructure.
 */
import {
  bigserial,
  boolean,
  index,
  integer,
  jsonb,
  pgTable,
  primaryKey,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";

/** One row per accepted signed claim batch (migration 0011).
 *  There is deliberately NO node_id column: a raw node id would let
 *  anyone reading these tables walk batch → portal_keys.bound_node_id
 *  → account, undoing the blind. The blind is the only identity here. */
export const metaBatches = pgTable(
  "meta_batches",
  {
    /** SHA-256 hex of the canonical batch payload — the batch's id. */
    digest: text("digest").primaryKey(),
    /** HMAC-SHA256(META_BLIND_SECRET, portal_users.id), hex (§12 Q1). */
    accountBlind: text("account_blind").notNull(),
    claimCount: integer("claim_count").notNull(),
    accepted: integer("accepted").notNull(),
    receivedAt: timestamp("received_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => [index("meta_batches_blind_received_idx").on(t.accountBlind, t.receivedAt)],
);

/** One row per (subject, field, value, account blind) — see 0011. */
export const metaClaims = pgTable(
  "meta_claims",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    subject: text("subject").notNull(),
    kind: text("kind").notNull(),
    field: text("field").notNull(),
    value: jsonb("value").notNull(),
    /** SHA-256 hex of the canonical JSON of `value`. */
    valueHash: text("value_hash").notNull(),
    source: text("source").notNull(),
    accountBlind: text("account_blind").notNull(),
    batchDigest: text("batch_digest")
      .notNull()
      .references(() => metaBatches.digest),
    receivedAt: timestamp("received_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => [
    uniqueIndex("meta_claims_identity_uq").on(
      t.subject,
      t.field,
      t.valueHash,
      t.accountBlind,
    ),
    index("meta_claims_subject_field_idx").on(t.subject, t.field),
    index("meta_claims_blind_received_idx").on(t.accountBlind, t.receivedAt),
    index("meta_claims_batch_digest_idx").on(t.batchDigest),
  ],
);

/**
 * portal_keys — ONLY the columns this service's door checks need.
 * userId is selected here (and nowhere in SIN) because the contributor
 * gate resolves token → key → account (§12 amendment).
 */
export const portalKeys = pgTable("portal_keys", {
  id: text("id").primaryKey(),
  userId: text("user_id").notNull(),
  expiresAt: timestamp("expires_at", { withTimezone: true }),
  revokedAt: timestamp("revoked_at", { withTimezone: true }),
  boundNodeId: text("bound_node_id"),
  /** Ed25519 public key, base64url (raw 32 bytes). */
  boundNodePublicKey: text("bound_node_public_key"),
});

/** portal_users — only the contributor gate columns (migration 0011). */
export const portalUsers = pgTable("portal_users", {
  id: text("id").primaryKey(),
  contributorApproved: boolean("contributor_approved").notNull().default(false),
  contributorApprovedAt: timestamp("contributor_approved_at", {
    withTimezone: true,
  }),
});

/** Shared §3A.2 replay defence (same table SIN uses; migration 0004). */
export const sinRequestNonces = pgTable(
  "sin_request_nonces",
  {
    nodeId: text("node_id").notNull(),
    nonceHash: text("nonce_hash").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
  },
  (t) => [
    primaryKey({ columns: [t.nodeId, t.nonceHash] }),
    index("sin_request_nonces_expires_at_idx").on(t.expiresAt),
  ],
);

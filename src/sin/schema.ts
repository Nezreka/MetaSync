/**
 * SIN Drizzle schema — see db/migrations/0001_sin_init.sql for the matching SQL.
 *
 * report_tokens: one row per (week, token) — the ONLY trace of a report
 *   beyond its counts. It refuses a second report for the same week and
 *   holds nothing else; pruned after REPORT_TOKEN_RETENTION_WEEKS.
 * aggregate_counts: private running distinct-node counts per (week,
 *   signal, mbid), incremented as each report arrives (migration 0008).
 *   Includes below-k counts, so NO route ever reads it.
 * raw_reports: the pre-0008 per-node ingest rows. Nothing writes it any
 *   more; aggregation drains leftover rows into aggregate_counts.
 * aggregates: the ONLY table the public index routes read. One row per
 *   (week, signal, mbid) with the distinct-node count, copied from
 *   aggregate_counts by aggregation once the k threshold is met.
 * index_publications: audit/bookkeeping for each aggregation run.
 * canonical_playlists: the week's shared playlist definitions, written
 *   only by the official MetaSync (admin key) — see 0002 migration.
 */
import { sql } from "drizzle-orm";
import {
  index,
  integer,
  jsonb,
  pgTable,
  primaryKey,
  text,
  timestamp,
  uniqueIndex,
  uuid,
  varchar,
} from "drizzle-orm/pg-core";
import type { PlaylistTrack } from "./logic.ts";
import type { MbBundle } from "./mb.ts";

export const rawReports = pgTable(
  "raw_reports",
  {
    id: uuid("id").defaultRandom().primaryKey(),
    week: varchar("week", { length: 8 }).notNull(),
    /** Rotating dedupe token hash: hash(node_key + week). Never a raw node key. */
    tokenHash: varchar("token_hash", { length: 128 }).notNull(),
    signal: varchar("signal", { length: 32 }).notNull(),
    /** MusicBrainz ID (artist MBID for watchlist_add, release-group MBID otherwise). */
    mbid: uuid("mbid").notNull(),
    receivedAt: timestamp("received_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => [
    uniqueIndex("raw_reports_binary_uq").on(
      t.tokenHash,
      t.week,
      t.signal,
      t.mbid,
    ),
    index("raw_reports_received_at_idx").on(t.receivedAt),
    index("raw_reports_week_signal_mbid_idx").on(t.week, t.signal, t.mbid),
  ],
);

export const reportTokens = pgTable(
  "report_tokens",
  {
    week: varchar("week", { length: 8 }).notNull(),
    /** Rotating dedupe token hash(node_key + week) — never linked to signals. */
    tokenHash: varchar("token_hash", { length: 128 }).notNull(),
  },
  (t) => [primaryKey({ columns: [t.week, t.tokenHash] })],
);

export const aggregateCounts = pgTable(
  "aggregate_counts",
  {
    week: varchar("week", { length: 8 }).notNull(),
    signal: varchar("signal", { length: 32 }).notNull(),
    mbid: uuid("mbid").notNull(),
    nodeCount: integer("node_count").notNull(),
  },
  (t) => [primaryKey({ columns: [t.week, t.signal, t.mbid] })],
);

export const aggregates = pgTable(
  "aggregates",
  {
    week: varchar("week", { length: 8 }).notNull(),
    signal: varchar("signal", { length: 32 }).notNull(),
    mbid: uuid("mbid").notNull(),
    nodeCount: integer("node_count").notNull(),
    computedAt: timestamp("computed_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => [primaryKey({ columns: [t.week, t.signal, t.mbid] })],
);

export const indexPublications = pgTable("index_publications", {
  id: uuid("id").defaultRandom().primaryKey(),
  week: varchar("week", { length: 8 }).notNull(),
  /** 'nightly' | 'manual' — how this aggregation run was triggered. */
  trigger: varchar("trigger", { length: 32 }).notNull(),
  aggregateCount: integer("aggregate_count").notNull(),
  rawDeleted: integer("raw_deleted").notNull(),
  publishedAt: timestamp("published_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

/**
 * canonical_playlists: the shared SIN playlist definitions for one week,
 * published ONLY by the official MetaSync (admin key — see index.ts).
 * One row per (week, key); key is one of trending | most-wanted |
 * breakouts. `tracks` holds the validated Spotify-shaped track subset
 * (logic.ts PlaylistTrack) as JSONB. User nodes only READ this table
 * (via GET /v1/playlists) and materialize local copies in their own
 * SoulSync; they can never write it. See db/migrations/0002.
 */
/**
 * portal_keys: per-user pool keys issued by the portal app
 * (PORTAL-DESIGN.md §2.2, migration 0003). Only the HASH is ever stored
 * (SHA-256 hex of API_KEY_PEPPER + raw key); the raw key exists only in
 * the user's MetaSync. Validity rule (roll-call's, §2.2):
 * `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())` —
 * there is deliberately no is_active flag.
 *
 * Privacy (§4, P3): this definition omits every identity column and
 * there is NO portal_users table in SIN's schema at all. SIN reads a
 * key row to answer pass/fail (+ key_prefix for /v1/whoami) and to
 * touch last_used_at; it never joins a key to a person in code, and
 * the user_id below is carried only because the table has it — SIN
 * code never selects or returns it.
 */
export const portalKeys = pgTable(
  "portal_keys",
  {
    id: text("id").primaryKey(),
    /** FK to portal_users(id) in SQL (migration 0003). Carried here only
     *  because the column exists; SIN code never selects it (P3). No
     *  Drizzle reference is declared: SIN deliberately does not model
     *  the portal's identity tables at all. */
    userId: text("user_id").notNull(),
    keyHash: text("key_hash").notNull().unique(),
    /** First 12 chars of the raw key (e.g. "sin_live_Ab…") — display only. */
    keyPrefix: text("key_prefix").notNull(),
    lastUsedAt: timestamp("last_used_at", { withTimezone: true }),
    expiresAt: timestamp("expires_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    revokedAt: timestamp("revoked_at", { withTimezone: true }),
    /* Node binding (§3A.3, migration 0004): the ONE MetaSync node this
     * key is approved for. bound_* is set only by the portal approval;
     * pending_* holds a first-exchange candidate until then. These are
     * door-check fields (P7) — verified at exchange and on every
     * token-authenticated call, never written to any SIN data table.
     * The portal app's Prisma schema mirrors the same columns. */
    boundNodeId: text("bound_node_id"),
    /** Ed25519 public key, base64url (raw 32 bytes). */
    boundNodePublicKey: text("bound_node_public_key"),
    boundAt: timestamp("bound_at", { withTimezone: true }),
    pendingNodeId: text("pending_node_id"),
    /** Ed25519 public key, base64url (raw 32 bytes). */
    pendingNodePublicKey: text("pending_node_public_key"),
    pendingRequestedAt: timestamp("pending_requested_at", {
      withTimezone: true,
    }),
  },
  (t) => [
    index("portal_keys_user_id_idx").on(t.userId),
    uniqueIndex("portal_keys_bound_node_public_key_uq")
      .on(t.boundNodePublicKey)
      .where(sql`${t.boundNodePublicKey} is not null`),
  ],
);

/**
 * sin_request_nonces: replay defence for node-signed requests (§3A.2,
 * migration 0004). One row per (node_id, nonce_hash) seen inside the
 * ±5-minute signature window; rows expire with the window and are
 * deleted opportunistically. This is a security table, NOT a report
 * log (P7): it holds no body, no report content, no report identifier.
 * nonce_hash is SHA-256 hex of the presented nonce.
 */
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

export const canonicalPlaylists = pgTable(
  "canonical_playlists",
  {
    /** ISO week string, e.g. "2026-W43". */
    week: text("week").notNull(),
    /** Playlist key: trending | most-wanted | breakouts. */
    key: text("key").notNull(),
    /** Display name for the week, e.g. "Trending in SoulSync". */
    name: text("name").notNull(),
    tracks: jsonb("tracks").$type<PlaylistTrack[]>().notNull(),
    publishedAt: timestamp("published_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => [primaryKey({ columns: [t.week, t.key] })],
);

/**
 * Composer resolution cache (migration 0006): MusicBrainz facts about
 * charted MBIDs, fetched once by the composer and kept. Public catalogue
 * data only (titles, track lists, genre, year) — never user data.
 */
export const mbResolutions = pgTable("mb_resolutions", {
  mbid: text("mbid").primaryKey(),
  /** "release-group" | "artist". */
  kind: text("kind").notNull(),
  bundle: jsonb("bundle").$type<MbBundle>().notNull(),
  resolvedAt: timestamp("resolved_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

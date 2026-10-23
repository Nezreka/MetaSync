/**
 * MetaSync metadata service — PLANNED schema. SCAFFOLD ONLY.
 *
 * Nothing in this file is implemented, migrated, or read by any route yet.
 * It records the intended shape so the real build (a separate, larger piece
 * of work) starts from the design in SIDECAR-ARCHITECTURE-SKETCH.md §3,
 * updated Oct 22, 2026: records are keyed by **SoulID** — the canonical ID
 * SoulSync assigns each track, album, and artist.
 *
 * SoulID is ONLY for this metadata service. SIN does not use SoulID;
 * SIN signals are keyed by MusicBrainz IDs (see src/sin/logic.ts).
 *
 * Planned tables (Drizzle sketch — DO NOT uncomment until the MetaSync
 * build is commissioned and a migration is written for them):
 *
 * ```ts
 * import {
 *   boolean, index, integer, jsonb, pgTable, text, timestamp, uuid, varchar,
 * } from "drizzle-orm/pg-core";
 *
 * export const metadataRecords = pgTable("metadata_records", {
 *   // SoulID: SoulSync-assigned canonical ID for the track / album / artist.
 *   soulId: varchar("soul_id", { length: 64 }).primaryKey(),
 *   entityType: varchar("entity_type", { length: 16 }).notNull(), // track|album|artist
 *   // MusicBrainz IDs this SoulID resolves to, when known (may be empty —
 *   // a SoulID can exist before any MBID does, e.g. Soulseek long-tail).
 *   artistMbid: uuid("artist_mbid"),
 *   releaseGroupMbid: uuid("release_group_mbid"),
 *   recordingMbid: uuid("recording_mbid"),
 *   // Cross-provider IDs SoulSync already resolved for this entity,
 *   // e.g. { spotify: "...", deezer: "...", itunes: "...", discogs: "..." }
 *   providerIds: jsonb("provider_ids").notNull().default({}),
 *   genres: text("genres").array().notNull().default([]),
 *   tags: text("tags").array().notNull().default([]),
 *   // User-corrected fields, kept in a separate bag and ALWAYS flagged as
 *   // corrections so they are never confused with provider data.
 *   corrections: jsonb("corrections").notNull().default({}),
 *   hasCorrections: boolean("has_corrections").notNull().default(false),
 *   // Every record is signed by the publishing node's keypair.
 *   signature: text("signature").notNull(),
 *   publisherNode: varchar("publisher_node", { length: 128 }).notNull(),
 *   // Corroboration: how many independent peers held this same record.
 *   // A record is only accepted at N >= 3 (proposed) or on a MusicBrainz match.
 *   corroborationCount: integer("corroboration_count").notNull().default(0),
 *   updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
 * });
 * ```
 *
 * Trust rules that any implementation MUST keep (sketch §3):
 * - Local data always wins. MetaSync never overwrites a user's own SoulSync
 *   metadata; it fills gaps and offers corrections.
 * - No audio. Ever. Not whole, not in pieces. Metadata only.
 * - No library dumps, no listening data, no file paths, no user identity
 *   beyond the node key that signs a record.
 */

export interface PlannedMetadataRecord {
  soulId: string;
  entityType: "track" | "album" | "artist";
  artistMbid: string | null;
  releaseGroupMbid: string | null;
  recordingMbid: string | null;
  providerIds: Record<string, string>;
  genres: string[];
  tags: string[];
  corrections: Record<string, unknown>;
  hasCorrections: boolean;
  signature: string;
  publisherNode: string;
  corroborationCount: number;
}

/** Names of the planned tables, for docs/status only. */
export const METASYNC_PLANNED_TABLES = ["metadata_records"] as const;

/** Proposed corroboration threshold (open decision, sketch §5). */
export const METASYNC_PROPOSED_CORROBORATION_N = 3;

/**
 * SIN database handle.
 *
 * Per the neon-functions skill:
 * - Use node-postgres (`pg`) with Drizzle — Neon Functions are long-running
 *   and reuse the isolate, so a persistent pool is the right fit (not the
 *   HTTP serverless driver).
 * - Create the pool ONCE at module scope, `max: 5` (each isolate keeps its
 *   own pool, so keep it small), and call `attachDatabasePool(pool)` so an
 *   idle disconnect is not an uncaughtException.
 * - Use the pooled `DATABASE_URL` for request traffic. Migrations use the
 *   DIRECT/unpooled URL instead — see db/migrations/README.md.
 *
 * `parseEnv` from @neon/env is the typed alternative for reading the URL;
 * here we read process.env.DATABASE_URL directly (also valid per the skill)
 * so importing this module in tests — where no DATABASE_URL exists — yields
 * `db = null` instead of throwing, and routes can fail closed with 503.
 */
import { attachDatabasePool } from "@neon/functions";
import { drizzle, type NodePgDatabase } from "drizzle-orm/node-postgres";
import { Pool } from "pg";
import * as schema from "./schema.ts";

export type SinDb = NodePgDatabase<typeof schema>;

const connectionString = process.env.DATABASE_URL;

export const pool: Pool | null = connectionString
  ? new Pool({ connectionString, max: 5 })
  : null;

if (pool) attachDatabasePool(pool);

export const db: SinDb | null = pool ? drizzle(pool, { schema }) : null;

/** Returns the db handle, or null when DATABASE_URL is not configured. */
export function getDb(): SinDb | null {
  return db;
}

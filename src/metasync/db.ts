/**
 * Metadata service database handle. Same shape as src/sin/db.ts:
 * node-postgres pool created once at module scope, `db = null` when
 * DATABASE_URL is unset so routes fail closed with 503 (and tests can
 * import the module without a database).
 */
import { attachDatabasePool } from "@neon/functions";
import { drizzle, type NodePgDatabase } from "drizzle-orm/node-postgres";
import { Pool } from "pg";
import * as schema from "./schema.ts";

export type MetaDb = NodePgDatabase<typeof schema>;

const connectionString = process.env.DATABASE_URL;

export const pool: Pool | null = connectionString
  ? new Pool({ connectionString, max: 5 })
  : null;

if (pool) attachDatabasePool(pool);

export const db: MetaDb | null = pool ? drizzle(pool, { schema }) : null;

/** Returns the db handle, or null when DATABASE_URL is not configured. */
export function getDb(): MetaDb | null {
  return db;
}

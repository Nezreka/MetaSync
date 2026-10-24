/**
 * In-memory fake SIN database shared by the route suites. It implements
 * the exact drizzle chains the routes use — portal select by hash AND
 * by id, the pending-binding UPDATE, the throttled last_used_at touch,
 * nonce insert/delete, the count-on-arrival report path (report_tokens
 * claim + additive aggregate_counts upserts inside a transaction), the
 * aggregation pass (raw drain, publish >= k, token prune), and the
 * playlist/aggregate reads — inferring each query's meaning from the
 * table and the bound parameter values, never from SQL text.
 * Not a test file itself (no *.test.ts suffix).
 */
import {
  aggregateCounts as aggregateCountsTable,
  aggregates as aggregatesTable,
  canonicalPlaylists as playlistsTable,
  portalKeys as portalKeysTable,
  rawReports as rawReportsTable,
  reportPopulations as populationsTable,
  reportTokens as reportTokensTable,
  sinRequestNonces as noncesTable,
} from "../src/sin/schema.ts";

/** Collect the Param values of a drizzle SQL condition, in order. */
export function sqlParams(cond: unknown, out: unknown[] = []): unknown[] {
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

export interface PortalRow {
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

export interface RawRow {
  week: string;
  tokenHash: string;
  signal: string;
  mbid: string;
  receivedAt: Date;
}

export interface CountRow {
  week: string;
  signal: string;
  mbid: string;
  nodeCount: number;
}

export interface PlaylistRow {
  week: string;
  key: string;
  name: string;
  tracks: unknown[];
  publishedAt: Date;
}

class FakeQuery {
  private cond: unknown = undefined;
  private fake: FakeSinDb;
  private table: unknown;
  private selection: Record<string, unknown> | undefined;
  constructor(
    fake: FakeSinDb,
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
    if (this.table === populationsTable) {
      // where(eq(week, w)) or where(inArray(week, [...])).
      return this.fake.populationRows
        .filter((r) => params.length === 0 || params.includes(r.week))
        .map((r) => ({ ...r }));
    }
    if (this.table === aggregateCountsTable) {
      // where(gte(nodeCount, k)) optionally and(eq(week, w), ...).
      const k = params.find((p) => typeof p === "number") as number | undefined;
      const week = params.find((p) => typeof p === "string") as string | undefined;
      return this.fake.countRows
        .filter((r) => (k === undefined || r.nodeCount >= k) && (week === undefined || r.week === week))
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
  private fake: FakeSinDb;
  private table: unknown;
  private vals: unknown;
  constructor(fake: FakeSinDb, table: unknown, vals: unknown) {
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
    const rows = (Array.isArray(this.vals) ? this.vals : [this.vals]) as Record<string, unknown>[];
    if (this.table === reportTokensTable) {
      const inserted: Record<string, unknown>[] = [];
      for (const v of rows) {
        const k = `${v.week}|${v.tokenHash}`;
        if (fake.reportTokens.has(k)) continue;
        fake.reportTokens.add(k);
        inserted.push({ week: v.week });
      }
      return inserted;
    }
    if (this.table === populationsTable) {
      // Production upserts reports + 1, churn_optin + (0 or 1).
      for (const v of rows as unknown as PopulationRow[]) {
        const existing = fake.populationRows.find((r) => r.week === v.week);
        if (existing) {
          existing.reports += v.reports;
          existing.churnOptin += v.churnOptin;
        } else fake.populationRows.push({ ...v });
      }
      return [];
    }
    if (this.table === aggregateCountsTable) {
      // Production upserts with node_count = node_count + excluded.node_count.
      for (const v of rows as unknown as CountRow[]) {
        const existing = fake.countRows.find(
          (r) => r.week === v.week && r.signal === v.signal && r.mbid === v.mbid,
        );
        if (existing) existing.nodeCount += v.nodeCount;
        else fake.countRows.push({ ...v });
      }
      return [];
    }
    if (this.table === rawReportsTable) {
      const inserted: Record<string, unknown>[] = [];
      for (const row of rows as unknown as RawRow[]) {
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
      const v = rows[0] as { nodeId: string; nonceHash: string; expiresAt: Date };
      const k = `${v.nodeId}|${v.nonceHash}`;
      if (fake.nonceRows.has(k)) return [];
      fake.nonceRows.set(k, { ...v });
      return [{ nodeId: v.nodeId }];
    }
    if (this.table === aggregatesTable) {
      // Production inserts chunked arrays with excluded.* conflict updates.
      for (const v of rows) {
        const k = `${v.week}|${v.signal}|${v.mbid}`;
        const existing = fake.aggregateRows.find(
          (r) => `${r.week}|${r.signal}|${r.mbid}` === k,
        );
        if (existing) {
          existing.nodeCount = v.nodeCount;
          existing.computedAt = v.computedAt;
        } else fake.aggregateRows.push({ ...v });
      }
      return [];
    }
    if (this.table === playlistsTable) {
      const v = rows[0] as unknown as PlaylistRow;
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

export interface PopulationRow {
  week: string;
  reports: number;
  churnOptin: number;
}

export class FakeSinDb {
  portalRows: PortalRow[];
  rawRows: RawRow[] = [];
  reportTokens = new Set<string>();
  countRows: CountRow[] = [];
  populationRows: PopulationRow[] = [];
  aggregateRows: Record<string, unknown>[] = [];
  playlistRows: PlaylistRow[] = [];
  nonceRows = new Map<string, { nodeId: string; nonceHash: string; expiresAt: Date }>();
  touchAttempts = 0;
  touchApplied = 0;
  transactions = 0;

  constructor(rows: PortalRow[] = []) {
    this.portalRows = rows;
  }

  /** Runs the callback against this same fake (no rollback needed: the
   *  routes under test never throw mid-transaction). */
  async transaction<T>(fn: (tx: FakeSinDb) => Promise<T>): Promise<T> {
    this.transactions += 1;
    return fn(this);
  }

  select(selection?: Record<string, unknown>) {
    return {
      from: (table: unknown) => new FakeQuery(this, table, selection),
    };
  }

  selectDistinct(selection?: Record<string, unknown>) {
    return this.select(selection);
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
            // Drain: every row (isNotNull), or one week (eq week).
            const week = typeof param === "string" ? param : undefined;
            const gone = this.rawRows.filter((r) => week === undefined || r.week === week);
            this.rawRows = this.rawRows.filter((r) => !gone.includes(r));
            return gone.map((r) => ({ ...r }));
          }
          if (table === playlistsTable) {
            // and(eq(week, w), notInArray(key, keep)): week first, then keys.
            const [week, ...keep] = sqlParams(cond) as string[];
            this.playlistRows = this.playlistRows.filter((r) => r.week !== week || keep.includes(r.key));
            return [];
          }
          if (table === aggregateCountsTable) {
            const cutoff = param as string;
            this.countRows = this.countRows.filter((r) => !(r.week < cutoff));
            return [];
          }
          if (table === reportTokensTable) {
            const cutoff = param as string;
            for (const k of [...this.reportTokens]) {
              if (k.split("|")[0]! < cutoff) this.reportTokens.delete(k);
            }
            return [];
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

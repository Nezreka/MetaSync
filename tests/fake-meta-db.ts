/**
 * In-memory fake metadata-service database for the ingest suite.
 * Implements exactly the drizzle chains src/metasync/index.ts uses —
 * portal key/user selects by id, batch select by digest and by
 * (blind, day), batch insert/update, claim bulk insert with the
 * identity unique rule, claim delete for retractions, and the nonce
 * insert/delete — inferring each query's meaning from the table and
 * the bound parameter values (via sqlParams), never from SQL text.
 * Not a test file itself (no *.test.ts suffix).
 */
import {
  metaBatches,
  metaClaims,
  portalKeys,
  portalUsers,
  sinRequestNonces,
} from "../src/metasync/schema.ts";
import { sqlParams } from "./fake-db.ts";

export interface FakePortalKeyRow {
  id: string;
  userId: string;
  expiresAt: Date | null;
  revokedAt: Date | null;
  boundNodeId: string | null;
  boundNodePublicKey: string | null;
}

export interface FakePortalUserRow {
  id: string;
  contributorApproved: boolean;
  contributorApprovedAt: Date | null;
}

export interface FakeBatchRow {
  digest: string;
  accountBlind: string;
  claimCount: number;
  accepted: number;
  receivedAt: Date;
}

export interface FakeClaimRow {
  id: number;
  subject: string;
  kind: string;
  field: string;
  value: unknown;
  valueHash: string;
  source: string;
  accountBlind: string;
  batchDigest: string;
  receivedAt: Date;
}

interface NonceRow {
  nodeId: string;
  nonceHash: string;
  expiresAt: Date;
}

/** A thenable query result that also carries chain methods (.limit /
 *  .returning) for the call sites that use them. */
function chainable<T>(rows: T[], extra: Record<string, unknown> = {}) {
  return {
    ...extra,
    then: (
      resolve: (v: T[]) => unknown,
      reject?: (e: unknown) => unknown,
    ) => Promise.resolve(rows).then(resolve, reject),
  };
}

export class FakeMetaDb {
  keys = new Map<string, FakePortalKeyRow>();
  users = new Map<string, FakePortalUserRow>();
  batches = new Map<string, FakeBatchRow>();
  claims: FakeClaimRow[] = [];
  nonces = new Map<string, NonceRow>();
  private claimSeq = 0;
  /** Test hook: throw on the Nth claim-insert call (1-based; 0 = never). */
  failAtClaimInsertCall = 0;
  private claimInsertCalls = 0;

  /** Snapshot/rollback transaction, like the real thing: if the
   *  callback throws, every write inside it is undone. */
  async transaction<T>(cb: (tx: this) => Promise<T>): Promise<T> {
    const snapshot = structuredClone({
      keys: this.keys,
      users: this.users,
      batches: this.batches,
      claims: this.claims,
      nonces: this.nonces,
      claimSeq: this.claimSeq,
    });
    try {
      return await cb(this);
    } catch (e) {
      this.keys = snapshot.keys;
      this.users = snapshot.users;
      this.batches = snapshot.batches;
      this.claims = snapshot.claims;
      this.nonces = snapshot.nonces;
      this.claimSeq = snapshot.claimSeq;
      throw e;
    }
  }

  /** Operation log so tests can pin ordering (the advisory lock must
   *  be the transaction's first statement). */
  events: string[] = [];

  /** Advisory lock: the fake always grants it, and records the call. */
  execute(_sql: unknown) {
    this.events.push("execute");
    return Promise.resolve([{ locked: true }]);
  }

  select(_selection?: unknown) {
    const self = this;
    return {
      from(table: unknown) {
        return {
          where(cond: unknown) {
            const params = sqlParams(cond);
            let rows: unknown[] = [];
            if (table === portalKeys) {
              const row = self.keys.get(params[0] as string);
              rows = row ? [row] : [];
            } else if (table === portalUsers) {
              const row = self.users.get(params[0] as string);
              rows = row ? [row] : [];
            } else if (table === metaBatches) {
              if (params.length === 1) {
                const row = self.batches.get(params[0] as string);
                rows = row ? [row] : [];
              } else {
                const [blind, since] = params as [string, Date];
                rows = [...self.batches.values()].filter(
                  (b) => b.accountBlind === blind && b.receivedAt >= since,
                );
              }
            }
            const result = rows as never[];
            return chainable(result, {
              limit: (n: number) => Promise.resolve(result.slice(0, n)),
            });
          },
        };
      },
    };
  }

  insert(table: unknown) {
    const self = this;
    this.events.push("insert");
    return {
      values(v: unknown) {
        return {
          onConflictDoNothing() {
            return {
              returning(_sel?: unknown) {
                if (table === metaBatches) {
                  const row = v as FakeBatchRow;
                  if (self.batches.has(row.digest)) return [];
                  const stored = { ...row, receivedAt: new Date() };
                  self.batches.set(row.digest, stored);
                  return [stored];
                }
                if (table === metaClaims) {
                  self.claimInsertCalls++;
                  if (
                    self.failAtClaimInsertCall > 0 &&
                    self.claimInsertCalls === self.failAtClaimInsertCall
                  ) {
                    throw new Error("injected claim insert failure");
                  }
                  const inserted: FakeClaimRow[] = [];
                  for (const item of v as Omit<FakeClaimRow, "id" | "receivedAt">[]) {
                    const dupe = self.claims.some(
                      (c) =>
                        c.subject === item.subject &&
                        c.field === item.field &&
                        c.valueHash === item.valueHash &&
                        c.accountBlind === item.accountBlind,
                    );
                    if (dupe) continue;
                    const row: FakeClaimRow = {
                      ...item,
                      id: ++self.claimSeq,
                      receivedAt: new Date(),
                    };
                    self.claims.push(row);
                    inserted.push(row);
                  }
                  return inserted;
                }
                if (table === sinRequestNonces) {
                  const row = v as NonceRow;
                  const key = `${row.nodeId}|${row.nonceHash}`;
                  if (self.nonces.has(key)) return [];
                  self.nonces.set(key, row);
                  return [row];
                }
                return [];
              },
            };
          },
        };
      },
    };
  }

  update(table: unknown) {
    const self = this;
    return {
      set(patch: Record<string, unknown>) {
        return {
          where(cond: unknown) {
            const params = sqlParams(cond);
            if (table === metaBatches) {
              const row = self.batches.get(params[0] as string);
              if (row) Object.assign(row, patch);
            }
            return Promise.resolve([]);
          },
        };
      },
    };
  }

  delete(table: unknown) {
    const self = this;
    return {
      where(cond: unknown) {
        const params = sqlParams(cond);
        let removed: unknown[] = [];
        if (table === sinRequestNonces) {
          const before = params[0] as Date;
          for (const [key, row] of self.nonces) {
            if (row.expiresAt < before) self.nonces.delete(key);
          }
        } else if (table === metaClaims) {
          // Handler condition shape: blind, cutoff, then
          // (subject, field, valueHash) triples from the OR group.
          const [blind, cutoff, ...rest] = params as [
            string, Date, ...unknown[],
          ];
          const triples: [string, string, string][] = [];
          for (let i = 0; i + 3 <= rest.length; i += 3) {
            triples.push([rest[i] as string, rest[i + 1] as string, rest[i + 2] as string]);
          }
          const kept: FakeClaimRow[] = [];
          for (const c of self.claims) {
            if (
              c.accountBlind === blind &&
              c.receivedAt >= cutoff &&
              triples.some(
                ([s, f, h]) =>
                  c.subject === s && c.field === f && c.valueHash === h,
              )
            ) {
              removed.push(c);
            } else {
              kept.push(c);
            }
          }
          self.claims = kept;
        }
        return chainable([] as never[], {
          returning: (_sel?: unknown) => Promise.resolve(removed),
        });
      },
    };
  }
}

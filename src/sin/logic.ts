/**
 * SIN (SoulSync Intelligence Network) — pure logic.
 *
 * Everything in this file is side-effect free and database-free so it can be
 * unit-tested without a live Neon/Postgres instance.
 *
 * Privacy invariants baked in here (see README / SIDECAR-ARCHITECTURE-SKETCH.md §2):
 * - Signals are keyed ONLY by MusicBrainz IDs. SoulID is NOT used by SIN —
 *   SoulID belongs exclusively to the metadata MetaSync service.
 * - Exactly eight signal types exist. Anything else is rejected.
 * - Contributions are binary: one node can contribute at most 1 per
 *   (week, signal, MBID), and a node's token is accepted once per week.
 * - Reports are counted on arrival and never stored per node.
 * - Nothing is published below the k-anonymity threshold.
 */

/** k-anonymity publish threshold: an item needs at least this many distinct
 *  nodes in a week before it may appear in any public index. */
export const K_PUBLISH_THRESHOLD = 15;

/** A breakout needs at least this many watchlist_add nodes in the current week. */
export const BREAKOUT_MIN_NODES = 20;

/** A breakout needs week-over-week watchlist_add growth of at least this factor. */
export const BREAKOUT_GROWTH_FACTOR = 2;

/** Hard cap on a single POST /v1/report body, in bytes. A report carries
 *  one week of events (DESIGN-NODE.md §12), not a library listing, so it
 *  is a few hundred signals; this leaves headroom while rejecting abuse. */
export const MAX_REPORT_BODY_BYTES = 1024 * 1024;

/** Hard cap on the number of signals in a single report. */
export const MAX_SIGNALS_PER_REPORT = 5_000;

/** Per-type cap in one report. The node suppresses a bulk import rather
 *  than reporting it (it is a migration, not taste), so a real report
 *  sits far below this; a report above it is a library dump and is
 *  refused rather than stored. */
export const MAX_SIGNALS_PER_TYPE = 1_000;

/** listen_album is the most personal signal, so its cap is tighter: the
 *  node sends at most its top 50 albums of the week (DESIGN-LISTEN-SIGNALS.md). */
export const MAX_LISTEN_SIGNALS = 100;

/** The only eight signal types SIN accepts (DESIGN-DOWNLOAD-SIGNALS.md,
 *  DESIGN-LISTEN-SIGNALS.md). */
export const SIGNAL_TYPES = [
  "watchlist_add",
  "wishlist_add",
  "import_keep",
  "hunt_demand",
  "recent_add",
  "download_grab",
  "download_churn",
  "listen_album",
] as const;

export type SignalType = (typeof SIGNAL_TYPES)[number];

/** Which MBID kind each signal is keyed by (documentation + validation aid). */
export const SIGNAL_MBID_KIND: Record<SignalType, "artist" | "release-group"> = {
  watchlist_add: "artist",
  wishlist_add: "release-group",
  import_keep: "release-group",
  hunt_demand: "release-group",
  recent_add: "release-group",
  download_grab: "release-group",
  download_churn: "release-group",
  listen_album: "release-group",
};

function signalTypeCap(type: SignalType): number {
  return type === "listen_album" ? MAX_LISTEN_SIGNALS : MAX_SIGNALS_PER_TYPE;
}

/** ISO-8601 week of a UTC instant, e.g. "2026-W43" (weeks start Monday;
 *  week 1 holds the year's first Thursday). Same rule as the node's
 *  weekutil.iso_week_string. */
export function isoWeekOf(when: Date): string {
  const d = new Date(Date.UTC(when.getUTCFullYear(), when.getUTCMonth(), when.getUTCDate()));
  const weekday = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - weekday); // the Thursday of this week
  const yearStart = Date.UTC(d.getUTCFullYear(), 0, 1);
  const week = Math.ceil(((d.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

/** The weeks a report may be filed for: the one that just closed (what a
 *  current node sends) and the current one. Anything older or in the
 *  future is refused, so nobody can backfill or pre-stuff a week. */
export function openReportWeeks(now: Date = new Date()): string[] {
  const previous = new Date(now.getTime() - 7 * 86_400_000);
  return [isoWeekOf(previous), isoWeekOf(now)];
}

/** Report tokens older than this many weeks can no longer collide with
 *  an accepted report (the window is two weeks), so they are pruned. */
export const REPORT_TOKEN_RETENTION_WEEKS = 3;

export interface SignalInput {
  type: SignalType;
  mbid: string;
}

export interface ReportInput {
  /** ISO week string, e.g. "2026-W43". */
  week: string;
  /** Rotating dedupe token: hash(node_key + week). Unlinkable across weeks. */
  token: string;
  signals: SignalInput[];
  /** Sent (as true) only by a node that shares drop signals. Counted per
   *  week in report_populations, never stored per node; it lets Loved &
   *  Kept scale churn from the opted-in share up to every reporter. */
  churnOptin?: boolean;
}

export interface RawReportRow {
  week: string;
  token: string;
  signal: SignalType;
  mbid: string;
  /** Epoch ms, ISO string, or Date — when the aggregator received it. */
  receivedAt: string | number | Date;
}

export interface AggregateRow {
  week: string;
  signal: SignalType;
  mbid: string;
  nodeCount: number;
}

const UUID_SHAPE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuidShape(value: unknown): value is string {
  return typeof value === "string" && UUID_SHAPE.test(value);
}

const ISO_WEEK_SHAPE = /^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$/;

export function isIsoWeekString(value: unknown): value is string {
  return typeof value === "string" && ISO_WEEK_SHAPE.test(value);
}

const TOKEN_SHAPE = /^[A-Za-z0-9_-]{16,128}$/;

export function isTokenHash(value: unknown): value is string {
  return typeof value === "string" && TOKEN_SHAPE.test(value);
}

export function isSignalType(value: unknown): value is SignalType {
  return (
    typeof value === "string" &&
    (SIGNAL_TYPES as readonly string[]).includes(value)
  );
}

export type ValidationResult =
  | { ok: true; value: ReportInput }
  | { ok: false; errors: string[] };

/**
 * Validate an untrusted POST /v1/report payload.
 *
 * Strict on purpose: unknown top-level or per-signal fields are rejected so a
 * client can never smuggle titles, names, paths, listening history, or any
 * other never-collected data through this endpoint.
 */
export function validateReport(input: unknown): ValidationResult {
  const errors: string[] = [];
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return { ok: false, errors: ["payload must be a JSON object"] };
  }
  const obj = input as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (key !== "week" && key !== "token" && key !== "signals" && key !== "churn_optin") {
      errors.push(`unknown field: ${key}`);
    }
  }
  if (obj.churn_optin !== undefined && obj.churn_optin !== true) {
    errors.push("churn_optin, when present, must be true");
  }
  if (!isIsoWeekString(obj.week)) {
    errors.push("week must be an ISO week string like 2026-W43");
  }
  if (!isTokenHash(obj.token)) {
    errors.push("token must be a 16-128 char token hash (letters, digits, - or _)");
  }
  if (!Array.isArray(obj.signals)) {
    errors.push("signals must be an array");
  } else {
    if (obj.signals.length === 0) {
      errors.push("signals must not be empty");
    }
    if (obj.signals.length > MAX_SIGNALS_PER_REPORT) {
      errors.push(`signals exceeds max of ${MAX_SIGNALS_PER_REPORT} per report`);
    }
    obj.signals.forEach((raw, i) => {
      if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
        errors.push(`signals[${i}] must be an object`);
        return;
      }
      const sig = raw as Record<string, unknown>;
      for (const key of Object.keys(sig)) {
        if (key !== "type" && key !== "mbid") {
          errors.push(`signals[${i}] has unknown field: ${key}`);
        }
      }
      if (!isSignalType(sig.type)) {
        errors.push(
          `signals[${i}].type must be one of: ${SIGNAL_TYPES.join(", ")}`,
        );
      }
      if (!isUuidShape(sig.mbid)) {
        errors.push(`signals[${i}].mbid must be a UUID-shaped MusicBrainz ID`);
      }
    });
    if (errors.length === 0) {
      const perType = new Map<SignalType, number>();
      for (const s of dedupeSignals(obj.signals as SignalInput[])) {
        perType.set(s.type, (perType.get(s.type) ?? 0) + 1);
      }
      for (const [type, count] of perType) {
        if (count > signalTypeCap(type)) {
          errors.push(`${type} exceeds max of ${signalTypeCap(type)} per report`);
        }
      }
    }
  }

  if (errors.length > 0) return { ok: false, errors };
  const valid = obj as unknown as ReportInput;
  return {
    ok: true,
    value: {
      week: valid.week,
      token: valid.token,
      // Normalise (dedupe within the payload) so callers insert binary rows.
      signals: dedupeSignals(valid.signals),
      ...(obj.churn_optin === true ? { churnOptin: true } : {}),
    },
  };
}

/** Binary contributions: collapse duplicate (type, mbid) pairs, keeping order. */
export function dedupeSignals(signals: SignalInput[]): SignalInput[] {
  const seen = new Set<string>();
  const out: SignalInput[] = [];
  for (const s of signals) {
    const key = `${s.type}:${s.mbid.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ type: s.type, mbid: s.mbid.toLowerCase() });
  }
  return out;
}

/** One +1 for the running count of (week, signal, mbid). */
export interface CountIncrement {
  week: string;
  signal: SignalType;
  mbid: string;
}

/**
 * Expand a validated report into one count increment per distinct
 * signal. This is ALL the server keeps of a report: the token is used
 * once, to refuse a second report for the same week, and never stored
 * beside the signals — no per-node row ever exists at rest.
 */
export function reportToCountIncrements(report: ReportInput): CountIncrement[] {
  return dedupeSignals(report.signals).map((s) => ({
    week: report.week,
    signal: s.type,
    mbid: s.mbid,
  }));
}

/** Dedupe raw rows on (token, week, signal, mbid) — the binary-contribution key. */
export function dedupeRawReports(rows: RawReportRow[]): RawReportRow[] {
  const seen = new Set<string>();
  const out: RawReportRow[] = [];
  for (const r of rows) {
    const key = `${r.token}|${r.week}|${r.signal}|${r.mbid.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
  }
  return out;
}

/**
 * Count DISTINCT tokens per (week, signal, mbid) and keep groups at or
 * above k. Used only to drain raw_reports rows left by the pre-0008
 * ingest path into the running counts (with k = 1); new reports never
 * create raw rows.
 */
export function aggregateRawReports(
  rows: RawReportRow[],
  k: number = K_PUBLISH_THRESHOLD,
): AggregateRow[] {
  const groups = new Map<string, { row: AggregateRow; tokens: Set<string> }>();
  for (const r of dedupeRawReports(rows)) {
    const key = `${r.week}|${r.signal}|${r.mbid.toLowerCase()}`;
    let g = groups.get(key);
    if (!g) {
      g = {
        row: {
          week: r.week,
          signal: r.signal,
          mbid: r.mbid.toLowerCase(),
          nodeCount: 0,
        },
        tokens: new Set<string>(),
      };
      groups.set(key, g);
    }
    g.tokens.add(r.token);
  }
  const out: AggregateRow[] = [];
  for (const g of groups.values()) {
    g.row.nodeCount = g.tokens.size;
    if (g.row.nodeCount >= k) out.push(g.row);
  }
  return out.sort(
    (a, b) =>
      a.week.localeCompare(b.week) ||
      a.signal.localeCompare(b.signal) ||
      a.mbid.localeCompare(b.mbid),
  );
}

/** Trending score: adds_this_week / (1 + average weekly adds over prior weeks). */
export function trendingScore(
  addsThisWeek: number,
  priorWeekAdds: number[],
): number {
  const avg =
    priorWeekAdds.length === 0
      ? 0
      : priorWeekAdds.reduce((a, b) => a + b, 0) / priorWeekAdds.length;
  return addsThisWeek / (1 + avg);
}

export interface TrendingItem {
  mbid: string;
  addsThisWeek: number;
  score: number;
  reason: string;
}

const HEAT_SIGNALS: readonly SignalType[] = [
  "watchlist_add",
  "wishlist_add",
  "download_grab",
];

/**
 * One week's heat for one MBID: watchlist adds plus the LARGER of wishlist
 * adds and download grabs (DESIGN-DOWNLOAD-SIGNALS.md §6). A node that
 * wanted an album and then grabbed it is one node's heat, not two; grabs
 * from non-wishlisters still count in full.
 */
function heatComponents(
  aggregates: AggregateRow[],
): Map<string, Map<string, { watch: number; wish: number; grab: number }>> {
  const out = new Map<
    string,
    Map<string, { watch: number; wish: number; grab: number }>
  >();
  for (const a of aggregates) {
    if (!HEAT_SIGNALS.includes(a.signal)) continue;
    let byWeek = out.get(a.mbid);
    if (!byWeek) {
      byWeek = new Map();
      out.set(a.mbid, byWeek);
    }
    const c = byWeek.get(a.week) ?? { watch: 0, wish: 0, grab: 0 };
    if (a.signal === "watchlist_add") c.watch += a.nodeCount;
    else if (a.signal === "wishlist_add") c.wish += a.nodeCount;
    else c.grab += a.nodeCount;
    byWeek.set(a.week, c);
  }
  return out;
}

function heatOf(
  components: Map<string, Map<string, { watch: number; wish: number; grab: number }>>,
  mbid: string,
  week: string,
): number {
  const c = components.get(mbid)?.get(week);
  return c ? c.watch + Math.max(c.wish, c.grab) : 0;
}

/**
 * Trending rail: per-MBID heat (see heatOf) for `week`, scored against the
 * same MBID's heat in the prior weeks present in `aggregates` (callers
 * should pass the prior 3 weeks; if `priorWeeks` is given, only those
 * weeks count as baseline). Aggregates are already k-filtered, so
 * everything here is publishable.
 */
export function computeTrending(
  aggregates: AggregateRow[],
  week: string,
  priorWeeks?: string[],
): TrendingItem[] {
  const components = heatComponents(aggregates);
  const items: TrendingItem[] = [];
  for (const [mbid, byWeek] of components) {
    const adds = heatOf(components, mbid, week);
    if (adds <= 0) continue;
    const priorCounts = priorWeeks
      ? priorWeeks.map((w) => heatOf(components, mbid, w))
      : [...byWeek.keys()]
          .filter((w) => w !== week)
          .map((w) => heatOf(components, mbid, w));
    items.push({
      mbid,
      addsThisWeek: adds,
      score: trendingScore(adds, priorCounts),
      reason: `Added by ${adds} collectors this week`,
    });
  }
  return items.sort(
    (a, b) =>
      b.score - a.score || b.addsThisWeek - a.addsThisWeek || a.mbid.localeCompare(b.mbid),
  );
}

export interface MostWantedItem {
  mbid: string;
  nodeCount: number;
  huntDemand: number;
  reason: string;
}

/** Most Wanted rail: wishlist_add + hunt_demand per MBID for `week`. */
export function computeMostWanted(
  aggregates: AggregateRow[],
  week: string,
): MostWantedItem[] {
  const byMbid = new Map<string, { total: number; hunt: number }>();
  for (const a of aggregates) {
    if (a.week !== week) continue;
    if (a.signal !== "wishlist_add" && a.signal !== "hunt_demand") continue;
    const entry = byMbid.get(a.mbid) ?? { total: 0, hunt: 0 };
    entry.total += a.nodeCount;
    if (a.signal === "hunt_demand") entry.hunt += a.nodeCount;
    byMbid.set(a.mbid, entry);
  }
  const items: MostWantedItem[] = [];
  for (const [mbid, { total, hunt }] of byMbid) {
    items.push({
      mbid,
      nodeCount: total,
      huntDemand: hunt,
      reason:
        hunt > 0
          ? `Wishlisted by ${total} collectors, still hard to find`
          : `Wishlisted by ${total} collectors this week`,
    });
  }
  return items.sort(
    (a, b) => b.nodeCount - a.nodeCount || a.mbid.localeCompare(b.mbid),
  );
}

export interface BreakoutItem {
  mbid: string;
  nodeCount: number;
  previousNodeCount: number;
  /** Week-over-week growth factor, or null when the previous week was 0. */
  growth: number | null;
  reason: string;
}

function formatGrowth(growth: number): string {
  return Number.isInteger(growth) ? String(growth) : growth.toFixed(1);
}

/**
 * Breakouts rail: watchlist_add for `week` vs `previousWeek`.
 * Breakout = current >= BREAKOUT_MIN_NODES and (previous == 0 or
 * current / previous >= BREAKOUT_GROWTH_FACTOR).
 */
export function computeBreakouts(
  aggregates: AggregateRow[],
  week: string,
  previousWeek: string,
): BreakoutItem[] {
  const current = new Map<string, number>();
  const previous = new Map<string, number>();
  for (const a of aggregates) {
    if (a.signal !== "watchlist_add") continue;
    if (a.week === week) current.set(a.mbid, a.nodeCount);
    if (a.week === previousWeek) previous.set(a.mbid, a.nodeCount);
  }
  const items: BreakoutItem[] = [];
  for (const [mbid, count] of current) {
    if (count < BREAKOUT_MIN_NODES) continue;
    const prev = previous.get(mbid) ?? 0;
    const growth = prev === 0 ? null : count / prev;
    if (growth !== null && growth < BREAKOUT_GROWTH_FACTOR) continue;
    items.push({
      mbid,
      nodeCount: count,
      previousNodeCount: prev,
      growth,
      reason:
        growth === null
          ? `Breakout artist: added by ${count} collectors this week`
          : `Watchlist adds grew ${formatGrowth(growth)}x this week — added by ${count} collectors`,
    });
  }
  return items.sort(
    (a, b) => b.nodeCount - a.nodeCount || a.mbid.localeCompare(b.mbid),
  );
}

/** Keep-rate reason helper from the sketch (used by Discover cards later). */
export function keepRateReason(keepNodes: number, importNodes: number): string | null {
  if (importNodes < K_PUBLISH_THRESHOLD || importNodes === 0) return null;
  const pct = Math.round((keepNodes / importNodes) * 100);
  return `Kept by ${pct}% of collectors who downloaded it`;
}

/* ------------------------------------------------------------------ */
/* Portal keys (PORTAL-DESIGN.md §2.2, §3)                             */
/*                                                                     */
/* Per-user keys issued by the portal app, stored ONLY as hashes in    */
/* portal_keys. SIN verifies a presented key at the door: SHA-256 hex  */
/* of pepper + raw key, one indexed lookup by hash, and every failure  */
/* mode (unknown / revoked / expired) collapses to the same 401 —     */
/* no oracle (roll-call's validateApiKey rule). Identity stops at the  */
/* gate: the lookup result feeds pass/fail and the last_used_at touch  */
/* ONLY; it never enters the report insert path (design §4, P1–P3).   */
/* ------------------------------------------------------------------ */

import { createHash } from "node:crypto";

/** SHA-256 hex of `pepper + rawKey` — the only form of a portal key SIN
 *  (or the database) ever holds. Copied from roll-call's pepperedHash. */
export function portalKeyHash(rawKey: string, pepper: string): string {
  return createHash("sha256").update(pepper + rawKey).digest("hex");
}

/** The validity rule, exactly roll-call's (§2.2): a key is valid iff it
 *  has not been revoked and has not expired. There is deliberately no
 *  isActive/status flag — a single nullable timestamp cannot contradict
 *  itself. */
export function isPortalKeyValid(
  row: { revokedAt: Date | null; expiresAt: Date | null },
  now: Date = new Date(),
): boolean {
  if (row.revokedAt !== null) return false;
  if (row.expiresAt !== null && row.expiresAt.getTime() <= now.getTime()) {
    return false;
  }
  return true;
}

/** How stale `last_used_at` may be before a successful verification
 *  refreshes it (§3.3): the throttle lives in the UPDATE's WHERE clause. */
export const LAST_USED_TOUCH_INTERVAL_MS = 60 * 60 * 1000;

/* Per-key request-count rate limits (§3.2). Tripwires, not quotas: a
 * healthy node sends ONE report per week, so these caps only bite on
 * abuse or a runaway client. Counters are keyed by key id (or a fixed
 * bucket for env keys) and count REQUESTS only — never signals, never
 * report content (§4, P4/P5). Fixed windows, in-memory per isolate
 * (roll-call uses Upstash Redis for the same job; SIN has no Redis, and
 * a stolen key is stopped cold by admin revoke regardless). */
export const REPORTS_PER_HOUR_PER_KEY = 10;
export const REPORTS_PER_DAY_PER_KEY = 50;
export const WHOAMI_PER_HOUR_PER_KEY = 60;
/** Exchange tripwire (§3A.8): a healthy node exchanges about once a day
 *  (its token lasts 24h), so 10/hour only bites on abuse or a runaway
 *  client. Applied per key fingerprint AND per node id. */
export const EXCHANGES_PER_HOUR = 10;

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

/** Fixed-window per-bucket request counter. Pure apart from its own map,
 *  so it is unit-testable with injected timestamps. */
export class RequestRateLimiter {
  private windows = new Map<string, { windowStart: number; count: number }>();

  /** Count one request for `bucket` in the window containing `now`.
   *  Returns true while the count is within `limit`, false once the
   *  limit is exceeded (the request that trips it is also counted).
   *  `scope` namespaces the counters per route, so report and whoami
   *  windows never share a count for the same bucket. */
  allow(
    scope: string,
    bucket: string,
    windowMs: number,
    limit: number,
    now: Date,
  ): boolean {
    const windowStart = Math.floor(now.getTime() / windowMs) * windowMs;
    const key = `${scope}|${bucket}|${windowMs}`;
    const current = this.windows.get(key);
    if (!current || current.windowStart !== windowStart) {
      this.windows.set(key, { windowStart, count: 1 });
      return true;
    }
    current.count += 1;
    return current.count <= limit;
  }

  /** One POST /v1/report attempt: must fit BOTH the hourly and the
   *  daily cap (§3.2). */
  allowReport(bucket: string, now: Date): boolean {
    const hourly = this.allow("report", bucket, HOUR_MS, REPORTS_PER_HOUR_PER_KEY, now);
    const daily = this.allow("report", bucket, DAY_MS, REPORTS_PER_DAY_PER_KEY, now);
    return hourly && daily;
  }

  /** One GET /v1/whoami attempt (§3.2). */
  allowWhoami(bucket: string, now: Date): boolean {
    return this.allow("whoami", bucket, HOUR_MS, WHOAMI_PER_HOUR_PER_KEY, now);
  }

  /** One POST /v1/auth/exchange attempt (§3A.8). Callers invoke this
   *  separately for the key bucket and the node bucket; both must fit. */
  allowExchange(bucket: string, now: Date): boolean {
    return this.allow("exchange", bucket, HOUR_MS, EXCHANGES_PER_HOUR, now);
  }
}

/* ------------------------------------------------------------------ */
/* Canonical playlists (DESIGN-NODE.md §9)                             */
/*                                                                     */
/* The official MetaSync (admin key) publishes the shared playlist     */
/* definitions once per week; every user node PULLS them and           */
/* materializes local copies in its own SoulSync. User nodes can never */
/* publish — POST /v1/playlists accepts the admin key only.            */
/* ------------------------------------------------------------------ */

/** The only canonical playlist keys that exist. */
export const PLAYLIST_KEYS = ["trending", "most-wanted", "breakouts"] as const;

export type PlaylistKey = (typeof PLAYLIST_KEYS)[number];

/** A publish carries at most one playlist per key. */
export const MAX_PLAYLISTS_PER_PUBLISH = 3;

/** Hard cap on tracks in one canonical playlist. */
export const MAX_TRACKS_PER_PLAYLIST = 500;

/** Sensible cap on a playlist display name. */
export const MAX_PLAYLIST_NAME_CHARS = 200;

/** Sensible caps on track text fields (Spotify-shaped payloads are short). */
export const MAX_TRACK_FIELD_CHARS = 512;

/** Hard cap on a single POST /v1/playlists body. 3 playlists x 500 tracks
 *  of Spotify-shaped JSON is a few MB at most; this rejects abuse/bloat. */
export const MAX_PLAYLISTS_BODY_BYTES = 8 * 1024 * 1024;

export interface PlaylistTrackArtist {
  name: string;
}

export interface PlaylistTrackAlbumImage {
  url: string;
}

export interface PlaylistTrackAlbum {
  name: string;
  images?: PlaylistTrackAlbumImage[];
}

/**
 * One track in a canonical playlist — the Spotify-shaped subset SoulSync's
 * playlist sync endpoint consumes (see DESIGN-NODE.md §5). `album` is
 * optional on the wire, but when present it must be well-formed.
 */
export interface PlaylistTrack {
  id: string;
  name: string;
  artists: PlaylistTrackArtist[];
  album?: PlaylistTrackAlbum;
  duration_ms?: number;
  image_url?: string;
}

export interface CanonicalPlaylist {
  /** A flagship key (PLAYLIST_KEYS) or a composer edition key
   *  (e.g. "trending-rock", "most-wanted-1990s"). The manual publish
   *  endpoint still accepts only PLAYLIST_KEYS; the composer writes the
   *  wider catalog directly. */
  key: string;
  name: string;
  tracks: PlaylistTrack[];
}

export interface PlaylistsPublishInput {
  week: string;
  playlists: CanonicalPlaylist[];
}

export type PlaylistsValidationResult =
  | { ok: true; value: PlaylistsPublishInput }
  | { ok: false; errors: string[] };

export function isPlaylistKey(value: unknown): value is PlaylistKey {
  return (
    typeof value === "string" &&
    (PLAYLIST_KEYS as readonly string[]).includes(value)
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown, maxChars: number): value is string {
  return (
    typeof value === "string" && value.length > 0 && value.length <= maxChars
  );
}

const TRACK_FIELDS = ["id", "name", "artists", "album", "duration_ms", "image_url"];

/** Validate one track; returns the normalized track, or null (errors pushed). */
function validateTrack(
  raw: unknown,
  path: string,
  errors: string[],
): PlaylistTrack | null {
  if (!isPlainObject(raw)) {
    errors.push(`${path} must be an object`);
    return null;
  }
  for (const key of Object.keys(raw)) {
    if (!TRACK_FIELDS.includes(key)) {
      errors.push(`${path} has unknown field: ${key}`);
    }
  }
  let ok = true;
  if (!isNonEmptyString(raw.id, MAX_TRACK_FIELD_CHARS)) {
    errors.push(`${path}.id must be a non-empty string`);
    ok = false;
  }
  if (!isNonEmptyString(raw.name, MAX_TRACK_FIELD_CHARS)) {
    errors.push(`${path}.name must be a non-empty string`);
    ok = false;
  }

  let artists: PlaylistTrackArtist[] | null = null;
  if (!Array.isArray(raw.artists) || raw.artists.length === 0) {
    errors.push(`${path}.artists must be a non-empty array`);
    ok = false;
  } else {
    artists = [];
    raw.artists.forEach((rawArtist, j) => {
      const apath = `${path}.artists[${j}]`;
      if (!isPlainObject(rawArtist)) {
        errors.push(`${apath} must be an object`);
        ok = false;
        return;
      }
      for (const key of Object.keys(rawArtist)) {
        if (key !== "name") errors.push(`${apath} has unknown field: ${key}`);
      }
      if (!isNonEmptyString(rawArtist.name, MAX_TRACK_FIELD_CHARS)) {
        errors.push(`${apath}.name must be a non-empty string`);
        ok = false;
        return;
      }
      artists!.push({ name: rawArtist.name });
    });
  }

  let album: PlaylistTrackAlbum | undefined;
  if (raw.album !== undefined) {
    if (!isPlainObject(raw.album)) {
      errors.push(`${path}.album must be an object`);
      ok = false;
    } else {
      for (const key of Object.keys(raw.album)) {
        if (key !== "name" && key !== "images") {
          errors.push(`${path}.album has unknown field: ${key}`);
        }
      }
      if (!isNonEmptyString(raw.album.name, MAX_TRACK_FIELD_CHARS)) {
        errors.push(`${path}.album.name must be a non-empty string`);
        ok = false;
      } else {
        album = { name: raw.album.name };
        if (raw.album.images !== undefined) {
          if (!Array.isArray(raw.album.images)) {
            errors.push(`${path}.album.images must be an array`);
            ok = false;
          } else {
            const images: PlaylistTrackAlbumImage[] = [];
            let imagesOk = true;
            raw.album.images.forEach((rawImage, j) => {
              const ipath = `${path}.album.images[${j}]`;
              if (!isPlainObject(rawImage)) {
                errors.push(`${ipath} must be an object`);
                imagesOk = false;
                return;
              }
              for (const key of Object.keys(rawImage)) {
                if (key !== "url") {
                  errors.push(`${ipath} has unknown field: ${key}`);
                }
              }
              if (!isNonEmptyString(rawImage.url, MAX_TRACK_FIELD_CHARS)) {
                errors.push(`${ipath}.url must be a non-empty string`);
                imagesOk = false;
                return;
              }
              images.push({ url: rawImage.url });
            });
            if (imagesOk) album.images = images;
            else ok = false;
          }
        }
      }
    }
  }

  if (raw.duration_ms !== undefined) {
    if (
      typeof raw.duration_ms !== "number" ||
      !Number.isInteger(raw.duration_ms) ||
      raw.duration_ms < 0
    ) {
      errors.push(`${path}.duration_ms must be a non-negative integer`);
      ok = false;
    }
  }
  if (raw.image_url !== undefined) {
    if (!isNonEmptyString(raw.image_url, MAX_TRACK_FIELD_CHARS)) {
      errors.push(`${path}.image_url must be a non-empty string`);
      ok = false;
    }
  }

  if (!ok || artists === null) return null;
  const track: PlaylistTrack = {
    id: raw.id as string,
    name: raw.name as string,
    artists,
  };
  if (album !== undefined) track.album = album;
  if (raw.duration_ms !== undefined) track.duration_ms = raw.duration_ms as number;
  if (raw.image_url !== undefined) track.image_url = raw.image_url as string;
  return track;
}

/**
 * Validate an untrusted POST /v1/playlists payload (admin publish).
 *
 * Same strict style as validateReport: unknown fields are rejected at
 * every level, so nothing beyond the canonical Spotify-shaped track
 * subset can be smuggled into the shared playlist definitions.
 */
export function validatePlaylistsPublish(
  input: unknown,
): PlaylistsValidationResult {
  const errors: string[] = [];
  if (!isPlainObject(input)) {
    return { ok: false, errors: ["payload must be a JSON object"] };
  }
  for (const key of Object.keys(input)) {
    if (key !== "week" && key !== "playlists") {
      errors.push(`unknown field: ${key}`);
    }
  }
  if (!isIsoWeekString(input.week)) {
    errors.push("week must be an ISO week string like 2026-W43");
  }
  if (!Array.isArray(input.playlists)) {
    errors.push("playlists must be an array");
    return { ok: false, errors };
  }
  if (input.playlists.length === 0) {
    errors.push("playlists must not be empty");
  }
  if (input.playlists.length > MAX_PLAYLISTS_PER_PUBLISH) {
    errors.push(
      `playlists exceeds max of ${MAX_PLAYLISTS_PER_PUBLISH} per publish`,
    );
  }

  const playlists: CanonicalPlaylist[] = [];
  const seenKeys = new Set<string>();
  input.playlists.forEach((raw, i) => {
    const path = `playlists[${i}]`;
    if (!isPlainObject(raw)) {
      errors.push(`${path} must be an object`);
      return;
    }
    for (const key of Object.keys(raw)) {
      if (key !== "key" && key !== "name" && key !== "tracks") {
        errors.push(`${path} has unknown field: ${key}`);
      }
    }
    if (!isPlaylistKey(raw.key)) {
      errors.push(
        `${path}.key must be one of: ${PLAYLIST_KEYS.join(", ")}`,
      );
      return;
    }
    if (seenKeys.has(raw.key)) {
      errors.push(`duplicate playlist key: ${raw.key}`);
    }
    seenKeys.add(raw.key);
    if (!isNonEmptyString(raw.name, MAX_PLAYLIST_NAME_CHARS)) {
      errors.push(
        `${path}.name must be a non-empty string of at most ${MAX_PLAYLIST_NAME_CHARS} chars`,
      );
      return;
    }
    if (!Array.isArray(raw.tracks)) {
      errors.push(`${path}.tracks must be an array`);
      return;
    }
    if (raw.tracks.length > MAX_TRACKS_PER_PLAYLIST) {
      errors.push(
        `${path}.tracks exceeds max of ${MAX_TRACKS_PER_PLAYLIST} tracks per playlist`,
      );
      return;
    }
    const before = errors.length;
    const tracks: PlaylistTrack[] = [];
    raw.tracks.forEach((rawTrack, j) => {
      const track = validateTrack(rawTrack, `${path}.tracks[${j}]`, errors);
      if (track) tracks.push(track);
    });
    if (errors.length !== before) return;
    playlists.push({ key: raw.key, name: raw.name, tracks });
  });

  if (errors.length > 0) return { ok: false, errors };
  return {
    ok: true,
    value: { week: input.week as string, playlists },
  };
}

/* ------------------------------------------------------------------ */
/* MetaSync-only access: node signatures + pool access tokens (§3A)    */
/*                                                                     */
/* The exchange (§3A.4) trades a raw portal key — presented once,      */
/* together with a fresh Ed25519 signature from the node bound to      */
/* that key — for a short-lived pool-signed access token. Routine      */
/* calls carry the token plus a fresh node signature, never the raw    */
/* key. Signatures, tokens and bindings are door checks only (P7):     */
/* nothing in this section is ever written to a SIN data table.        */
/* ------------------------------------------------------------------ */

import {
  createPrivateKey,
  createPublicKey,
  sign as cryptoSign,
  verify as cryptoVerify,
  type JsonWebKeyInput,
  type KeyObject,
} from "node:crypto";

/** Access-token lifetime at launch (§3A.4): 24 hours. */
export const ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60;

/** Node-signature clock window (§3A.2): ±5 minutes. */
export const NODE_TIMESTAMP_WINDOW_SECONDS = 5 * 60;

/** Scopes a user portal key can receive at exchange (§3A.4). */
export const USER_TOKEN_SCOPES = [
  "report:write",
  "index:read",
  "playlists:read",
  "whoami:read",
  // The metadata network's claims ingest (DESIGN-METADATA §6) lives in
  // the separate `metasync` function; the exchange is the one token
  // issuer for both services, so the scope is granted here. Holding
  // the scope is not enough to contribute — the metadata function
  // additionally requires the account's contributor_approved flag.
  "meta:write",
] as const;

/** Scopes the official MetaSync receives when it exchanges its admin
 *  credential (§3A.4): the user scopes plus aggregation + publishing. */
export const ADMIN_TOKEN_SCOPES = [
  ...USER_TOKEN_SCOPES,
  "aggregate:write",
  "playlists:publish",
] as const;

export type TokenScope =
  | (typeof USER_TOKEN_SCOPES)[number]
  | "aggregate:write"
  | "playlists:publish";

/** The exact claim set of a SIN access token (§3A.4) — nothing else. */
export interface AccessTokenClaims {
  key_id: string;
  node_id: string;
  scopes: string[];
  exp: number;
  iat: number;
}

const BASE64URL_SHAPE = /^[A-Za-z0-9_-]+={0,2}$/;

/** Encode bytes as base64url (unpadded, the JWS convention). */
export function base64UrlEncode(data: Uint8Array | string): string {
  const bytes = typeof data === "string" ? Buffer.from(data, "utf8") : Buffer.from(data);
  return bytes.toString("base64url");
}

/**
 * Strict base64url decode. Accepts optional padding (some clients emit
 * padded urlsafe base64) but rejects anything that does not re-encode
 * to the same value, so malformed or non-canonical input cannot slip
 * through Node's lenient decoder.
 */
export function decodeBase64Url(
  value: unknown,
  expectedLength?: number,
): Buffer | null {
  if (typeof value !== "string" || !BASE64URL_SHAPE.test(value)) return null;
  const unpadded = value.replace(/=+$/, "");
  if (unpadded.length % 4 === 1) return null;
  let bytes: Buffer;
  try {
    bytes = Buffer.from(unpadded, "base64url");
  } catch {
    return null;
  }
  if (bytes.toString("base64url") !== unpadded) return null;
  if (expectedLength !== undefined && bytes.length !== expectedLength) {
    return null;
  }
  return bytes;
}

/** SHA-256 hex of a string or byte buffer. */
export function sha256Hex(data: string | Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** node_id (§3A.2): SHA-256 hex of the node's raw Ed25519 public key. */
export function nodeIdForPublicKey(publicKeyRaw: Uint8Array): string {
  return sha256Hex(publicKeyRaw);
}

const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const ED25519_PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

/** Build a verify-ready KeyObject from a raw 32-byte Ed25519 public key. */
export function ed25519PublicKeyObject(publicKeyRaw: Uint8Array): KeyObject | null {
  if (publicKeyRaw.length !== 32) return null;
  try {
    return createPublicKey({
      key: Buffer.concat([ED25519_SPKI_PREFIX, Buffer.from(publicKeyRaw)]),
      format: "der",
      type: "spki",
    });
  } catch {
    return null;
  }
}

/**
 * The canonical string a node signs (shared contract):
 *   METHOD "\n" PATH+QUERY "\n" TIMESTAMP "\n" NONCE "\n" SHA256_HEX(body)
 * The body hash covers the raw request body; an empty body (GETs) is
 * the SHA-256 of "".
 */
export function canonicalNodeRequest(
  method: string,
  pathAndQuery: string,
  timestamp: string,
  nonce: string,
  body: string | Uint8Array,
): string {
  return [
    method.toUpperCase(),
    pathAndQuery,
    timestamp,
    nonce,
    sha256Hex(body),
  ].join("\n");
}

/** Verify an Ed25519 node signature over the canonical request string. */
export function verifyNodeSignature(input: {
  publicKeyRaw: Uint8Array;
  method: string;
  pathAndQuery: string;
  timestamp: string;
  nonce: string;
  body: string | Uint8Array;
  signature: Uint8Array;
}): boolean {
  const key = ed25519PublicKeyObject(input.publicKeyRaw);
  if (!key || input.signature.length !== 64) return false;
  const canonical = canonicalNodeRequest(
    input.method,
    input.pathAndQuery,
    input.timestamp,
    input.nonce,
    input.body,
  );
  try {
    return cryptoVerify(null, Buffer.from(canonical, "utf8"), key, Buffer.from(input.signature));
  } catch {
    return false;
  }
}

/** True when a unix-seconds timestamp is inside the ±5-minute window. */
export function isTimestampWithinWindow(
  timestampSeconds: number,
  now: Date,
  windowSeconds: number = NODE_TIMESTAMP_WINDOW_SECONDS,
): boolean {
  if (!Number.isSafeInteger(timestampSeconds) || timestampSeconds < 0) {
    return false;
  }
  const nowSeconds = Math.floor(now.getTime() / 1000);
  return Math.abs(nowSeconds - timestampSeconds) <= windowSeconds;
}

/**
 * Load the pool's token-signing private key from SIN_TOKEN_PRIVATE_KEY.
 * Accepted formats (all are things node:crypto produces or consumes):
 *   - base64url of the raw 32-byte private seed (the JWK `d` value);
 *   - base64url of a PKCS#8 DER export;
 *   - a JWK JSON string ({"kty":"OKP","crv":"Ed25519","d":…,"x":…});
 *   - a PEM string (actual newlines, or literal \n escapes from env
 *     files). A base64url wrapper around either text form also works.
 * Returns null for anything unset, malformed, or not Ed25519 — callers
 * fail closed (503 token_not_configured).
 */
export function loadPoolTokenPrivateKey(
  rawEnv: string | undefined,
): KeyObject | null {
  if (!rawEnv) return null;
  const value = rawEnv.trim();
  if (!value) return null;
  try {
    let key: KeyObject;
    if (value.startsWith("{")) {
      key = createPrivateKey({
        key: JSON.parse(value) as JsonWebKeyInput["key"],
        format: "jwk",
      });
    } else if (value.startsWith("-----")) {
      key = createPrivateKey(value.replace(/\\n/g, "\n"));
    } else {
      const decoded = decodeBase64Url(value);
      if (!decoded) return null;
      const asText = decoded.toString("utf8").trim();
      if (asText.startsWith("{") || asText.startsWith("-----")) {
        return loadPoolTokenPrivateKey(asText);
      }
      key =
        decoded.length === 32
          ? createPrivateKey({
              key: Buffer.concat([ED25519_PKCS8_PREFIX, decoded]),
              format: "der",
              type: "pkcs8",
            })
          : createPrivateKey({ key: decoded, format: "der", type: "pkcs8" });
    }
    return key.asymmetricKeyType === "ed25519" ? key : null;
  } catch {
    return null;
  }
}

/** The pool's raw 32-byte Ed25519 public key, derived from its private key. */
export function poolTokenPublicKeyRaw(privateKey: KeyObject): Buffer | null {
  try {
    const jwk = createPublicKey(privateKey).export({ format: "jwk" }) as {
      x?: string;
    };
    if (!jwk.x) return null;
    return decodeBase64Url(jwk.x, 32);
  } catch {
    return null;
  }
}

/** Issue a signed access token. Returns the compact token + its expiry. */
export function issueAccessToken(
  privateKey: KeyObject,
  input: { keyId: string; nodeId: string; scopes: readonly string[] },
  now: Date = new Date(),
): { accessToken: string; expiresAt: Date; claims: AccessTokenClaims } {
  const iat = Math.floor(now.getTime() / 1000);
  const claims: AccessTokenClaims = {
    key_id: input.keyId,
    node_id: input.nodeId,
    scopes: [...input.scopes],
    exp: iat + ACCESS_TOKEN_TTL_SECONDS,
    iat,
  };
  const header = { alg: "EdDSA", typ: "SIN" };
  const signingInput = `${base64UrlEncode(JSON.stringify(header))}.${base64UrlEncode(JSON.stringify(claims))}`;
  const signature = cryptoSign(null, Buffer.from(signingInput, "utf8"), privateKey);
  return {
    accessToken: `${signingInput}.${base64UrlEncode(signature)}`,
    expiresAt: new Date(claims.exp * 1000),
    claims,
  };
}

/**
 * Verify a compact access token against the pool's key. Returns the
 * claims, or null for malformed / wrongly signed / expired tokens —
 * callers collapse every failure to the same 401 (no oracle). NOTE:
 * this checks the token itself only; the per-request key-status and
 * binding check (§3A.4 immediate death) is the caller's job.
 */
export function verifyAccessToken(
  token: unknown,
  privateKey: KeyObject,
  now: Date = new Date(),
): AccessTokenClaims | null {
  if (typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [headerPart, payloadPart, signaturePart] = parts as [string, string, string];
  const signature = decodeBase64Url(signaturePart, 64);
  if (!signature) return null;
  let header: unknown;
  let payload: unknown;
  try {
    header = JSON.parse(Buffer.from(headerPart, "base64url").toString("utf8"));
    payload = JSON.parse(Buffer.from(payloadPart, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (
    typeof header !== "object" || header === null ||
    (header as { alg?: unknown }).alg !== "EdDSA" ||
    (header as { typ?: unknown }).typ !== "SIN"
  ) {
    return null;
  }
  try {
    const ok = cryptoVerify(
      null,
      Buffer.from(`${headerPart}.${payloadPart}`, "utf8"),
      createPublicKey(privateKey),
      signature,
    );
    if (!ok) return null;
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const claims = payload as Partial<AccessTokenClaims>;
  if (
    typeof claims.key_id !== "string" || claims.key_id.length === 0 ||
    typeof claims.node_id !== "string" || claims.node_id.length === 0 ||
    !Array.isArray(claims.scopes) ||
    !claims.scopes.every((s) => typeof s === "string") ||
    typeof claims.exp !== "number" || !Number.isSafeInteger(claims.exp) ||
    typeof claims.iat !== "number" || !Number.isSafeInteger(claims.iat) ||
    claims.iat > claims.exp
  ) {
    return null;
  }
  if (claims.exp <= Math.floor(now.getTime() / 1000)) return null;
  return claims as AccessTokenClaims;
}

/* ------------------------------------------------------------------ */
/* Composer: weekly chart selection (auto-publish)                     */
/*                                                                     */
/* The composer turns one week's k-filtered aggregates into chart      */
/* specs: ranked MBID lists, nothing more. Turning MBIDs into track    */
/* lists is the resolver's job (mb.ts); a spec only publishes once     */
/* every item on it has resolved. Work that is identical for every     */
/* user happens once, here in the middle.                              */
/* ------------------------------------------------------------------ */

export const CHART_ALBUM_LIMIT = 25;
export const CHART_ARTIST_LIMIT = 10;
export const CHART_MIN_EDITION_ITEMS = 8;
/** A published playlist holds at most this many tracks: a chart's 25
 *  albums at 2 tracks each (mb.ts CHART_TRACKS_PER_ALBUM). */
export const CHART_MAX_TRACKS = 50;
export const RISING_GROWTH_FACTOR = 2;

export interface ChartItem {
  mbid: string;
  kind: "release-group" | "artist";
  score: number;
  /** "public" = a cold-start fill from ListenBrainz's public weekly
   *  statistics (fillFromPublic); absent = counted by the SIN network. */
  source?: "public";
}

export interface ChartSpec {
  key: string;
  name: string;
  items: ChartItem[];
  /** An edition's base chart key ("trending" for "trending-rock"). */
  base?: string;
}

function sumsFor(
  aggregates: AggregateRow[],
  week: string,
  signals: readonly SignalType[],
): Map<string, number> {
  const out = new Map<string, number>();
  for (const a of aggregates) {
    if (a.week !== week || !signals.includes(a.signal)) continue;
    out.set(a.mbid, (out.get(a.mbid) ?? 0) + a.nodeCount);
  }
  return out;
}

/**
 * Per-MBID heat for one week without the watchlist component (release-group
 * charts): max(wishlist_add, download_grab). See heatOf for the rationale.
 */
function grabHeatSums(aggregates: AggregateRow[], week: string): Map<string, number> {
  const wish = sumsFor(aggregates, week, ["wishlist_add"]);
  const grab = sumsFor(aggregates, week, ["download_grab"]);
  const out = new Map<string, number>();
  for (const mbid of new Set([...wish.keys(), ...grab.keys()])) {
    out.set(mbid, Math.max(wish.get(mbid) ?? 0, grab.get(mbid) ?? 0));
  }
  return out;
}

function topItems(
  sums: Map<string, number>,
  kind: ChartItem["kind"],
  limit: number,
): ChartItem[] {
  return [...sums.entries()]
    .map(([mbid, score]) => ({ mbid, kind, score }))
    .sort((a, b) => b.score - a.score || a.mbid.localeCompare(b.mbid))
    .slice(0, limit);
}

/** How many installs reported a week, and how many of them share drop
 *  signals (report_populations). Counts only: never which installs. */
export interface WeekPopulation {
  reports: number;
  churnOptin: number;
}

/** Churn is reported by the opted-in share only; one drop stands for
 *  reports/churnOptin drops across everyone. Capped, so a handful of
 *  opted-in installs can never multiply a drop past CHURN_SCALE_MAX.
 *  No population data (weeks before it was counted) means 1, the old
 *  unscaled rule. */
export const CHURN_SCALE_MAX = 5;
export function churnScale(population?: WeekPopulation): number {
  if (!population || population.churnOptin <= 0 || population.reports <= 0) return 1;
  return Math.min(CHURN_SCALE_MAX, Math.max(1, population.reports / population.churnOptin));
}

/**
 * Build the week's flagship chart specs from k-filtered aggregates.
 * Trending here ranks release groups by heat (max of wishlist adds and
 * download grabs) scored against
 * prior weeks (artists chart separately as Breakouts); the public index
 * blends kinds for display, a playlist cannot.
 */
export function selectChartSpecs(
  aggregates: AggregateRow[],
  week: string,
  priorWeeks: string[],
  previousWeek: string | null,
  population?: WeekPopulation,
): ChartSpec[] {
  const specs: ChartSpec[] = [];
  const push = (key: string, name: string, items: ChartItem[]) => {
    if (items.length > 0) specs.push({ key, name, items });
  };

  push(
    "most-wanted",
    "Most Wanted",
    computeMostWanted(aggregates, week)
      .slice(0, CHART_ALBUM_LIMIT)
      .map((i) => ({ mbid: i.mbid, kind: "release-group" as const, score: i.nodeCount })),
  );

  // Loved & Kept: albums that passed 14 days in installs' libraries this
  // week (import_keep fires once per album, at that mark), net of churn
  // (also once per album, at the drop). download_churn aggregates only
  // exist for albums at/above k within the toggle-on population, so
  // subtracting them never mixes populations; where no churn aggregate
  // exists there is no dampening. Albums whose churn meets or beats
  // their keeps drop out. Churn is a ranking input only — never shown.
  //
  // Keeps come from every reporter, drops only from the opted-in share,
  // so raw churn undercounts drops by that share. churnScale scales it
  // back up from the week's population counts.
  const keeps = sumsFor(aggregates, week, ["import_keep"]);
  const churn = sumsFor(aggregates, week, ["download_churn"]);
  const scale = churnScale(population);
  const netKeeps = new Map<string, number>();
  for (const [mbid, keep] of keeps) {
    const net = keep - (churn.get(mbid) ?? 0) * scale;
    if (net > 0) netKeeps.set(mbid, net);
  }
  push(
    "loved-and-kept",
    "Loved & Kept",
    topItems(netKeeps, "release-group", CHART_ALBUM_LIMIT),
  );

  push(
    "just-landed",
    "Just Landed",
    topItems(sumsFor(aggregates, week, ["recent_add"]), "release-group", CHART_ALBUM_LIMIT),
  );

  // On Repeat: albums the most installs actually played this week
  // (listen_album — opt-in on the node, DESIGN-LISTEN-SIGNALS.md). Like
  // churn, it only exists where >= k opted-in installs played the album.
  push(
    "on-repeat",
    "On Repeat",
    topItems(sumsFor(aggregates, week, ["listen_album"]), "release-group", CHART_ALBUM_LIMIT),
  );

  // Trending: this week's heat per release group (max of wishlist adds
  // and download grabs — see grabHeatSums), scored against the same
  // group's heat in each prior week (trendingScore).
  const adds = grabHeatSums(aggregates, week);
  const priorByMbid = new Map<string, Map<string, number>>();
  for (const w of priorWeeks) {
    for (const [mbid, count] of grabHeatSums(aggregates, w)) {
      let byWeek = priorByMbid.get(mbid);
      if (!byWeek) {
        byWeek = new Map<string, number>();
        priorByMbid.set(mbid, byWeek);
      }
      byWeek.set(w, count);
    }
  }
  const trending = [...adds.entries()]
    .map(([mbid, count]) => ({
      mbid,
      kind: "release-group" as const,
      score: trendingScore(count, priorWeeks.map((w) => priorByMbid.get(mbid)?.get(w) ?? 0)),
      adds: count,
    }))
    .sort((a, b) => b.score - a.score || b.adds - a.adds || a.mbid.localeCompare(b.mbid))
    .slice(0, CHART_ALBUM_LIMIT)
    .map(({ mbid, kind, score }) => ({ mbid, kind, score }));
  push("trending", "Trending in SoulSync", trending);

  if (previousWeek) {
    const demandSignals: readonly SignalType[] = ["wishlist_add", "hunt_demand", "recent_add"];
    const cur = sumsFor(aggregates, week, demandSignals);
    const prev = sumsFor(aggregates, previousWeek, demandSignals);
    const rising = [...cur.entries()]
      .map(([mbid, count]) => {
        const before = prev.get(mbid) ?? 0;
        return { mbid, count, growth: before === 0 ? Infinity : count / before };
      })
      .filter((r) => r.growth === Infinity || r.growth >= RISING_GROWTH_FACTOR)
      .sort((a, b) => b.growth - a.growth || b.count - a.count || a.mbid.localeCompare(b.mbid))
      .slice(0, CHART_ALBUM_LIMIT)
      .map((r) => ({ mbid: r.mbid, kind: "release-group" as const, score: r.count }));
    push("rising", "Rising Fast", rising);

    push(
      "breakouts",
      "Breakout Artists",
      computeBreakouts(aggregates, week, previousWeek)
        .slice(0, CHART_ARTIST_LIMIT)
        .map((i) => ({ mbid: i.mbid, kind: "artist" as const, score: i.nodeCount })),
    );
  }

  return specs;
}

/** Resolved public facts about one charted MBID (from MusicBrainz). */
export interface ResolvedMeta {
  genre?: string;
  year?: number;
}

export function slugifyGenre(genre: string): string {
  return genre
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

const GENRE_DISPLAY: Record<string, string> = { "r&b": "R&B" };

export function genreDisplay(genre: string): string {
  const lower = genre.toLowerCase();
  if (GENRE_DISPLAY[lower]) return GENRE_DISPLAY[lower];
  return lower.replace(/(^|[\s-])([a-z])/g, (_m, p1: string, p2: string) => p1 + p2.toUpperCase());
}

export function decadeLabel(year: number): string {
  return `${Math.floor(year / 10) * 10}s`;
}

/**
 * Genre and decade editions of the two flagship demand charts, built once
 * items have resolved metadata. An edition exists only when enough charted
 * items share the tag (CHART_MIN_EDITION_ITEMS): thin editions are noise,
 * and every item on them already cleared the k threshold individually.
 */
export function buildEditions(
  specs: ChartSpec[],
  meta: Map<string, ResolvedMeta>,
): ChartSpec[] {
  const editions: ChartSpec[] = [];
  for (const base of specs) {
    if (base.key !== "trending" && base.key !== "most-wanted" && base.key !== "just-landed") continue;
    const byGenre = new Map<string, ChartItem[]>();
    const byDecade = new Map<string, ChartItem[]>();
    for (const item of base.items) {
      if (item.kind !== "release-group") continue;
      const m = meta.get(item.mbid);
      if (!m) continue;
      if (m.genre) {
        const list = byGenre.get(m.genre) ?? [];
        list.push(item);
        byGenre.set(m.genre, list);
      }
      if (m.year !== undefined) {
        const label = decadeLabel(m.year);
        const list = byDecade.get(label) ?? [];
        list.push(item);
        byDecade.set(label, list);
      }
    }
    // An edition holding every item of its base is the base again under a
    // second name (every Just Landed album is from the 2020s): skip it.
    const distinct = (items: ChartItem[]) => items.length < base.items.length;
    for (const [genre, items] of byGenre) {
      if (items.length < CHART_MIN_EDITION_ITEMS || !distinct(items)) continue;
      editions.push({
        key: `${base.key}-${slugifyGenre(genre)}`,
        name: `${base.name.split(" in SoulSync")[0]}: ${genreDisplay(genre)}`,
        items,
        base: base.key,
      });
    }
    // Everything that just landed is from this decade: no decade editions.
    if (base.key === "just-landed") continue;
    for (const [label, items] of byDecade) {
      if (items.length < CHART_MIN_EDITION_ITEMS || !distinct(items)) continue;
      editions.push({
        key: `${base.key}-${label}`,
        name: `${base.name.split(" in SoulSync")[0]}: ${label}`,
        items,
        base: base.key,
      });
    }
  }
  return editions;
}

/** Share of a base chart's published tracks an edition may repeat. */
export const EDITION_MAX_OVERLAP = 0.8;

/** True when an edition's published tracks are mostly its base chart's
 *  again. buildEditions only sees candidates; the base publishes its top
 *  50 tracks, so an edition can differ on paper and still play the same
 *  songs (Just Landed: 2020s was Just Landed, 42 of 42). */
export function editionRepeatsBase(editionIds: string[], baseIds: string[]): boolean {
  if (baseIds.length === 0) return false;
  const base = new Set(baseIds);
  const shared = editionIds.filter((id) => base.has(id)).length;
  return shared >= EDITION_MAX_OVERLAP * baseIds.length;
}

/* ------------------------------------------------------------------ */
/* Cold start: public fill for the flagship charts                    */
/*                                                                     */
/* Nothing charts until k installs agree, so a young network publishes  */
/* empty playlists. Trending and Just Landed have an honest public      */
/* equivalent — what ListenBrainz's whole user base played last week,   */
/* and the recent releases among it — so while the network cannot fill  */
/* them, public items take the EMPTY slots only: network items always   */
/* rank first, and the public share falls to zero on its own as the     */
/* network grows. The playlist name says when public data is in it.     */
/* Editions are built from network items only.                          */
/* ------------------------------------------------------------------ */

/** The charts that get a public fill, their base names, and what the
 *  public source is for each. */
export const PUBLIC_FILL_CHARTS = {
  trending: "Trending in SoulSync",
  "just-landed": "Just Landed",
} as const;

export type PublicFillKey = keyof typeof PUBLIC_FILL_CHARTS;

/**
 * Top up a network chart with public candidates (ranked, release-group
 * MBIDs) to `limit` items, skipping anything the network already has.
 * `spec` may be undefined (nothing charted on the network yet).
 */
export function fillFromPublic(
  key: PublicFillKey,
  spec: ChartSpec | undefined,
  candidates: string[],
  limit: number = CHART_ALBUM_LIMIT,
): ChartSpec | undefined {
  const items: ChartItem[] = [...(spec?.items ?? [])];
  const have = new Set(items.map((i) => i.mbid));
  // Public items score below every network item, in their public order.
  for (const mbid of candidates) {
    if (items.length >= limit) break;
    const id = mbid.toLowerCase();
    if (have.has(id)) continue;
    have.add(id);
    items.push({ mbid: id, kind: "release-group", score: 0, source: "public" });
  }
  if (items.length === 0) return undefined;
  return { key, name: spec?.name ?? PUBLIC_FILL_CHARTS[key], items };
}

/**
 * The name a published chart carries, from what actually made it in:
 * pure network → the base name; part public → "· SoulSync + ListenBrainz";
 * all public → "· from ListenBrainz". Applies to the flagship charts and
 * to every edition built from them. Nobody should mistake ListenBrainz's
 * listening for SoulSync's.
 */
export function sourcedName(base: string, networkItems: number, publicItems: number): string {
  if (publicItems === 0) return base;
  const short = base.replace(/ in SoulSync$/, "");
  return networkItems === 0 ? `${short} · from ListenBrainz` : `${short} · SoulSync + ListenBrainz`;
}

/** sourcedName for a flagship fill chart, by key. */
export function chartDisplayName(key: PublicFillKey, networkItems: number, publicItems: number): string {
  return sourcedName(PUBLIC_FILL_CHARTS[key], networkItems, publicItems);
}

/**
 * Lay a chart's tracks out round by round: every item's best track first,
 * in chart order, then every item's second, and so on. The strongest songs
 * lead, one album never plays twice in a row, and the order moves only
 * when the chart does — unlike a shuffle, which would rewrite a synced
 * playlist every day. Duplicate track ids keep their first place.
 */
export function interleaveByRank<T extends { id: string }>(perItem: T[][], limit: number = CHART_MAX_TRACKS): T[] {
  const out: T[] = [];
  const seen = new Set<string>();
  const rounds = Math.max(0, ...perItem.map((tracks) => tracks.length));
  for (let round = 0; round < rounds && out.length < limit; round += 1) {
    for (const tracks of perItem) {
      const track = tracks[round];
      if (!track || seen.has(track.id)) continue;
      seen.add(track.id);
      out.push(track);
      if (out.length >= limit) break;
    }
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* Top Tracks · from ListenBrainz                                      */
/* ------------------------------------------------------------------ */

export const TOP_TRACKS_KEY = "top-tracks";
export const TOP_TRACKS_NAME = "Top Tracks · from ListenBrainz";
/** One artist's fan base can fill a raw weekly song chart (half of
 *  ListenBrainz's top 100 was one band the week this was built), so the
 *  chart takes at most this many songs per artist. */
export const TOP_TRACKS_PER_ARTIST = 2;

export interface PublicRecordingRow {
  recording_mbid?: string | null;
  track_name?: string | null;
  artist_name?: string | null;
  artist_mbids?: string[] | null;
  release_name?: string | null;
  caa_release_mbid?: string | null;
}

/**
 * ListenBrainz's public weekly song chart as a playlist: ranked rows in,
 * the top CHART_MAX_TRACKS songs out, at most TOP_TRACKS_PER_ARTIST per
 * artist, rows without a recording MBID or a name skipped. Public
 * catalogue data only; SIN collects no track-level signal from anyone.
 */
export function topTracksFromRecordings(
  rows: PublicRecordingRow[],
  limit: number = CHART_MAX_TRACKS,
  perArtist: number = TOP_TRACKS_PER_ARTIST,
): PlaylistTrack[] {
  const out: PlaylistTrack[] = [];
  const seen = new Set<string>();
  const perArtistCount = new Map<string, number>();
  for (const row of rows) {
    if (out.length >= limit) break;
    const id = row.recording_mbid?.toLowerCase();
    const name = row.track_name?.trim();
    const artist = row.artist_name?.trim();
    if (!id || !isUuidShape(id) || !name || !artist || seen.has(id)) continue;
    const artistKey = (row.artist_mbids?.[0] ?? artist).toLowerCase();
    const count = perArtistCount.get(artistKey) ?? 0;
    if (count >= perArtist) continue;
    perArtistCount.set(artistKey, count + 1);
    seen.add(id);
    const cover = row.caa_release_mbid && isUuidShape(row.caa_release_mbid)
      ? `https://coverartarchive.org/release/${row.caa_release_mbid}/front-250`
      : undefined;
    const track: PlaylistTrack = { id, name: name.slice(0, MAX_TRACK_FIELD_CHARS), artists: [{ name: artist.slice(0, MAX_TRACK_FIELD_CHARS) }] };
    if (row.release_name?.trim()) {
      track.album = { name: row.release_name.trim().slice(0, MAX_TRACK_FIELD_CHARS), ...(cover ? { images: [{ url: cover }] } : {}) };
    }
    if (cover) track.image_url = cover;
    out.push(track);
  }
  return out;
}

/** Private running counts are kept this long: once a week can no longer
 *  receive reports (REPORT_TOKEN_RETENTION_WEEKS), everything at or above
 *  k is already in `aggregates`, and the below-k remainder has no use. */
export const COUNT_RETENTION_WEEKS = REPORT_TOKEN_RETENTION_WEEKS;

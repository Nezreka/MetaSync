/**
 * MusicBrainz resolution for the composer (see logic.ts selectChartSpecs).
 *
 * The only place SIN talks to MusicBrainz. Everything resolved here is
 * public catalogue data about charted albums/artists, fetched once and
 * cached in mb_resolutions; no user data ever flows into these requests,
 * only MBIDs that already cleared the k publish threshold.
 *
 * Etiquette: MusicBrainz asks callers to average at most one request per
 * second and to send a descriptive User-Agent. The resolver spaces calls
 * by MB_MIN_INTERVAL_MS and identifies as MetaSync with a contact.
 */
import type { PlaylistTrack } from "./logic.ts";

export const MB_MIN_INTERVAL_MS = 1_100;
export const MB_USER_AGENT = "MetaSync/0.2 (composer; contact admin@ssync.net)";
const MB_BASE = "https://musicbrainz.org/ws/2";

export function coverArtUrl(releaseGroupMbid: string): string {
  return `https://coverartarchive.org/release-group/${releaseGroupMbid}/front-500`;
}

export interface ReleaseGroupBundle {
  mbid: string;
  kind: "release-group";
  title: string;
  artistName: string;
  artistMbid?: string;
  genre?: string;
  year?: number;
  tracks: PlaylistTrack[];
  /** The album's most popular tracks (recording MBIDs, best first), from
   *  ListenBrainz's public popularity data — at most POPULAR_TRACKS_CACHED.
   *  Absent = not looked up yet; [] = looked up, no popularity data (the
   *  playlist then takes the album's first tracks). */
  topTrackIds?: string[];
}

export interface ArtistBundle {
  mbid: string;
  kind: "artist";
  name: string;
  genre?: string;
  /** The artist's album release-group MBIDs, MusicBrainz order. */
  releaseGroupMbids: string[];
}

export type MbBundle = ReleaseGroupBundle | ArtistBundle;

type FetchFn = (url: string, init: { headers: Record<string, string> }) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

interface MbArtistCredit {
  name?: string;
  artist?: { id?: string; name?: string };
  joinphrase?: string;
}

function creditName(credit: MbArtistCredit[] | undefined): string {
  if (!credit || credit.length === 0) return "Unknown Artist";
  return credit.map((c) => c.name ?? c.artist?.name ?? "").join("").trim() || "Unknown Artist";
}

function creditArtists(credit: MbArtistCredit[] | undefined): { name: string }[] {
  if (!credit || credit.length === 0) return [{ name: "Unknown Artist" }];
  return credit.map((c) => ({ name: c.artist?.name ?? c.name ?? "Unknown Artist" }));
}

function topGenre(genres: { name?: string; count?: number }[] | undefined): string | undefined {
  if (!genres || genres.length === 0) return undefined;
  const sorted = [...genres].sort((a, b) => (b.count ?? 0) - (a.count ?? 0));
  return sorted[0]?.name ?? undefined;
}

interface MbReleaseGroupJson {
  id: string;
  title?: string;
  "first-release-date"?: string;
  "artist-credit"?: MbArtistCredit[];
  genres?: { name?: string; count?: number }[];
  releases?: { id: string; date?: string; status?: string }[];
}

interface MbReleaseJson {
  media?: {
    tracks?: {
      id: string;
      title?: string;
      length?: number;
      recording?: { id: string; title?: string; length?: number };
      "artist-credit"?: MbArtistCredit[];
    }[];
  }[];
}

/** Pick the release whose tracks define the group: earliest Official. */
export function pickReleaseId(group: MbReleaseGroupJson): string | null {
  const releases = (group.releases ?? []).filter((r) => r.id);
  if (releases.length === 0) return null;
  const official = releases.filter((r) => (r.status ?? "Official") === "Official");
  const pool = official.length > 0 ? official : releases;
  const sorted = [...pool].sort((a, b) => String(a.date ?? "9999").localeCompare(String(b.date ?? "9999")));
  return sorted[0]?.id ?? null;
}

export function releaseGroupBundle(
  group: MbReleaseGroupJson,
  release: MbReleaseJson,
): ReleaseGroupBundle {
  const tracks: PlaylistTrack[] = [];
  for (const medium of release.media ?? []) {
    for (const t of medium.tracks ?? []) {
      const recording = t.recording;
      const id = recording?.id ?? t.id;
      if (!id) continue;
      tracks.push({
        id,
        name: recording?.title ?? t.title ?? "Unknown Track",
        artists: creditArtists(t["artist-credit"] ?? group["artist-credit"]),
        album: { name: group.title ?? "Unknown Album", images: [{ url: coverArtUrl(group.id) }] },
        duration_ms: recording?.length ?? t.length ?? undefined,
        image_url: coverArtUrl(group.id),
      });
    }
  }
  const yearPart = (group["first-release-date"] ?? "").slice(0, 4);
  const year = /^\d{4}$/.test(yearPart) ? Number(yearPart) : undefined;
  return {
    mbid: group.id,
    kind: "release-group",
    title: group.title ?? "Unknown Album",
    artistName: creditName(group["artist-credit"]),
    artistMbid: group["artist-credit"]?.[0]?.artist?.id,
    genre: topGenre(group.genres),
    year,
    tracks,
  };
}

interface MbArtistJson {
  id: string;
  name?: string;
  genres?: { name?: string; count?: number }[];
  "release-groups"?: { id: string; "primary-type"?: string }[];
}

export function artistBundle(artist: MbArtistJson): ArtistBundle {
  const groups = (artist["release-groups"] ?? [])
    .filter((g) => g.id && (g["primary-type"] ?? "Album") === "Album")
    .map((g) => g.id);
  return {
    mbid: artist.id,
    kind: "artist",
    name: artist.name ?? "Unknown Artist",
    genre: topGenre(artist.genres),
    releaseGroupMbids: [...new Set(groups)],
  };
}

/**
 * Sequential, rate-spaced MusicBrainz resolver. `fetchFn` and `sleep` are
 * injectable for tests; production uses global fetch and real timers.
 */
export class MbResolver {
  private lastCallAt = 0;
  private readonly fetchFn: FetchFn;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(
    fetchFn?: FetchFn,
    sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  ) {
    this.fetchFn =
      fetchFn ??
      (async (url, init) => {
        const res = await fetch(url, init);
        return { ok: res.ok, status: res.status, json: () => res.json() };
      });
    this.sleep = sleep;
  }

  private async getJson(path: string): Promise<unknown> {
    const wait = this.lastCallAt + MB_MIN_INTERVAL_MS - Date.now();
    if (wait > 0) await this.sleep(wait);
    this.lastCallAt = Date.now();
    const res = await this.fetchFn(`${MB_BASE}${path}`, {
      headers: { "User-Agent": MB_USER_AGENT, Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`musicbrainz ${res.status} for ${path}`);
    return res.json();
  }

  async releaseGroup(mbid: string): Promise<ReleaseGroupBundle | null> {
    const group = (await this.getJson(
      `/release-group/${mbid}?inc=artists+genres+releases&fmt=json`,
    )) as MbReleaseGroupJson;
    const releaseId = pickReleaseId(group);
    if (!releaseId) return null;
    const release = (await this.getJson(
      `/release/${releaseId}?inc=recordings+artists&fmt=json`,
    )) as MbReleaseJson;
    const bundle = releaseGroupBundle(group, release);
    return bundle.tracks.length > 0 ? bundle : null;
  }

  async artist(mbid: string): Promise<ArtistBundle | null> {
    const artist = (await this.getJson(
      `/artist/${mbid}?inc=genres+release-groups&fmt=json`,
    )) as MbArtistJson;
    return artistBundle(artist);
  }
}

/* ------------------------------------------------------------------ */
/* Track picking: the album's most popular tracks, from public data.   */
/*                                                                     */
/* A charted album becomes at most CHART_TRACKS_PER_ALBUM tracks, not   */
/* its whole tracklist. "Most popular" comes from ListenBrainz's public */
/* per-recording listen statistics — catalogue-wide data about the      */
/* recordings, nothing from SIN's users. SIN itself never collects      */
/* track-level signals (DESIGN-LISTEN-SIGNALS.md §1, DATA.md §14).       */
/* ------------------------------------------------------------------ */

/** Tracks each charted album contributes to a playlist: its two most
 *  popular, so 25 albums make a 50-track playlist. */
export const CHART_TRACKS_PER_ALBUM = 2;
/** Popular picks kept per album in its cached bundle (topTrackIds) —
 *  more than a playlist uses, so the per-album count can change later
 *  without asking ListenBrainz again. */
export const POPULAR_TRACKS_CACHED = 5;
const LB_BASE = "https://api.listenbrainz.org/1";
/** ListenBrainz caps batch popularity requests; stay well under. */
const LB_BATCH = 100;

export interface RecordingPopularity {
  listens: number;
  users: number;
}

/**
 * Pick up to `limit` recording MBIDs, most popular first: ranked by
 * distinct listeners (breadth: one person on repeat cannot carry a
 * track), then total listens, then album order. Tracks with no data are
 * never picked; [] means nothing on the album had popularity data.
 */
export function pickTopTrackIds(
  tracks: PlaylistTrack[],
  popularity: Map<string, RecordingPopularity>,
  limit: number = POPULAR_TRACKS_CACHED,
): string[] {
  const seen = new Set<string>();
  return tracks
    .map((t, order) => ({ id: t.id, order, pop: popularity.get(t.id.toLowerCase()) }))
    .filter((t) => {
      if (!t.pop || (t.pop.users <= 0 && t.pop.listens <= 0) || seen.has(t.id)) return false;
      seen.add(t.id);
      return true;
    })
    .sort((a, b) => b.pop!.users - a.pop!.users || b.pop!.listens - a.pop!.listens || a.order - b.order)
    .slice(0, limit)
    .map((t) => t.id);
}

/**
 * The tracks a charted album contributes to a playlist: its picked top
 * tracks in popularity order, or — when there is no popularity data —
 * its first `limit` tracks in album order.
 */
export function chartTracksFor(
  bundle: ReleaseGroupBundle,
  limit: number = CHART_TRACKS_PER_ALBUM,
): PlaylistTrack[] {
  const byId = new Map(bundle.tracks.map((t) => [t.id, t]));
  const picked = (bundle.topTrackIds ?? [])
    .map((id) => byId.get(id))
    .filter((t): t is PlaylistTrack => t !== undefined);
  return (picked.length > 0 ? picked : bundle.tracks).slice(0, limit);
}

/** ListenBrainz batch recording popularity (public, no key). Injectable
 *  fetch for tests. Throws on a failed request so the caller retries the
 *  album on the next compose pass instead of caching "no data". */
export class ListenBrainzPopularity {
  private readonly fetchFn: (
    url: string,
    init: { method: string; headers: Record<string, string>; body: string },
  ) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

  constructor(fetchFn?: ListenBrainzPopularity["fetchFn"]) {
    this.fetchFn =
      fetchFn ??
      (async (url, init) => {
        const res = await fetch(url, init);
        return { ok: res.ok, status: res.status, json: () => res.json() };
      });
  }

  async recordings(mbids: string[]): Promise<Map<string, RecordingPopularity>> {
    const out = new Map<string, RecordingPopularity>();
    const unique = [...new Set(mbids.map((m) => m.toLowerCase()))];
    for (let i = 0; i < unique.length; i += LB_BATCH) {
      const res = await this.fetchFn(`${LB_BASE}/popularity/recording`, {
        method: "POST",
        headers: { "User-Agent": MB_USER_AGENT, "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ recording_mbids: unique.slice(i, i + LB_BATCH) }),
      });
      if (!res.ok) throw new Error(`listenbrainz ${res.status} for popularity/recording`);
      const rows = (await res.json()) as {
        recording_mbid?: string;
        total_listen_count?: number | null;
        total_user_count?: number | null;
      }[];
      for (const row of Array.isArray(rows) ? rows : []) {
        if (!row?.recording_mbid) continue;
        out.set(row.recording_mbid.toLowerCase(), {
          listens: row.total_listen_count ?? 0,
          users: row.total_user_count ?? 0,
        });
      }
    }
    return out;
  }
}

/**
 * Public chart data for the cold start (logic.ts fillFromPublic).
 *
 * ListenBrainz publishes site-wide listening statistics for its whole
 * user base. Two public reads, no key, from the composer only:
 *
 *   GET /1/stats/sitewide/release-groups?range=week&count=1000
 *       the albums ListenBrainz users played most last week (ranked by
 *       listens; the list can name one group twice — deduped here);
 *   GET /1/explore/fresh-releases/?days=30&past=true&future=false
 *       release groups released in the last 30 days (no play counts —
 *       it only marks what is new).
 *
 * Trending's fill is the weekly top; Just Landed's is the weekly top
 * restricted to fresh Albums and EPs. A third read,
 *   GET /1/stats/sitewide/recordings?range=week&count=1000,
 * is the weekly SONG chart behind "Top Tracks · from ListenBrainz". Nothing in either request comes
 * from a SIN user: they are the same public pages anyone can read.
 */
import { MB_USER_AGENT } from "./mb.ts";
import { isUuidShape, type PublicRecordingRow } from "./logic.ts";

const LB_BASE = "https://api.listenbrainz.org/1";

/** Candidates per chart: more than the 25 slots, so albums MusicBrainz
 *  cannot turn into a tracklist can be skipped without leaving a gap. */
export const PUBLIC_CANDIDATES_PER_CHART = 40;

export interface PublicSeeds {
  trending: string[];
  "just-landed": string[];
  /** ListenBrainz's weekly song chart, ranked (Top Tracks). */
  recordings: PublicRecordingRow[];
}

type FetchFn = (url: string, init: { headers: Record<string, string> }) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

export class ListenBrainzCharts {
  private readonly fetchFn: FetchFn;

  constructor(fetchFn?: FetchFn) {
    this.fetchFn =
      fetchFn ??
      (async (url, init) => {
        const res = await fetch(url, init);
        return { ok: res.ok, status: res.status, json: () => res.json() };
      });
  }

  private async getJson(path: string): Promise<unknown> {
    const res = await this.fetchFn(`${LB_BASE}${path}`, {
      headers: { "User-Agent": MB_USER_AGENT, Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`listenbrainz ${res.status} for ${path.split("?")[0]}`);
    return res.json();
  }

  /** Last week's most-played release groups, ranked, deduped. */
  async weeklyTopReleaseGroups(): Promise<string[]> {
    const body = (await this.getJson("/stats/sitewide/release-groups?range=week&count=1000")) as {
      payload?: { release_groups?: { release_group_mbid?: string | null }[] };
    };
    const seen = new Set<string>();
    const out: string[] = [];
    for (const row of body.payload?.release_groups ?? []) {
      const mbid = row?.release_group_mbid?.toLowerCase();
      if (!mbid || !isUuidShape(mbid) || seen.has(mbid)) continue;
      seen.add(mbid);
      out.push(mbid);
    }
    return out;
  }

  /** Last week's most-played recordings, ranked. */
  async weeklyTopRecordings(): Promise<PublicRecordingRow[]> {
    const body = (await this.getJson("/stats/sitewide/recordings?range=week&count=1000")) as {
      payload?: { recordings?: PublicRecordingRow[] };
    };
    return body.payload?.recordings ?? [];
  }

  /** Album and EP release groups released in the last 30 days. */
  async freshAlbumGroups(): Promise<Set<string>> {
    const body = (await this.getJson(
      "/explore/fresh-releases/?days=30&sort=release_date&past=true&future=false",
    )) as {
      payload?: { releases?: { release_group_mbid?: string | null; release_group_primary_type?: string | null }[] };
    };
    const out = new Set<string>();
    for (const row of body.payload?.releases ?? []) {
      const type = row?.release_group_primary_type;
      const mbid = row?.release_group_mbid?.toLowerCase();
      if (mbid && isUuidShape(mbid) && (type === "Album" || type === "EP")) out.add(mbid);
    }
    return out;
  }

  /** Ranked public candidates per fill chart. Any failure yields empty
   *  lists: the composer then publishes network data only, as before. */
  async seeds(): Promise<PublicSeeds> {
    try {
      const top = await this.weeklyTopReleaseGroups();
      let fresh = new Set<string>();
      try {
        fresh = await this.freshAlbumGroups();
      } catch {
        // Trending can still fill without the release-date list.
      }
      let recordings: PublicRecordingRow[] = [];
      try {
        recordings = await this.weeklyTopRecordings();
      } catch {
        // The album charts still fill without the song chart.
      }
      return {
        trending: top.slice(0, PUBLIC_CANDIDATES_PER_CHART),
        "just-landed": top.filter((m) => fresh.has(m)).slice(0, PUBLIC_CANDIDATES_PER_CHART),
        recordings,
      };
    } catch {
      return { trending: [], "just-landed": [], recordings: [] };
    }
  }
}

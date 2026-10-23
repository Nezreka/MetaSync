/**
 * SIN §9 tests — key split (node vs admin) + canonical playlists.
 * Same style as sin.test.ts: pure validation, route behaviour WITHOUT a
 * live database (an unconfigured DB answers 503, which is how "auth
 * passed" shows up at route level), and the storage helpers driven
 * through a small in-memory fake implementing the exact drizzle chains
 * publishPlaylists / loadLatestPlaylists use.
 * Run with: npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  MAX_PLAYLISTS_PER_PUBLISH,
  MAX_TRACKS_PER_PLAYLIST,
  validatePlaylistsPublish,
  type PlaylistTrack,
} from "../src/sin/logic.ts";
import type { SinDb } from "../src/sin/db.ts";

const WEEK = "2026-W43";
const NODE_KEY = "test-node-secret-0123456789";
const ADMIN_KEY = "test-admin-secret-012345678";

function track(id: string): PlaylistTrack {
  return {
    id,
    name: `Track ${id}`,
    artists: [{ name: "Some Artist" }],
    album: {
      name: "Some Album",
      images: [{ url: "https://example.com/cover.jpg" }],
    },
    duration_ms: 200_000,
  };
}

function publishBody(overrides: Record<string, unknown> = {}) {
  return {
    week: WEEK,
    playlists: [
      { key: "trending", name: "Trending in SoulSync", tracks: [track("t1")] },
      { key: "most-wanted", name: "Most Wanted", tracks: [track("t2"), track("t3")] },
    ],
    ...overrides,
  };
}

describe("playlist publish validation", () => {
  test("accepts a valid publish, keeping the Spotify-shaped track subset", () => {
    const result = validatePlaylistsPublish(publishBody());
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.value.week, WEEK);
      assert.equal(result.value.playlists.length, 2);
      assert.equal(result.value.playlists[0]?.tracks[0]?.album?.name, "Some Album");
      // A bare track (no optional fields) is valid too.
      const bare = validatePlaylistsPublish({
        week: WEEK,
        playlists: [
          { key: "breakouts", name: "Breakouts", tracks: [{ id: "x", name: "X", artists: [{ name: "A" }] }] },
        ],
      });
      assert.equal(bare.ok, true);
    }
  });

  test("rejects a bad playlist key value", () => {
    const result = validatePlaylistsPublish({
      week: WEEK,
      playlists: [{ key: "charts", name: "Charts", tracks: [] }],
    });
    assert.equal(result.ok, false);
  });

  test("rejects more than 3 playlists and duplicate keys", () => {
    const four = validatePlaylistsPublish({
      week: WEEK,
      playlists: [
        { key: "trending", name: "T", tracks: [] },
        { key: "most-wanted", name: "M", tracks: [] },
        { key: "breakouts", name: "B", tracks: [] },
        { key: "trending", name: "T2", tracks: [] },
      ],
    });
    assert.equal(four.ok, false);
    assert.ok(MAX_PLAYLISTS_PER_PUBLISH === 3);

    const dupe = validatePlaylistsPublish({
      week: WEEK,
      playlists: [
        { key: "trending", name: "T", tracks: [] },
        { key: "trending", name: "T again", tracks: [] },
      ],
    });
    assert.equal(dupe.ok, false);
  });

  test("rejects too many tracks in one playlist", () => {
    const many = Array.from({ length: MAX_TRACKS_PER_PLAYLIST + 1 }, (_, i) =>
      track(`t${i}`),
    );
    const result = validatePlaylistsPublish({
      week: WEEK,
      playlists: [{ key: "trending", name: "T", tracks: many }],
    });
    assert.equal(result.ok, false);
  });

  test("rejects malformed tracks", () => {
    const badTracks: unknown[] = [
      { name: "No Id", artists: [{ name: "A" }] },
      { id: "x", name: "No Artists" },
      { id: "x", name: "Empty Artists", artists: [] },
      { id: "x", name: "Artist Without Name", artists: [{}] },
      { id: "x", name: "Smuggled Field", artists: [{ name: "A" }], path: "/music/x.mp3" },
      { id: "x", name: "Bad Duration", artists: [{ name: "A" }], duration_ms: -5 },
      { id: "x", name: "Bad Album Image", artists: [{ name: "A" }], album: { name: "Al", images: [{}] } },
    ];
    for (const bad of badTracks) {
      const result = validatePlaylistsPublish({
        week: WEEK,
        playlists: [{ key: "trending", name: "T", tracks: [bad] }],
      });
      assert.equal(result.ok, false, `should reject: ${JSON.stringify(bad)}`);
    }
  });

  test("rejects unknown fields, a bad week, and an over-long name", () => {
    assert.equal(
      validatePlaylistsPublish({ ...publishBody(), publisher: "someone" }).ok,
      false,
    );
    assert.equal(
      validatePlaylistsPublish(publishBody({ week: "2026-43" })).ok,
      false,
    );
    assert.equal(
      validatePlaylistsPublish({
        week: WEEK,
        playlists: [{ key: "trending", name: "n".repeat(201), tracks: [] }],
      }).ok,
      false,
    );
  });
});

/* In-memory fake for the storage helpers: implements insert().values()
 * .onConflictDoUpdate() (upsert on (week, key)) and select().from(). */
interface StoredRow {
  week: string;
  key: string;
  name: string;
  tracks: PlaylistTrack[];
  publishedAt: Date;
}

class FakePlaylistDb {
  rows = new Map<string, StoredRow>();

  insert() {
    return {
      values: (v: StoredRow) => ({
        onConflictDoUpdate: async ({ set }: { set: Partial<StoredRow> }) => {
          const k = `${v.week}|${v.key}`;
          const existing = this.rows.get(k);
          if (existing) this.rows.set(k, { ...existing, ...set });
          else this.rows.set(k, { ...v });
          return [];
        },
      }),
    };
  }

  select() {
    return {
      from: async () => [...this.rows.values()].map((r) => ({ ...r })),
    };
  }
}

describe("canonical playlist storage (fake db)", () => {
  test("empty pull before the first publish", async () => {
    const { loadLatestPlaylists } = await import("../src/sin/index.ts");
    const fake = new FakePlaylistDb();
    const latest = await loadLatestPlaylists(fake as unknown as SinDb);
    assert.equal(latest.week, null);
    assert.deepEqual(latest.playlists, []);
  });

  test("publish → pull round-trip, served in canonical key order", async () => {
    const { publishPlaylists, loadLatestPlaylists, loadPlaylistsForWeek } =
      await import("../src/sin/index.ts");
    const fake = new FakePlaylistDb();
    const db = fake as unknown as SinDb;

    // Publish most-wanted first: serving order must not depend on it.
    const stored = await publishPlaylists(db, {
      week: WEEK,
      playlists: [
        { key: "most-wanted", name: "Most Wanted", tracks: [track("t2")] },
        { key: "trending", name: "Trending in SoulSync", tracks: [track("t1")] },
      ],
    });
    assert.equal(stored.week, WEEK);
    assert.deepEqual(
      stored.playlists.map((p) => p.key),
      ["trending", "most-wanted"],
    );
    assert.equal(stored.playlists[0]?.tracks[0]?.id, "t1");

    const latest = await loadLatestPlaylists(db);
    assert.deepEqual(latest, stored);
    const forWeek = await loadPlaylistsForWeek(db, WEEK);
    assert.deepEqual(forWeek, stored);
  });

  test("republishing the same (week, key) upserts instead of duplicating", async () => {
    const { publishPlaylists, loadPlaylistsForWeek } = await import(
      "../src/sin/index.ts"
    );
    const fake = new FakePlaylistDb();
    const db = fake as unknown as SinDb;

    await publishPlaylists(db, {
      week: WEEK,
      playlists: [{ key: "trending", name: "Old Name", tracks: [track("old")] }],
    });
    const stored = await publishPlaylists(db, {
      week: WEEK,
      playlists: [{ key: "trending", name: "New Name", tracks: [track("new")] }],
    });
    assert.equal(stored.playlists.length, 1);
    assert.equal(stored.playlists[0]?.name, "New Name");
    assert.equal(stored.playlists[0]?.tracks[0]?.id, "new");
    assert.equal(fake.rows.size, 1);
    const forWeek = await loadPlaylistsForWeek(db, WEEK);
    assert.equal(forWeek.playlists.length, 1);
  });

  test("the latest week wins; older weeks stay stored per week", async () => {
    const { publishPlaylists, loadLatestPlaylists, loadPlaylistsForWeek } =
      await import("../src/sin/index.ts");
    const fake = new FakePlaylistDb();
    const db = fake as unknown as SinDb;

    await publishPlaylists(db, {
      week: WEEK,
      playlists: [{ key: "trending", name: "T43", tracks: [track("a")] }],
    });
    await publishPlaylists(db, {
      week: "2026-W44",
      playlists: [{ key: "breakouts", name: "B44", tracks: [track("b")] }],
    });

    const latest = await loadLatestPlaylists(db);
    assert.equal(latest.week, "2026-W44");
    assert.deepEqual(latest.playlists.map((p) => p.key), ["breakouts"]);
    const w43 = await loadPlaylistsForWeek(db, WEEK);
    assert.equal(w43.playlists[0]?.name, "T43");
  });
});

describe("routes: key split (no live database)", () => {
  test("node key is rejected on aggregate and playlist publish; admin key passes auth", async () => {
    process.env.SIN_INGEST_KEY = NODE_KEY;
    process.env.SIN_ADMIN_KEY = ADMIN_KEY;
    delete process.env.DATABASE_URL;
    const { default: sinApp } = await import("../src/sin/index.ts");

    // Aggregate: node key 401, no key 401, admin key passes auth → 503 (no DB).
    const aggNode = await sinApp.request("/v1/aggregate", {
      method: "POST",
      headers: { "x-sin-key": NODE_KEY },
      body: JSON.stringify({}),
    });
    assert.equal(aggNode.status, 401);
    const aggAdmin = await sinApp.request("/v1/aggregate", {
      method: "POST",
      headers: { "x-sin-key": ADMIN_KEY },
      body: JSON.stringify({}),
    });
    assert.equal(aggAdmin.status, 503);

    // Report: BOTH keys pass auth → validation runs → 503 (no DB).
    for (const key of [NODE_KEY, ADMIN_KEY]) {
      const report = await sinApp.request("/v1/report", {
        method: "POST",
        headers: { "x-sin-key": key },
        body: JSON.stringify({
          week: WEEK,
          token: "a".repeat(64),
          signals: [{ type: "watchlist_add", mbid: "11111111-1111-1111-1111-111111111111" }],
        }),
      });
      assert.equal(report.status, 503);
    }
    const reportWrong = await sinApp.request("/v1/report", {
      method: "POST",
      headers: { "x-sin-key": "wrong-key-wrong-key-wrong" },
      body: JSON.stringify({}),
    });
    assert.equal(reportWrong.status, 401);

    // Publish: node key 401, no key 401, admin + bad body 400,
    // admin + valid body passes auth + validation → 503 (no DB).
    const pubNode = await sinApp.request("/v1/playlists", {
      method: "POST",
      headers: { "x-sin-key": NODE_KEY },
      body: JSON.stringify(publishBody()),
    });
    assert.equal(pubNode.status, 401);
    const pubNoKey = await sinApp.request("/v1/playlists", {
      method: "POST",
      body: JSON.stringify(publishBody()),
    });
    assert.equal(pubNoKey.status, 401);
    const pubBad = await sinApp.request("/v1/playlists", {
      method: "POST",
      headers: { "x-sin-key": ADMIN_KEY },
      body: JSON.stringify({
        week: WEEK,
        playlists: [{ key: "charts", name: "Nope", tracks: [] }],
      }),
    });
    assert.equal(pubBad.status, 400);
    const pubAdmin = await sinApp.request("/v1/playlists", {
      method: "POST",
      headers: { "x-sin-key": ADMIN_KEY },
      body: JSON.stringify(publishBody()),
    });
    assert.equal(pubAdmin.status, 503);

    // Playlist pull is gated since §3A.7: no credential -> 401; with a
    // valid key auth passes and the missing DB answers 503, never a
    // fake empty success at route level.
    const pullAnon = await sinApp.request("/v1/playlists");
    assert.equal(pullAnon.status, 401);
    const pull = await sinApp.request("/v1/playlists", {
      headers: { "x-sin-key": NODE_KEY },
    });
    assert.equal(pull.status, 503);
  });

  test("admin routes fail closed (503) when SIN_ADMIN_KEY is not configured", async () => {
    process.env.SIN_INGEST_KEY = NODE_KEY;
    const savedAdmin = process.env.SIN_ADMIN_KEY;
    delete process.env.SIN_ADMIN_KEY;
    try {
      const { default: sinApp } = await import("../src/sin/index.ts");
      // Even the node key gets 503 (not 401): the admin gate is unconfigured.
      const agg = await sinApp.request("/v1/aggregate", {
        method: "POST",
        headers: { "x-sin-key": NODE_KEY },
        body: JSON.stringify({}),
      });
      assert.equal(agg.status, 503);
      const pub = await sinApp.request("/v1/playlists", {
        method: "POST",
        headers: { "x-sin-key": NODE_KEY },
        body: JSON.stringify(publishBody()),
      });
      assert.equal(pub.status, 503);
      // The node/report path still works off the node key alone.
      const report = await sinApp.request("/v1/report", {
        method: "POST",
        headers: { "x-sin-key": NODE_KEY },
        body: JSON.stringify({
          week: WEEK,
          token: "a".repeat(64),
          signals: [{ type: "watchlist_add", mbid: "11111111-1111-1111-1111-111111111111" }],
        }),
      });
      assert.equal(report.status, 503); // auth passed; DB unconfigured
    } finally {
      if (savedAdmin === undefined) delete process.env.SIN_ADMIN_KEY;
      else process.env.SIN_ADMIN_KEY = savedAdmin;
    }
  });
});

/**
 * MetaSync metadata service — Neon Function, slug `metasync`.
 *
 * SCAFFOLD ONLY. No real implementation lives here yet, by design
 * (Boulder, Oct 22, 2026): SIN is built first because it is the smaller
 * project; a lot of work will go into MetaSync later.
 *
 * What this will become (sketch §3, SoulID-keyed since Oct 22):
 * SoulSync installs acting as a distributed metadata source for each other,
 * with records keyed by SoulID (SoulSync's own canonical ID per track /
 * album / artist), signed by the publishing node, accepted only after
 * corroboration, and never overwriting local data.
 *
 * Non-negotiables for the future implementation:
 * - Local data always wins.
 * - No audio. Ever. Metadata only.
 */
import { Hono } from "hono";

const app = new Hono();

app.get("/health", (c) =>
  c.json({ ok: true, service: "metasync", status: "scaffold" }),
);

// TODO(metasync): implement record lookup by SoulID once the metadata
// service is commissioned — schema sketch in ./schema.ts, design in
// SIDECAR-ARCHITECTURE-SKETCH.md §3. Until then this is deliberately a 501.
app.get("/v1/records/:soulId", (c) =>
  c.json(
    {
      error: "not_implemented",
      soulId: c.req.param("soulId"),
      todo: "MetaSync metadata service is scaffold-only; SIN ships first.",
    },
    501,
  ),
);

app.notFound((c) => c.json({ error: "not_found" }, 404));

export default app;

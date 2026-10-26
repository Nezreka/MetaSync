# DESIGN-METADATA.md — the MetaSync metadata network

Status: design, v1 not yet built. Revised after two hostile Claude
review rounds (Oct 26, 2026); both rounds' must-fixes are folded
throughout and marked nowhere — the doc is meant to read as one
design, not a changelog. Round 2's verdict: architecture unchanged
and right; the remaining work is the step-0 spike (§13), not more
design.

This document supersedes the assumptions in `src/metasync/schema.ts` (one
signed record per SoulID, a stored `publisher_node`, corroboration at
N>=3, a corrections bag). The scaffold code stays as-is until the build
PRs replace it; where the scaffold and this doc disagree, this doc wins,
and §13 step 10 removes the scaffold pieces that contradict it.

SoulID is only for this service. SIN does not use SoulID; SIN signals are
keyed by MusicBrainz IDs (see `src/sin/`).

## 1. The experience

A user searches MetaSync for an artist, album, or track and gets results
that look familiar — the same shape as a Spotify or Deezer search — but
packed with far more data than any single service returns: the complete
tracklist, every provider ID anyone has ever matched (MusicBrainz,
Spotify, Deezer, Tidal, iTunes, Discogs), ISRCs, durations, year, genres.
One user didn't have everything; another had the missing pieces. The
merged result is complete in a way no single library or service is.

Searching and enriching are the same flow at different depths:

- **Search** finds the item (fuzzy text, partial names, typos included)
  and shows the merged record.
- **Enrich** takes something already in the user's SoulSync library and
  fills its empty fields from the merged record. It never overwrites
  anything (§8, §14.1).

## 2. Principles

1. **Local data always wins.** The network fills gaps and offers; it
   never overwrites a user's own metadata. Fields the network itself
   filled are tagged as network-sourced forever (§8) — they are not
   "local data" and a later snapshot can correct or retract them.
2. **No audio. Ever.** Metadata only — no files, no file paths, no
   listening data.
3. **Verify, don't vote.** Anything the network claims that can be
   checked against a public source gets checked (§7). Counting is the
   fallback for what cannot be checked, never the first resort.
4. **The network is a hint cache, not an authority.** A bad hint costs
   one verification call and gets dropped. SoulSync works fully with
   the network absent.
5. **SoulIDs are computed, never registered.** A SoulID is a
   deterministic hash of public names (SoulSync SoulID v2, `kinded_v2`).
   There is no registry, no minting service, no central database of
   SoulIDs. Any install derives the same ID offline.
6. **Contributing is its own opt-in**, separate from joining SIN and
   separate from pulling data, and it additionally requires
   operator-approved contributor status (§5). Pulling is on by default
   for new pool joins; nodes that joined SIN before this service exists
   are asked once before their node starts pulling snapshots (the
   download is tens to hundreds of MB — §10 — and silently changing an
   existing install's behaviour is not acceptable).
7. **Decentralization is staged, not faked.** V1 runs on infrastructure
   that exists. Every v1 format choice keeps the peer-to-peer stages
   possible without a rewrite (§11).

## 3. Why v1 is not live peer-to-peer

The original vision was a live BitTorrent-style query: ask the network,
peers answer, the requester merges. That stays the long-term direction.
At the network's actual starting scale it fails on arithmetic, not
taste:

- **Availability.** The founding cohort is 15–50 nodes. The items the
  network exists for are the sparse ones, typically held by 1–2 nodes,
  and home servers are up maybe 70% of the time. A live query for a
  1-holder item succeeds ~70% of the time, and 0% when nobody holding
  it is online. The network would be least reliable exactly where it
  should help most, and users read that as broken.
- **A DHT buys nothing at this size.** Kademlia routing exists so a
  query doesn't have to contact everyone. With ~30 nodes you just
  contact everyone; the DHT's costs arrive with none of its benefits.
- **NAT.** Nodes are home servers in Docker behind consumer routers.
  Inbound peer connections need hole punching, UPnP, or a relay — and
  a relay is a central server wearing a costume. Peer discovery needs
  bootstrap nodes, which is a central server again.
- **Privacy.** Answering live queries reveals holdings, and a SoulID is
  a hash of public names: anyone can hash every artist in MusicBrainz
  (~2M names, minutes of compute) and query a peer for each one,
  reconstructing that peer's library. Live P2P also exposes every
  peer's home IP to every other peer.
- **DHTs don't do text search.** A DHT answers "record for key X",
  never "radioh" or "ok compter". The familiar search box in §1 cannot
  be powered by a peer network at all; something else must resolve
  text to a SoulID first.

V1 therefore splits the vision in two: the **corpus** (accumulated,
verified, merged knowledge) is built centrally and shipped to every node
as a file; the **experience** (instant fuzzy search, packed results,
local merge, gap-filling) is exactly the one described in §1, served
locally.

## 4. Why not Hydrabase's shape

SoulSync already ships a Hydrabase client (`core/hydrabase_client.py`),
so the comparison is concrete. Hydrabase is a live search client: the
query goes over a shared WebSocket, the call blocks up to 8 seconds,
and results come back shaped like Spotify/iTunes results and are
consumed the same way.

| | Hydrabase | This design |
|---|---|---|
| Knowledge lives | In whoever is connected right now | In a persistent corpus every node holds |
| Search latency | Live round trip, blocking, per search | Local lookup, milliseconds, offline |
| Trust in results | Taken as-is, like a provider's | Verified against providers before publishing; per-field provenance after |
| Identity | Names re-resolved per query | SoulID v2 — one deterministic ID on every install |
| Failure mode | Timeout = nothing | Stale snapshot = slightly older data |
| Search | Text search is the network's job | Fuzzy text search is local (FTS over the corpus) |

What Hydrabase gets right and this design copies: the search feel.
Type, get familiar results. Hydrabase IDs stay compatible — the alias
table (§10) can carry them as one more provider ID. Note that a
Hydrabase ID claim is unverifiable (there is no public Hydrabase
catalogue to check it against), so Hydrabase aliases are floor-gated
like any other unverifiable claim (§9).

## 5. Architecture (v1)

```
SoulSync ──export API──► MetaSync node ──claim batches (opt-in)──► claims store
                                                                      │
                                        Python worker: verify (§7), merge (§8),
                                        publication gates (§9)
                                                                      │
                                              signed manifest + snapshot + deltas
                                                                      ▼
                              every node: local store (SQLite + FTS + alias table)
                                                                      │
                              search UI · enrich (fill-empty only) ──► SoulSync
```

Decided (not open): hosting and runtimes.

- **Claims store:** the existing Neon Postgres (new tables, separate
  from SIN's — §12). Claims ingestion rides the existing SIN rails:
  portal account, Ed25519-signed requests, exchange tokens.
- **Contributor approval is a new, real gate.** SIN's portal approval
  is the *user* approving their *own* node binding; anyone with a
  Google/GitHub account can sign up, and PORTAL-DESIGN says so. That is
  not a Sybil wall. Contributing therefore requires a per-account
  `contributor_approved` flag set by the operator in the portal admin,
  separate from node binding. Approval is manual and based on the
  operator knowing the person or their history in the SoulSync
  community — it is never automatic on signup, because if it were, the
  §9 floor of 3 accounts would be 3 throwaway signups again.
  Unapproved accounts' claim batches are refused. (Pulling snapshots
  needs no account at all — §10.)
- **Compiler/verification worker:** a long-running **Python** worker
  (not a Neon request handler, not the GitHub-Actions curl loop the SIN
  compose job uses — that loop's 600s timeouts and per-pass limits
  cannot run a multi-day verification backlog). Python because the
  node and SoulSync are Python: the merge library (§8) and the SoulID
  normalizer exist exactly once, in Python, shared by worker and node.
  **Host:** a single small always-on VM/container under the operator's
  control, outbound connections only, no inbound ports beyond admin
  SSH. This box is the most sensitive in the design — it holds the
  online snapshot key (§10), reaches the blind secret (§12), and
  holds the provider credentials (§7) — so its secrets live only in
  its environment, never in the repo or the Neon DB. The specific
  provider is the operator's choice; the properties above are not
  optional. Compiled output is pushed to the public snapshot host
  (§10); the worker itself serves nothing.
- **Node contribution.** The MetaSync node reads its SoulSync's
  metasync export and turns what it finds into claim batches (§6).
  The node is the only thing that talks to the network; SoulSync
  never does. The node has no export client yet — building one is
  §13 step 4.
- **Node consumption.** Nodes poll the manifest (§10), verify
  signatures, apply deltas or fall back to the full snapshot, and load
  the corpus into a local SQLite store. Search and enrichment read
  only the local store (plus the fallback sources in §11).
- **Back into SoulSync.** Enrichment output is fill-empty-fields only,
  and only into fields tagged network-sourced (§8). The SoulSync-side
  write path is its own PR, after v1 (§14.1); until then enrichment is
  display-only.

### 5.1 SoulSync export — verified state and gaps

Verified against SoulSync dev at PR #1883 (SoulID v2, commit
`9b21ba21`): the export serves `soul_id` (+ `soul_id_legacy`), all
provider ID columns, ISRC, genres, year, track `duration`, per-entity
`canonical_source`, and per-entity `soulid_version` stamped from the
DB cutover flags. Gaps the build must respect:

- **G1 — no per-field provenance.** Provider ID fields are
  self-describing (a Spotify ID came from Spotify). Scalar fields
  (genres, year) carry only the entity's `canonical_source`. Claim
  `source` for scalars is therefore the entity's `canonical_source`,
  and scalars whose canonical source is a local/user source are
  treated as unverifiable.
- **G2 — no user-edit marker.** The export cannot distinguish a
  hand-edited title from provider data, so the `user_edit` value in
  the §6 `source` enum is **reserved** — nothing can emit it in v1.
  Recomputing the SoulID from a name claim does *not* catch edits:
  SoulSync computed the ID from its own (possibly edited) names, so
  the recompute always agrees. The recompute catches implementation
  bugs and cross-install inconsistency. What catches edits is §7's
  comparison of claimed names against provider data. Accept the
  residue in v1; it is why scalar claims from a single account never
  publish unverified (§9).
- **G3 — the node has no export client.** `node/metasync_node/
  soulsync.py` speaks the SIN-era API surface only. Step 4 builds the
  export walk (keyset cursor + `since`, per the SoulSync API).

## 6. The claim — the unit of data

There are no "records" passed around. The atomic unit is a **claim**:
one field of one entity, asserted by one contributor.

```json
{
  "subject": "soul_…",            // SoulID v2 of the entity
  "kind": "track",                // artist | album | track
  "field": "isrc",                // provider id, mbid, duration, year, …
  "value": "USRC10000001",
  "source": "deezer",             // see G1; enum incl. "user_edit", "file_tags"
  "soulid_version": "kinded_v2"
}
```

Plus three special claim shapes:

- **Name claim (mandatory, one per entity per batch):** the exact
  SoulID input tuple — the normalized parts `kinded_v2` hashes
  (artist: [name]; album: [artist, title]; track: [artist, title]).
  The compiler recomputes the SoulID from the tuple; a mismatch
  invalidates the whole entity's claims in that batch. This makes
  name claims self-verifying and supplies the names §7 compares
  against providers.
- **Display-name claim:** the entity's names as humans write them
  ("OK Computer", not the normalized "ok computer" the hash
  consumes). Display names are presentation, not identity: when an
  MBID is strong-verified for the entity, the compiler takes
  MusicBrainz's display names (CC0, §7.1); otherwise display names
  are floor-gated contributor claims, most-common-form wins, and are
  never used for matching. Whether provider-sourced display names
  (Deezer/Spotify spellings) may be republished is a step-0 terms
  item (§13); until it clears, they are not.
- **Relationship claim (`album_track`):** subject = album SoulID,
  field = `album_track`, value = `{track_soul_id, disc, position,
  variant}` where `variant` is a verified provider release ID when
  known. This is how tracklists — §1's headline feature — are
  expressed, verified (§7), and merged (§8).
- **Provider-link claims:** verification fetches return provider-side
  relations (this Deezer album belongs to that Deezer artist; this
  Spotify track sits on that Spotify album). The worker records
  these as verified published data (`provider_link` fields). They
  are what makes same-name disambiguation possible at all (§8):
  album and track SoulIDs are name hashes, so two same-name artists'
  albums collide under one artist name — only the provider-side
  links separate them.

Transport and identity rules:

- **Batches are signed, claims are not.** A batch is a canonical
  JSON payload (JCS, RFC 8785) of claims plus a header; the node
  signs the batch hash with its SIN-bound Ed25519 key, exactly like
  SIN request signing (PORTAL-DESIGN §3A.2; keys and signatures are
  base64url of the raw 32 bytes, as in `portal_keys`). Per-claim
  signatures would cost ~1 GB of signatures per large library and
  could never be checked by anyone anyway: the published corpus
  strips contributor identity (§9), so no consumer can verify an
  individual claim signature. Batch signing gives the compiler
  non-repudiation at ingest, which is the only place identity exists.
- **Server receipt time is authoritative** for the revocation window,
  re-verification age, and ordering. A node-supplied timestamp is
  informational only (node clocks are skewed and attacker-controlled).
- **Claim identity / dedupe:** `(subject, field, value, account
  blind)` (§12). Re-sending the same batch is idempotent. Claims
  older than the §9 retention window dedupe against the *frozen
  published state* instead: if the corpus already carries that
  (subject, field, value), a re-send is a no-op, never a second count.
- **Retractions:** a batch may carry retractions for claims the
  contributor's SoulSync no longer holds (album deleted, tag fixed).
  Retraction removes that account's support from **floor-counted**
  claims; a field that drops below its gate is withdrawn at the
  next compile (§10 withdraw list). Retraction never withdraws a
  strong-verified claim — those fall only to re-verification failure
  (§7). (Otherwise a one-holder item vanishing from a delta would
  announce the day its holder deleted it.) Retractions reach back
  90 days (§9); older claims are frozen counts and cannot be
  retracted individually.
- **Caps and quotas.** Steady state (mirroring SIN's report caps):
  ≤5,000 claims per batch, one batch per account per day. **Backfill
  is separate:** for the first 30 days after contributor approval an
  account may send up to 50,000 claims/day — at the steady-state cap
  a 50k-track library would take 50+ days and the step-0 library
  (~700k tracks) about two years, which would strangle the network
  at birth. What actually protects the verification budget is the
  **unverified-new-subject quota**: ≤20,000 new subjects per account
  per week enter the verification queue; the rest wait. Name claims
  are exempt from per-field-type limits (they are mandatory and
  self-verifying). All four numbers are initial values; the step-0
  spike (§13) resizes them.
- **Legacy IDs:** pre-v2 installs know only legacy SoulIDs, so in
  v1 they effectively **contribute nothing** — they can pull
  snapshots like anyone, but their exports can't form kinded_v2 name
  claims. The legacy→v2 mapping is supplied by *upgraded* installs,
  whose export serves both IDs for the same row; the compiler
  verifies the mapping by recomputing v2 from the row's name claim.
  Legacy subjects are accepted only as alias entries, never as v2
  subjects.

## 7. Verification

Majority voting is weak at 30 nodes — an attacker with a handful of
accounts outvotes the 1–2 honest holders of any niche item — and most
of what matters doesn't need voting at all. Verification has two
strength tiers:

- **Strong verification:** the claimed provider ID is fetched from
  the provider and *cross-matched*: normalized name and artist agree
  **and** an independent key agrees — ISRC for recordings,
  MusicBrainz ID for any kind, duration ±2s as a supporting (never
  sufficient) signal. **Both sides of a cross-match come from
  provider responses, never from the claim.** If the claimant
  supplies a Spotify ID and an ISRC, "Spotify's ISRC equals the
  claimed ISRC" proves nothing — the claimant controls both sides.
  The check is "the Spotify response's ISRC equals the MusicBrainz
  recording's ISRC", or "MusicBrainz links to this Spotify URL".
  Strong-verified claims publish at N=1 (§9).
- **`album_track` verification:** fetch the tracklist of a
  strong-verified provider release (one call per album — cheap) and
  match each claimed (disc, position) against the response's track
  at that position by normalized title (via the track's name
  claim). A variant is strong-verified when its full tracklist
  matches a fetched release. **Variant equivalence across
  providers:** two provider releases are the same variant when they
  share a MusicBrainz release ID, or a UPC, or a *variant
  fingerprint* — a hash of the ordered (disc, position, normalized
  title) sequence. Variants publish with their fingerprint, so a
  Spotify deluxe and a Deezer deluxe merge into one variant instead
  of living as two.
- **Plausible only:** name + artist + duration match, but no ISRC or
  MBID cross-match. Live versions, remasters, re-recordings, and
  clean/explicit pairs pass this test while being the *wrong* item,
  so plausible-only claims are treated as unverifiable: floor-gated
  (§9), clearly labelled in provenance.

Per provider in v1:

- **MusicBrainz, Deezer, Spotify:** verification fetchers built.
  MBID kinds are explicit per entity — artist → artist MBID, album →
  **release-group** MBID, track → **recording** MBID. SoulSync
  exposes *release* MBIDs; release → release-group resolution happens
  in the worker and is charged to the MusicBrainz budget below.
- **Tidal, iTunes, Discogs, Hydrabase:** claims accepted but held as
  unverifiable in v1 (floor-gated). Their fetchers are later work.

### 7.1 The verification budget (read this before promising dates)

One pass over the founding corpus is not a weekend job:

- ~50 libraries × ~50k tracks, heavy overlap → roughly 1–2M unique
  recordings, each potentially carrying several provider IDs.
- MusicBrainz allows ~1 req/s → 12–23 days for one pass over
  recordings alone, before release-group resolution.
- Deezer (~50 req / 5s) → days. Spotify: undocumented rolling limits
  on one app credential, tightened for dev-mode apps. iTunes
  (~20/min) and Discogs (60/min) are slower still.

Consequences, all mandatory:

- **Priority queue:** most-held subjects verify first (support is
  countable from claims; "most-searched" is not — search is local
  and there is deliberately no query telemetry), then oldest
  unverified. The long tail verifies over weeks. Coverage grows over
  time; §15 measures it instead of promising day-one completeness.
- **Batch endpoints where they exist** (Deezer/Spotify multi-get),
  per-provider caps, and a persistent queue in the worker — never a
  request handler.
- **Terms:** Spotify's and Deezer's developer terms restrict storing
  and redistributing their catalogue data. The published corpus
  therefore contains **MusicBrainz data (CC0) and bare ID links**
  (facts: "SoulID X is Deezer track Y"). Spotify/Deezer API
  *responses* are used for internal verification only and are not
  republished. Whether contributor-sourced durations and ISRCs may
  be republished (they usually *originated* in provider data
  upstream of the contributor's files — "no provider owns them" is
  an assertion, not a fact) and whether provider display names may
  be republished (§6) are explicit step-0 terms-check items; until
  they clear, those fields publish only from MusicBrainz or not at
  all. Year is MusicBrainz-only for the same reason (§8).
- **Re-verification:** sized from the budget, not a round
  percentage — the daily re-verification sample is capped at 5% of
  the day's provider-call budget (a flat 1%-of-corpus sample would
  be ~50k calls/day at 5M claims and would eat the MusicBrainz
  budget whole). A provider 404 from the worker's region is
  *unreachable*, not *deleted* (geo-restriction is real for Spotify
  markets and Deezer). A claim is dropped only on positive evidence
  of absence: a not-found from a second region, or MusicBrainz
  showing the entity merged/removed. There is no "provider data
  changed" trigger — no provider offers the worker a change feed —
  so staleness is handled by the sample plus claim age, nothing
  else.
- **One credential is a single point of failure** for verification
  throughput (and a ban risk if it behaves like a bulk crawler —
  millions of lookups from one IP is not "ordinary catalog traffic").
  Mitigations: the caps above, aggressive caching (a verified ID is
  never re-fetched inside 90 days except by the re-verification
  sample), and MusicBrainz-first ordering since MB is both the
  strongest signal and the most permissive source.

## 8. The merge — one library, run everywhere

Merging is a pure, versioned library (`merge_rules_version`), written
**once in Python** and run by the worker (compile) and by the node
(local view). There are two input shapes, and the doc is explicit
about them: the worker merges *claims → corpus fields*; the node
merges *corpus fields + fresh claims* (from provider search, and
later from peers) *→ a local view*. Same rules, both shapes.

The library includes the SoulID normalizer. SoulSync's `kinded_v2`
normalizer is Python; the worker/node copy is held byte-identical by
**golden test vectors** generated from SoulSync's implementation
(name tuples → SoulIDs, including the script-aware mark-stripping
cases). A vector failure blocks a compile.

Field rules:

| Field | Rule |
|---|---|
| Provider IDs | A **set** per provider. Same-name artists share an artist SoulID (artist = hash(name)); two bands called Nirvana have two true Spotify artist IDs. Never pick a winner for artists/albums. |
| MBIDs | Kind-explicit (§7). One release-group/recording/artist MBID per entity is expected; multiples are kept and flagged, as with provider IDs. |
| Tracklist | Variants are legitimate (deluxe vs standard share an album SoulID, since album = hash(artist, title)). Variants are keyed by the `album_track` claims' `variant` (verified provider release ID) or, failing that, by (disc count, track count). Never collapse variants. |
| ISRC | Set-valued. Strong-verified when it matches a verified provider ID's report. |
| Duration | Track SoulIDs ignore album (track = hash(artist, title)), so durations merge **per album_track context**, not per bare track: median of verified reports within a variant; outliers beyond ±2s of the median are dropped. A bare-track duration publishes only when all contexts agree within ±2s. |
| Year | MusicBrainz release-group year (verified) or **unpublished**. Provider years stay internal until the step-0 terms check (§7.1) clears republishing them. Edition years are a separate field and never feed this one. (No "earliest with ≥2 accounts": two accounts can set 1901.) |
| Genres | Not a conflict. Union them; publish rank order and the §9 coarse buckets, never exact per-genre counts. Disputed genres are marked never-auto-fill. |
| Anything unverifiable and disputed | Publish all values **with** the disagreement visible, marked never-auto-fill. |
| Everything | Fill empty fields only. Never overwrite local data. |

**Multi-valued fill rule (enrich):** provider IDs are sets at
*every* kind, tracks included — a track SoulID (hash of artist +
title) is shared by the live version, the remaster, and the single
edit, and each has its own true Spotify ID. When the corpus holds a
set for a field SoulSync stores singly, enrich fills only if
exactly one candidate survives disambiguation against the user's
local data:

- **Tracks:** disambiguate by `album_track` context — the user's
  album, its variant (§7 fingerprint), and the track's position in
  it. The candidate is the ID the verified release's tracklist
  carries at that position. No context, no fill.
- **Artists/albums:** disambiguate by the `provider_link` relations
  (§6): follow the user's local albums' provider IDs to the artist
  ID those albums link to on the provider's side. A candidate
  survives if **≥3** of the user's local albums for that artist
  name link to it, and no other candidate reaches 3. (Album SoulIDs
  alone cannot do this — both same-name artists' albums hash under
  the same name.)
- A shared strong-verified MBID settles any kind immediately.

If zero or ≥2 candidates survive, the field is display-only. This
rule, not a vote, resolves the Nirvana problem at the point of use.

**Provenance and retraction:** every published field carries value,
support (strong-verified / plausible / coarse account bucket §9),
and `merge_rules_version`. When write-back ships (§14.1), SoulSync
stores that provenance with the filled value (value, snapshot `seq`,
rules version) in a network-sourced marker. Network-filled fields
never promote to "user data": a later snapshot can update or retract
them (§10 withdraw list), and §2.1's "local wins" applies to the
user's own data only. Without this, one bad compile would be
permanent in every library — the compiler being wrong is a
when, not an if.

**Compile canary:** before publishing, the worker diffs the new
corpus against the previous one. The canary counts **modifications
and removals of existing fields only** — during bootstrap every
compile is nearly all additions, and a canary that halts every
early compile halts the network. If modified/removed fields exceed
2% of the existing corpus, or any single provider's claims change
at an anomalous rate, publication halts and the operator is
alerted. Until the corpus passes 10,000 subjects, publication
additionally requires a manual operator approve. A bad compile
must never reach nodes on schedule alone.

## 9. Publication rules

A SoulID is dictionary-matchable, so publishing a claim says "someone
in this network has this item". Two gates keep that honest, plus
batching to keep timing from giving the game away:

- **Strong-verified claims publish at N=1** (§7). The SoulID↔ID link
  is a public fact once verified. No contributor identity is attached
  — but see the batching rule: *when* it publishes still matters.
- **Everything else needs the floor:** ≥3 independent
  contributor-approved accounts asserting it. Below the floor, claims
  are held, not discarded; they publish when support arrives.
- **Publication batching (anti-fingerprinting):** new *subjects*
  (entities not in the corpus before) publish only in a release
  whose new subjects come from ≥5 distinct contributor accounts,
  **and** in which no single account supplies more than 20% of the
  new subjects — one account adding 50,000 subjects while four add
  one each is one library wearing a crowd costume; the excess rolls
  into later releases. Held subjects wait for a qualifying release,
  however long that takes: there is **no time-based fallback**,
  because in a quiet month a 30-day fallback would publish one
  contributor's additions alone, on schedule — the exact leak this
  rule exists to stop. Privacy wins over freshness here, explicitly.
  The first snapshot publishes only once ≥10 contributor-approved
  accounts have contributed — otherwise the "network corpus" is one
  person's library with extra steps. Deltas of *new fields on known
  subjects* follow the same batching.
- **Support is published coarse:** `strong-verified` (which carries
  **no account bucket at all** — a bucket on a strong-verified field
  could only ever read "1–2 accounts", marking exactly the sparse
  items), or `3+ accounts`. Exact counts are never published.
- **The no-MBID, unverifiable long tail** is floor-gated like
  everything unverifiable: such an item publishes when ≥3 approved
  accounts assert it, and not before. Nothing in this class is
  categorically excluded — but below the floor, items nobody can
  check and almost nobody holds are the closest thing to a
  fingerprint of one library, and they wait.

Operational rules:

- **Retention:** the claim → account-blind mapping (§12) is kept
  for **90 days** from server receipt for published claims; after
  that, the claim's support is a **frozen count** — it can no
  longer be retracted, revoked, or incremented by a re-send (§6).
  A published claim leaves the corpus after the window only by
  re-verification failure (§7) or canary/operator action (§8),
  never by count arithmetic. Held (below-floor) claims keep their
  blind for as long as they are held — otherwise a resubmission
  would double-count one account toward the floor — up to a
  **maximum hold of 12 months** from first receipt, after which
  the claim is discarded outright. The privacy copy (§12) states
  both numbers; it does not say "90 days" unqualified.
- **Revocation reaches back 90 days.** Revoking an account excludes
  its claims inside the window and the next snapshot is clean.
  Claims older than the window can no longer be attributed and
  cannot be individually revoked — stated plainly, not hidden.
  (Live P2P has no recall at all; a 90-day recall window is still
  a structural advantage.)

## 10. The snapshot, the manifest, and the keys

**Snapshot format:** a single SQLite file — the corpus *is* a
queryable database: entities (with display names, §6), merged fields
with provenance, provider-link relations, the alias table, and a
full-text index over names.

**Search mechanics (specified, because "fuzzy" is not a
mechanism):** FTS5's trigram tokenizer generates *candidates* —
queried as an OR of the query's trigrams, never a `MATCH` AND
(trigram `MATCH` requires every query trigram to be present, and
"compter" contains trigrams "computer" doesn't, so the naive form
misses the very typo it was chosen for). Candidates are reranked by
edit distance (Levenshtein) over normalized names. Tokens under 3
characters ("ok") can't form trigrams and are served by a prefix
index over normalized names. Trigram index ≈3× the text size;
rerank cost is bounded by candidate count and is measured against
the §15 latency target in the step-0 spike.

Contributor-supplied names are length-capped (512 chars) at ingest
and escaped at render everywhere they surface in node UI or search
results.

**Size — arithmetic, then a spike:** per track row, hex SoulID +
provider IDs + provenance ≈ 250–400 bytes before indexes; at 1–2M
tracks plus albums/artists, FTS trigram index, and alias table, the
honest planning range is **100–300 MB uncompressed, less compressed**
— the earlier 30–60 MB guess did not survive per-row math. §13 step 0
measures a real library export and replaces this range with a
number. If the full file outgrows ~300 MB compressed, split by kind.

**Manifest:** snapshots are distributed by a signed manifest:

```json
{
  "compiler_id": "metasync-official",   // §11: nodes pin roots per compiler
  "seq": 1042,                       // monotonic; node rejects seq <= current
  "issued_at": "…", "expires_at": "…",
  "schema_version": 1, "merge_rules_version": 3,
  "min_node_version": "0.3.0",
  "full": {"logical_digest": "…", "url": "…"},
  "delta": {"from_seq": 1041, "logical_digest": "…", "url": "…"},
  "withdraw": [{"subject": "soul_…", "field": "…", "value": "…"}],
  "snapshot_key": {
    "id": "…", "pubkey": "…", "not_before": "…", "expires_at": "…",
    "root_cert": "<root signature over (pubkey, id, not_before, expires_at)>"
  },
  "sig": "<signature by the snapshot key>"
}
```

- **Digests are logical, never file bytes.** A SQLite file's bytes
  depend on page layout, freelist, and rowid placement; a node
  applying a changeset and rebuilding FTS will *never* reproduce
  the compiler's bytes, so a file hash would fail every check. The
  `logical_digest` is SHA-256 over a canonical ordered dump of the
  content tables (entities, fields, links, aliases — excluding FTS
  and all SQLite internals), computed identically by compiler and
  node. File downloads are additionally integrity-checked in
  transit by the CDN's own hashes, but acceptance is by logical
  digest after load.
- **Deltas are cumulative from the last weekly full snapshot** —
  one delta to apply, never a chain. A delta is a SQLite
  session-extension changeset; after applying, the node rebuilds
  the FTS index (cheap relative to download size; patch-in-place
  FTS is not worth the corruption risk) and verifies the logical
  digest. A missed delta, a digest mismatch, or a node older than
  the last full → download the full snapshot.
- **Withdraw list:** entries are `(subject, field, value)` triples,
  cumulative across the last 90 days of seqs (a node that skipped
  seqs still sees every withdrawal it missed; a full snapshot never
  contains withdrawn values in the first place). Withdrawals matter
  most once write-back exists (§8), which is why the format is
  frozen now.
- **Freshness:** content addressing proves integrity, not
  freshness. Anti-rollback/anti-freeze: nodes refuse `seq` ≤
  current; refuse a `seq` jump larger than 4 per day elapsed since
  the last accepted manifest (a runaway seq is the signature of a
  stolen key trying to wall off the future); clamp `expires_at` to
  the snapshot key's own expiry; an **expired snapshot key = the
  manifest is refused**, not warned about. An expired *manifest*
  warns and the node keeps serving its local corpus.
- **Keys, two tiers:** an **offline root key**, pinned in the node
  (as a per-compiler list, §11), signs **online snapshot keys**
  with 90-day expiry via the `root_cert` carried in every manifest
  — the node verifies the full chain (root → online key → manifest
  signature) from the manifest alone. The worker holds only the
  online key, so daily automated signing never touches the root.
  Rotation = root certifies the next online key before the old one
  expires. **Compromise:** the root can sign a *revocation
  statement* (revoked key id, and optionally a reset of the `seq`
  floor). Nodes process root-signed statements **regardless of
  `seq`** — a revocation that had to out-sequence the attacker's
  manifests could never arrive, which is why the seq rule has this
  one root-shaped exception. (This key is *a* true secret of the
  network, not the only one — SIN's token-signing key and API
  pepper exist too; the operational point is that this one is
  offline.)

**Distribution — decided: public.** The corpus is public facts
(CC0 MusicBrainz data + ID links, §7.1); gating it would buy nothing
(the first snapshot already reveals the aggregate) and would break
CDN hosting and mirroring. So: static file + manifest over HTTPS/
CDN, anonymous download, no download log kept by the pool. (The
CDN's own access logs record downloader IPs like any web traffic;
retention is set to 30 days where the CDN allows configuring it,
and those logs are never joined to accounts.) Portal/exchange auth
gates *contribution*, not *reading*. This supersedes the §3A.7
pattern (gated reads) for this service, and §12 says so.

## 11. The road to peer-to-peer (staged, no rewrites)

The node resolves a lookup through a **pluggable source chain**:

```
local snapshot → provider search → (future) live peer query
```

The search UI never knows which source answered. On top of that:

1. **P2P distribution first.** Peers mirroring content-addressed
   snapshots is the first decentralization step: cheap, safe, removes
   the bandwidth/bus-factor concentration. No new protocol
   semantics — the manifest's signature model (§10) was designed
   for untrusted transport.
2. **Federation.** SIN pools already allow multiple servers. A node
   may subscribe to snapshots from several compilers and merge them
   locally with the same library — **with one hard limit stated
   now:** support counts are never summed across compilers. Account
   blinds are per-compiler (different HMAC secrets, §12), so the
   same account contributing to two compilers cannot be deduplicated
   across them, and a hostile compiler can assert "verified" without
   checking. Cross-compiler merge therefore treats each compiler as
   one trust domain: fields carry their compiler of origin
   (`compiler_id` is in the manifest and snapshot metadata, §10),
   counts stay per-compiler, root keys are pinned **per compiler**
   (the node's trust list is a list, not a single key), and the
   node's compiler trust list decides whose strong-verified it
   accepts. Several independent compilers = no single owner of the
   corpus, with zero P2P networking — but it is federation of *trust
   domains*, not one merged crowd.
3. **Live peer queries last, if ever.** Revisit only when active
   nodes pass a few hundred *and* the measured complaint is snapshot
   staleness, not coverage. When it lands, peer answers are claims
   (§6) fed into the same merge library's local-view shape; the
   privacy costs in §3 get a fresh decision at that scale, where
   cover traffic is real.

## 12. Privacy — this service amends SIN's rules for itself

SIN's published privacy rules (PORTAL-DESIGN §4, P1–P7) promise that
SIN never joins keys to people (P3) and that the operator cannot see
any signal attributable to an account (P5). **Those promises are
SIN's, and SIN keeps them.** This metadata service cannot keep them:
counting distinct contributors per claim and revoking a poisoner's
claims both require knowing which account asserted what. Pretending
otherwise would be lying to users. So:

- The metadata service is a **separate function with separate
  tables**. The precise boundary: SIN's report and aggregate tables
  (the tables P1–P7 protect) are off-limits to metadata code, and
  metadata tables are off-limits to SIN code. The metadata *ingest*
  path necessarily resolves token → key → account (to check
  `contributor_approved` and compute the blind) — that resolution
  is exactly what P3 forbids inside SIN, and this amendment scopes
  the exception to this service's ingest path and nothing else.
  The amendment is written into the portal copy before the
  contributing opt-in ships (§13 step 3) — a user deciding whether
  to contribute sees the actual rules, not SIN's.
- **Metadata privacy rules (Q-rules):**
  - **Q1.** Claims are stored against an **account blind**:
    `HMAC(secret, account_id)`, the secret held by the metadata
    function only. Dedupe, floor-counting, and revocation all work
    on blinds; raw account IDs are never written to metadata tables.
  - **Q2.** The blind protects against a leak of the metadata
    tables, **not against the operator**: at this scale the
    operator can re-compute the HMAC over every account ID in
    seconds and attribute any claim. The opt-in copy says this in
    plain words: contributing to the metadata network is **not
    anonymous to the operator**; SIN reporting still is. Users who
    don't accept that don't contribute — and can still pull
    snapshots.
  - **Q3.** Retention per §9: published claims 90 days, then frozen
    counts; held claims up to 12 months, then discarded. No play
    data, no listening data, no file paths are ever in a claim
    (§6 schema has no field that could hold them).
  - **Q4.** Published data carries no contributor identity and only
    coarse support (§9).

| Who | Learns what |
|---|---|
| Other nodes | The aggregate corpus (public, §10). Per-item: coarse support buckets and publication batching (§9) mean a new item reveals "≥5 accounts contributed new material this cycle", not who or exactly when. Not literally nothing — the corpus itself is the network's combined knowledge, published on purpose. |
| The compiler (operator's server) | Contributing accounts' holdings at claim granularity, via blinds (Q1/Q2) — attributable by the operator (Q2), retained 90 days for published claims and up to 12 months for held ones (§9). The honest cost of v1, stated before opt-in. |
| Providers | Substantial verification traffic from the worker's credentials — bulk-shaped, a terms/ban risk managed per §7.1. It carries no user data. |
| Non-contributors | Nothing; pulling is public and anonymous (§10). |

Contrast with live P2P (§3): every peer learns your IP, your
queries, and — by enumeration — plausibly your whole library.

## 13. What v1 builds, in order

0. **Spike (before any commitment):** run one real library (~70k
   albums) through the export — with a throwaway script, since the
   node's export client is step 4 (G3) — and measure claim counts,
   snapshot size, FTS size, search latency against the §10
   mechanics, and the verification-call census by provider.
   Provider terms re-check: Spotify/Deezer storage clauses,
   contributor ISRC/duration republication, provider display
   names. Note the spike library is ~14× the §7.1 per-library
   average: it measures per-library numbers exactly, but the
   cross-library overlap behind the 1–2M unique-recordings figure
   stays an assumption until several libraries have contributed,
   and the §7.1/§10 ranges are re-derived with the spike's real
   size. The timeline is estimated *after* the spike, not before.
1. **Frozen specs:** claim canonical form + batch format (incl.
   name, display-name, `album_track`, and provider-link claims),
   snapshot schema (incl. `compiler_id` and the search mechanics),
   manifest + logical-digest + delta format, merge-rules v1.
   Golden SoulID vectors generated from SoulSync.
2. **Keys:** root key ceremony, online snapshot key, node-side
   pinning + manifest verification.
3. **Privacy plumbing:** Q-rules implemented (blinds, retention job),
   `contributor_approved` gate in the portal, portal copy amendment
   (§12), contributing opt-in copy. *Nothing attributable is
   collected before this step lands.*
4. **Claims endpoint + node contributor:** export client (G3),
   batch builder behind the opt-in, caps/quotas (§6).
5. **Verification worker:** Python, persistent priority queue,
   Deezer/MusicBrainz/Spotify fetchers, strong/plausible tiers (§7).
6. **Merge library + compiler:** §8 rules, canary, manifest +
   snapshot + delta emission.
7. **Node snapshot consumer:** manifest poll, signature checks,
   delta apply / full fallback, local SQLite store. (Can start at
   step 1 against a fixture snapshot.)
8. **Search UI** in the node: §1 experience over the source chain
   (§11), with escaping/length caps (§10).
9. **Enrichment view:** merged record vs local gaps, display-only.
10. **Scaffold cleanup:** *delete* `GET /v1/records/:soulId` from
    `src/metasync/` (a per-item server lookup is exactly the query
    leak this design refuses), remove `METASYNC_PROPOSED_
    CORROBORATION_N`, and rewrite the `schema.ts` header — including
    removing its "no user identity beyond the node key" line, which
    §12 reverses for this service and says so.

## 14. Open decisions (Boulder)

1. **Enrichment write-back:** v1 is display-only (§5). Write-back
   needs (a) a SoulSync API write path for fill-empty fields and
   (b) the provenance tagging of §8 as a precondition. Its own
   SoulSync PR, after v1 proves the corpus. (Not a v1 question;
   recorded so it isn't lost.)
2. **Genres in v1:** included (§8) — unverifiable, union-merged,
   never-auto-fill when disputed. The alternative is dropping the
   noisiest field from v1 entirely.

Decided and therefore *not* open: compiler hosting (Python worker +
Neon claims store, §5); snapshot distribution (public, §10); delta
cadence (weekly full + daily deltas, subject to §9 batching).

## 15. Success measures

V1 measures what v1 actually changes:

- **Snapshot hit rate:** on a fresh test library, the share of
  albums/tracks with a corpus record — measured at 15, 30, 50
  contributors.
- **Verification throughput and coverage-over-time:** claims
  strong-verified per day; share of claimed subjects published.
- **Search latency:** local, <100 ms at the §10 corpus size.
- **Zero network-sourced overwrites of local data.** (Invariant,
  not a target.)

Deferred to the write-back phase (§14.1): provider API calls per
newly imported album — target ~5× fewer than today. It cannot be a
v1 metric: while enrich is display-only, SoulSync still makes its
own calls.

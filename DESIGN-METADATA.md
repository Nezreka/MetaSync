# DESIGN-METADATA.md — the MetaSync metadata network

Status: design, rewritten Oct 26, 2026 to Boulder's model. Folded
Oct 27, 2026: a night of hostile review (three Claude passes plus a
"what makes it 10/10" pass) and Boulder's depot decision are now
part of this doc — where folded text contradicts earlier text, the
folded text is the design. A second review round the same day
folded the attestation lifecycle, the depot's bounds, the
no-laundering rule, and the promotion floor — same rule: folded
text wins. A third round the same day folded the promotion
floor's legs, revocation delivery to live sessions, and depot
staleness — same rule again. A fourth round folded the floor's
leg (b) key check and the admission-time revocation check —
same rule again. The first draft staged a centrally
compiled snapshot as v1 and live peer queries
as a later stage. **That staging is retired.** Boulder's decision
(Oct 26, 2026): there is no server-side store of metadata, at any
stage. Queries go out live, peers answer, the asking node merges.
Adoption pace is not a risk to manage — the network is only upside,
at whatever speed it grows. This doc is the design of record; where
the earlier draft or any scaffold code disagrees, this doc wins.

Consequences already in flight:

- PR #10 (claim format, merge library, SoulID port) is unaffected —
  it was always transport-neutral and is the foundation here too.
- PR #11 (server claims ingest + migration 0011) is **superseded and
  closed unmerged**. Migration 0011 is never applied. There is no
  claims table, no blind secret, no contributor-approval gate,
  because there is nothing server-side to gate.
- PR #12 (node contributor) is **superseded in its transport only**:
  its export→claims builder, opt-in switch, and preview are reused
  verbatim by the node responder (§5); its daily push to a server
  endpoint is deleted.
- The `src/metasync/` scaffold (per-item server records) is deleted
  as part of the build (§13) — a per-item server lookup is exactly
  what this design refuses to have.
- The SoulSync side is a **depot**, not a write-back (Boulder,
  Oct 27, 2026): network data is stored in a separate SoulSync
  extension and promoted into library records only by SoulSync's
  own workflows (§1, §14.1).

SoulID is only for this service. SIN does not use SoulID; SIN signals
are keyed by MusicBrainz IDs (see `src/sin/`). The two halves of
MetaSync share exactly one thing: the portal account and its bound
node key. No data crosses between them (§12).

## 1. The experience

A user searches SoulSync for an artist, album, or track. SoulSync
relays the query to the user's own MetaSync node. The node checks
what it already knows, then asks the network. Every online node that
has the item answers with what it knows. The asking node combines
all answers into a single result — duplicates merged, gaps in one
answer backfilled from another — and hands that one result back to
SoulSync.

**SoulSync does not write that result into its library.** It stores
it in the **depot**: a separate extension inside SoulSync that holds
collected network metadata — merged records with their provenance —
under its own management. The merged result is packed the way no
single service's result is: the complete tracklist, every provider
ID anyone has matched (MusicBrainz, Spotify, Deezer, Tidal, iTunes,
Discogs), ISRCs, durations, year, genres. The depot is where that
richness lives until something earns it a place in the library.

The depot is a cache with the same bounds as the node's (§6), not
a second, unbounded store that escapes them. Provenance in the
depot is **per field**, not per entry — a merged record has many
origin batches, so each field carries the **claim identities**
behind it — (origin key, subject, field, value), §6's dedupe
identity — its **verification tier** (§7, §9), and an
**expiry** derived from the origins' `asserted_at` under §6's
aging rule. Batch ids are **evidence** for a claim, never its
key: origins re-mint their batches weekly (§6), and a re-minted
batch refreshes the evidence — and the `asserted_at` the expiry
derives from — for the same claim identities, pushed to the
depot as an update through the mechanism below. Keying on the
claim rather than the batch is what keeps a live claim's depot
fields from being stranded under a dead batch id every week.
A verified field also carries **`verified_at`**: the
depot cannot re-verify on reliance the way the node does (§6) —
promotion runs inside SoulSync and never calls the node — so the
depot's version of that rule is a timer. Ninety days after
`verified_at` the field's tier drops to unverified in the depot
unless the node pushes a fresh verdict, and §8's floor (a)
requires a verdict inside that window. Stated plainly, that
means **depot verifications lapse at 90 days by default**:
depot reliance never reaches the node, so nothing refreshes
them in the ordinary course, and a lapsed field's promotion
falls back to §8's floor legs (b) and (c). A node *may* run a
**budgeted proactive re-check** of fields its depot actually
holds and push fresh verdicts before they lapse — an option
with a budget (§7.1's etiquette applies), never a requirement,
and never a precondition for the depot to be useful. The node pushes
**invalidations and updates** to the depot over the local API — a
revocation (§10), an expiry, a superseding answer — and SoulSync
applies them: an invalidated entry dies in the depot, and its
death stops future promotions from it. An invalidation also
triggers a **re-merge** on the node, whose result is pushed as
an update, so a field fed by several batches loses only the dead
batch's contribution, not the whole record. Delivery of these
pushes is guaranteed, not best-effort — a durable outbox on the
node and a reconcile on reconnect (§5). What invalidation cannot
do is reach
backward: a value already promoted into a file's tags is **not**
rewritten by the network. The entry is flagged, future promotions
stop, and any correction to written tags happens only through
SoulSync's own workflows (§8).

The depot is used two ways:

- **Display.** Search results and detail pages read from the depot.
  Search shows the merged record as a source of its own — a
  super-source, because it is effectively all sources combined. The
  detail-page jump-start and the combined search tab (§13 step 8)
  read from the same depot; there is one store of network data in
  SoulSync, not one per surface.
- **Workflows.** SoulSync's own jobs consult the depot when they
  need facts — chiefly **tagging during post-processing**, which
  today runs live provider searches at tagging time. The depot is
  the tagging pipeline's first source; a live search happens only
  on a depot miss. When the pipeline matches a file against depot
  data, *its* matching judgment promotes that data into the
  library — subject to §8's promotion floor, which matching alone
  never satisfies. The network proposes; SoulSync's pipeline
  disposes.

Collecting by exact lookup is expected to be the everyday workhorse:
SoulSync already knows the IDs of what the user owns, so "who can
complete this album?" is a precise question the network can answer
usefully even when it is small — and each answer lands in the depot,
ready for the next tagging run. Text search is the headline; exact
lookup is the daily value.

## 2. Principles

1. **Local data always wins — and the network never writes to it.**
   Network data is never written into library records on arrival;
   it lands in SoulSync's depot (§1), labelled, provenance kept.
   Only SoulSync's own workflows promote depot data into the
   library, through their own matching, and promotion fills empty
   fields only. Promoted fields carry their provenance (§8). When
   the network's answer changes, the depot entry is corrected or
   dropped (§1) and future promotions from it stop — but a value
   already written into tags is flagged, not rewritten by the
   network; corrections to tags happen only through SoulSync's
   own workflows.
2. **No audio. Ever.** Metadata only — no files, no file paths, no
   listening data.
3. **No metadata on the server. Ever.** The server stores accounts
   and keys (it already does, for SIN) and nothing else. Music data
   lives on nodes. The relay (§10) keeps the who's-online list in
   its own memory only and logs no query content — *routes and
   forgets* is a written rule of the service, not a vibe.
4. **Verify, don't vote — and know where the data comes from.**
   Claims are built from SoulSync libraries whose contents were
   matched against providers when they were imported, so library
   data is *assumed pure*: it is displayed and deposited freely,
   not re-checked on arrival. **One exception, and it is marked:**
   a value the depot promoted into a library (§1) was matched
   against the *network*, not against a provider, so it does not
   inherit the premise. SoulSync marks every promoted value with a
   provenance bit in its own data; the export carries that bit
   (§5.1 G1), and the claim build emits a marked value with source
   **`network`**. A `network` claim is the network's own output
   coming back — it counts **zero support** everywhere (§9) and
   is never re-served as own-library data. Zero support is not
   zero use: a `network` claim may still be **promoted when this
   node strong-verifies it itself** (§7, §8 floor (a)) — the
   node's own check earns that, never the network's agreement.
   Without the mark, one network answer promoted on three nodes
   would re-enter as three independent asserting keys and
   launder itself into support; the mark is what keeps the
   premise honest. The mark also covers what a promoted value
   *drags in*: fields **fetched by way of a promoted ID** —
   tagging takes a depot-promoted provider ID, fetches the rest
   of that release from the provider, writes those fields too —
   are marked `network` as well. A steered fetch does not
   launder: everything downstream of a promoted ID inherits the
   mark, so a network association cannot mint provider-sourced
   independence for itself on any node it reaches. One path the
   mark cannot follow, recorded as an **accepted risk**: values
   written into **file tags** travel with the files (SoulSync is
   a Soulseek client), and on re-import a tag-sourced provider ID
   reads back as provider-sourced (§5.1 G1 treats provider IDs as
   self-describing). Once a value has been promoted above §8's
   floor, its life inside tags is the file's own metadata;
   closing the re-import loop would be SoulSync-side provenance
   work, noted for §13 step 8 — this design does not paper over
   it with a tag provenance frame. Verification (§7) grades answers — it sets the
   labels and depot ranking, and gives a node a self-run check
   where one exists. Counting agreeing nodes orders display and
   depot candidacy; it never, by itself, promotes data into a
   library — SoulSync's own pipelines do that, above §8's floor
   (§14.1).
5. **The network is a hint cache, not an authority.** A bad hint
   costs at most one verification call and gets dropped (§8's
   promotion floor is where that call is spent). SoulSync works
   fully with the network absent.
6. **SoulIDs are computed, never registered.** A SoulID is a
   deterministic hash of public names (SoulSync SoulID v2,
   `kinded_v2`). No registry, no minting service, no central
   database of SoulIDs. Any install derives the same ID offline.
7. **Answering is its own opt-in**, separate from joining SIN and
   separate from asking. A node may ask without ever answering
   (§9's node modes). The switch is off by default, and a preview
   shows what the node would answer with before it answers anything.

## 3. Why live queries — and the honest problems

The model is live because Boulder's idea is a shared, decentralized
network, and a centrally stored corpus is a different product: a
worse MusicBrainz that one operator has to run. The live model's
known problems are real; each has an answer in this design, and the
remaining cost of each is stated, not hidden.

- **Availability.** An answer exists only if a node holding the item
  is online and answering. *Answer:* caching with re-serving (§5).
  Every node keeps the answers it receives and may answer later
  queries with them, so knowledge spreads edge-to-edge like torrent
  pieces — one node having an item is enough for the nodes that
  learned it to answer for it. *Remaining costs, both accepted:*
  the true long tail — items held by one node, never asked about
  while it was awake — stays unreachable until that node is online;
  and re-serving is each user's own switch (§9's default is
  own-library-only), so the spread happens at the speed of users
  opting in, not at the speed of queries. Early on, most answers
  come from origin libraries, live. That is the honest shape of
  the mechanism, and the network still only grows.
- **NAT.** Home nodes cannot accept inbound connections. *Answer:*
  the relay (§10). Every node holds one outbound connection open to
  the relay; questions and answers travel on connections the nodes
  themselves opened. The relay is a switchboard, not a warehouse.
  *Remaining cost:* a relay must exist and be run by someone. In v1
  it is a small program alongside the existing SIN backend (§10).
- **Privacy.** A query reveals interest; an answer reveals holding.
  *Answers:* node keys are pseudonymous (never accounts, never IPs
  peer-to-peer — the relay hides them); the relay enforces per-node
  query budgets, which slow enumeration; nodes choose how much to
  answer (§9 modes). *Remaining cost, stated plainly:* budgets do
  not make enumeration impossible — over time a determined asker
  can profile what a stable answering key holds, and the opt-in
  copy says so (§12 Q3).
- **Text search over peers.** A DHT cannot do it, and broadcasting
  every keystroke is abuse. *Answer:* two query kinds (§10) — exact
  lookups by ID (cheap, precise), and text queries broadcast whole
  (one per search, not per keystroke) with a short answer window.
  At the network's realistic sizes — tens to low hundreds of nodes —
  broadcast is fine. The trigger for redesigning routing is written
  down in advance: roughly 1,000 concurrently answering nodes at
  sustained query budgets. Past it, routing is a named future
  decision (§11), not something v1 pre-builds.

## 4. Why not Hydrabase's shape

SoulSync already ships a Hydrabase client (`core/hydrabase_client.py`),
so the comparison is concrete. Hydrabase is a live search over a
shared WebSocket: the query goes out, the call blocks up to 8
seconds, results come back shaped like a provider's and are consumed
as-is. This design keeps the live feel and fixes the trust shape:

| | Hydrabase | This design |
|---|---|---|
| Knowledge lives | Whoever is connected right now | Nodes' libraries **plus** every node's answer cache |
| Answers | Provider-shaped results, taken as-is | Signed claim bundles, merged locally, spot-checked against providers (§7) |
| Identity | Names re-resolved per query | SoulID v2 — one deterministic ID on every install |
| After the query | Nothing is kept | Answers are cached and may be re-served (§5) |
| Failure mode | Timeout = nothing | No answers = SoulSync's normal sources, unchanged |

Hydrabase IDs stay compatible as one more provider ID in a claim.
A Hydrabase ID is unverifiable (no public catalogue to check it
against), so it is treated like any unverifiable claim (§9).

## 5. Architecture

```
        (the user's machine)                       (Boulder's server)

SoulSync ──local API──► MetaSync node ──one open connection──► relay
                           │  ▲                                  │
                           │  └──── questions / answers ─────────┤
                           ▼                                     ▼
                    local stores:                        other nodes,
                    · own-library claims                 same shape
                    · answer cache (re-servable)
                    · per-node reputation
                                                       the relay reads no
                                                       database — the guest
                                                       check happens at the
                                                       SIN token exchange
                                                       (§10)
```

- **SoulSync ↔ node:** local, the pairing that already exists for
  SIN charts. SoulSync relays a search to the node and receives one
  merged result, which it stores in its depot (§1). The same local
  API carries the node's **invalidation and update pushes** back
  to the depot — revocations, expiries, superseding answers (§1)
  — which SoulSync applies to the matching depot entries; the
  node is the only party that can know a network answer died, and
  the depot must not outlive it. These pushes are **not
  fire-and-forget**: the node keeps a **durable outbox** of
  pending invalidations and retries until SoulSync acknowledges
  them, and whenever SoulSync reconnects, a **reconcile** step
  runs — SoulSync presents the claim identities its depot fields
  depend on (origin key, subject, field, value — §1, §6), the
  node answers which are still live, and dead ones are
  dropped. A claim its origin has re-minted (§6) is live under
  the same identity: re-minting refreshes the evidence, never
  the identity the reconcile keys on. A push lost because SoulSync was down is a delay,
  never a silent hole. SoulSync never talks to the
  relay or to peers, and its library records are written only by
  its own workflows (§14.1).
- **Node → relay:** one persistent outbound connection (WebSocket),
  authenticated at connect by the node's SIN key exchange (§10):
  the exchange checks the binding, the relay verifies the token
  offline, and from then on the connection carries queries and
  answers. If the connection drops, the node reconnects; the
  relay's presence list rebuilds itself.
- **The node's local stores** (all on the user's machine):
  - *Own-library claims:* the node walks its SoulSync's metasync
    export and builds claims (§6) exactly as PR #12's contributor
    did — but instead of pushing them anywhere, it keeps them in a
    local claims store and answers queries from it.
  - *Answer cache:* every answer the node receives is stored with
    its provenance (which node key asserted it, the claims' own
    sources). Cached claims age from the origin's `asserted_at`
    (§6), and batches from revoked origins are dropped when the
    issuer-signed revocation list (§10) names their key — and the
    node pushes that same death to SoulSync's depot (§1). Behind
    the cache sits a **retained index** keyed by **claim
    identity** — (origin key, subject, field, value) →
    (`asserted_at`, the batch ids seen as evidence, the depot
    fields it fed) — kept for as long as any depot field depends
    on that claim. Batch ids sit in the index as evidence only:
    a re-minted batch (§6) refreshes the entry's `asserted_at`
    and evidence in place, so weekly re-minting never churns
    the index or the depot fields hanging off it. Cache eviction under
    size pressure (§9) drops claims, never the index: eviction
    must not sever the depot's link back to the cache, or a
    revocation naming a key could no longer resolve to the depot
    fields that key fed. Cached claims may
    be **re-served**: when a later query matches, the node may
    answer with cached claims it learned from others, labeled as
    re-served, with the original signatures intact (a re-served
    bundle is the origin node's signed bundle, forwarded — never
    re-signed as the forwarder's own).
  - *Reputation:* a local score per node key, from how that key's
    answers fared under §7 checks. Private to this node, never
    shared, never leaves the machine.
- **The relay:** §10. It stores nothing, holds presence in memory,
  and has no database access at all — the guest-list check happens
  at the SIN token exchange, and the relay verifies tokens
  offline. Attestations (§9) are issued at the same exchange but
  verified by asking nodes, from inside the batches themselves;
  the relay never handles them.

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
  treated as unverifiable. **One bit of per-field provenance is
  not a gap but a build requirement:** the export must say, per
  value, whether it was promoted from the depot (§2.4) — SoulSync
  marks promoted values, and the builder emits a marked value with
  source `network`. Without that bit the depot launders network
  claims into independent support, so the bit ships with the claim
  build, not after it.
- **G2 — no user-edit marker.** The export cannot distinguish a
  hand-edited title from provider data, so the `user_edit` value in
  the §6 `source` enum is **reserved** — nothing can emit it in v1.
  Recomputing the SoulID from a name claim does *not* catch edits
  (SoulSync computed the ID from its own possibly-edited names, so
  the recompute always agrees). The recompute catches implementation
  bugs and cross-install inconsistency. What catches edits is §7's
  comparison of claimed names against provider data.
- **G3 — the node's export client exists** as of PR #12's builder
  (cursor walk + `since`), reused by the responder unchanged.

## 6. The claim — the unit of data

There are no "records" passed around. The atomic unit is a **claim**:
one field of one entity, asserted by one node.

```json
{
  "subject": "soul_…",            // SoulID v2 of the entity
  "kind": "track",                // artist | album | track
  "field": "isrc",                // provider id, mbid, duration, year, …
  "value": "USRC10000001",
  "source": "deezer",             // see G1; enum incl. "user_edit", "file_tags", "network"
  "soulid_version": "kinded_v2"
}
```

Plus three special claim shapes:

- **Name claim (mandatory, one per entity per batch):** the exact
  SoulID input tuple — the normalized parts `kinded_v2` hashes
  (artist: [name]; album: [artist, title]; track: [artist, title]).
  Any node recomputes the SoulID from the tuple; a mismatch
  invalidates the whole entity's claims in that batch. This makes
  name claims self-verifying and supplies the names §7 compares
  against providers. The rule is per *batch*, not per answer: an
  answer's two batches (§13 step 4) each carry the name claim, so
  each batch is independently verifiable on its own.
- **Display-name claim:** the entity's names as humans write them
  ("OK Computer", not the normalized "ok computer" the hash
  consumes). Display names are presentation, not identity — never
  used for matching. Re-serving follows §7.1's allowlist, computed
  like every other claim's: a display name travels only when it is
  MusicBrainz-sourced. Provider spellings (Deezer/Spotify) and a
  node's own local spellings are answered from its own library but
  **not re-served**, until the §7.1 terms check clears them.
- **Relationship claim (`album_track`):** subject = album SoulID,
  field = `album_track`, value = `{track_soul_id, disc, position,
  variant}` where `variant` is a verified provider release ID when
  known. This is how tracklists — §1's headline feature — are
  expressed, checked (§7), and merged (§8).
- **Provider-link claims:** relations learned from provider
  responses (this Deezer album belongs to that Deezer artist; this
  Spotify track sits on that Spotify album). Shape: `subject` =
  SoulID of the *from* entity, `field` = `provider_link`, `value` =
  `{provider, from_id, to_kind, to_id, relation}` — e.g. subject =
  an album's SoulID, value = `{provider: "deezer", from_id:
  "3135556", to_kind: "artist", to_id: "399", relation:
  "album_of"}`. `from_id`/`to_id` are the provider's own IDs for
  the two entities; `relation` names the from→to direction
  (`album_of`, `track_of`, `artist_of`). These links are what makes
  same-name disambiguation possible at all (§8): album and track
  SoulIDs are name hashes, so two same-name artists' albums collide
  under one artist name — only provider-side links separate them.

Transport and identity rules:

- **Answers are signed claim batches.** A batch is a canonical JSON
  payload (JCS, RFC 8785) of claims plus a header `{batch_id, node,
  asserted_at, nonce, attestation}` — `batch_id` is the
  **SHA-256** digest of the batch's JCS payload computed with
  the `batch_id` field itself left out (a digest cannot contain
  itself; verifiers recompute it the same way), `node` the
  asserting node's key,
  `asserted_at`
  the node's clock at minting (cache ages run from it — see
  Retractions, below), and `attestation` the origin's current
  pool-signed binding attestation (§9), embedded at minting so it
  travels with the batch everywhere the batch goes, re-serving
  included. Askers validate the batch against it under §9's rules;
  a batch without one is displayable but counts zero support.
  The batch is **not bound to any query**: the same batch may answer
  many queries over its life, which is what makes re-serving (§5)
  possible. The query binding lives one layer out, in the unsigned
  `answer` message (§10) that carries the batch — routing metadata,
  not part of the signed claim. The answering node signs the
  **32 raw bytes of that SHA-256 digest** with its SIN-bound
  Ed25519 key — exactly what PR #10's `claims.py` does — in the
  manner of SIN request signing (PORTAL-DESIGN §3A.2; keys and
  signatures are base64url of the raw 32 bytes, as in
  `portal_keys`). The digest covers the whole canonical payload
  — header and claims, with `batch_id` the one field left out,
  it being the digest's own output — so the signature binds
  every other field of the batch, and through the digest the
  `batch_id` value itself: a batch whose stated `batch_id` does
  not equal the recomputed digest fails verification before the
  signature is even checked. The §10 conformance corpus pins
  these bytes exactly. Signing binds claims to a
  node key — answers cannot be forged in another node's name, and
  §7's reputation has something stable to attach to. A re-served
  batch keeps the origin node's signature and header intact; the
  forwarder wraps it in its own signed *forwarding envelope*
  `{origin_batch_id, origin_node, forwarded_at}` — the envelope
  attests "I am passing this along", transfers no authorship, and
  adds no support (§6 dedupe counts the origin key only).
- **Claim identity / dedupe (local):** `(subject, field, value,
  asserting node key)`. A node's caches dedupe on it; the same node
  re-asserting the same claim is one assertion, however many times
  it arrives (directly or re-served).
- **Retractions are local, and their afterlife is bounded.** A node
  whose library changed simply stops asserting the claim. There is
  no network-wide recall — stated plainly, because pretending
  otherwise would be lying. What bounds a retracted claim's life in
  other nodes' caches is aging measured from the **origin**: every
  cached claim's age runs from its origin batch's signed
  `asserted_at`, capped at the moment of receipt (a future-dated
  batch cannot extend its own life), with a hard maximum lifetime
  on top. Forwarders pass batches along but can never refresh
  their age, so a retraction actually propagates:
  - *Unverified* cached claims expire **90 days after the origin's
    `asserted_at`** and are re-asked for if still needed.
  - *Verified* cached claims (§7) do not expire on a timer, but are
    **re-verified before being relied on again** once their check
    is more than 90 days old, and dropped when that check fails or
    when §7.1's positive-evidence-of-absence test is met. A
    verified claim is therefore never immortal: reliance always
    eventually re-runs the check. (In SoulSync's depot this rule
    runs as a timer on `verified_at` instead, because promotion
    there never calls back to the node — §1.)
  - Batches whose origin key is named by the **issuer-signed**
    revocation list (§10) are dropped when a list naming their
    key reaches the node, forwarding envelopes included — and the
    drop propagates to SoulSync's depot through the node's
    invalidation pushes (§1). The drop is not a one-time sweep:
    §9's admission check refuses a listed key's batches
    **whenever they arrive**, direct or re-served, so a
    forwarder holding an old copy cannot undo the drop by
    re-serving it. The list reaches nodes on every
    token exchange and re-authentication and as a pushed
    `revocations` message (§10), and a node keeps the highest
    list **sequence** it has seen **from each issuer**,
    rejecting older ones — so a stale list served by anyone, a
    third-party relay included, cannot resurrect a revoked key.
    Revocation reaches caches
    and depots, not just future connections (§9).
- **Re-minting.** Batches are not minted once and served forever.
  A responder re-mints its stored batches — fresh `asserted_at`,
  its current attestation (§9) — **whenever its attestation
  renews** (§9's 7-day lifetime), well under the 90-day cache
  lifetime. A batch whose embedded attestation no longer covers
  its `asserted_at` counts zero support under §9's window rule,
  and a batch left un-re-minted ages out of other nodes' caches
  on the schedule above; re-minting is what keeps a live
  library's answers standing-bearing and cached. Re-minting
  changes a batch's id, never its claims' identities:
  provenance in caches and the depot is keyed by claim identity
  (§1, §5), so a re-minted batch is fresh evidence for the same
  claims, and receiving nodes — and, via §1's update pushes,
  the depot — refresh the `asserted_at` they hold for those
  claims rather than treating the fresh batch as a stranger.
  Copies already
  sitting in other caches are not recalled by a re-mint — they
  live and die by this section's aging, and the fresh batch
  supersedes them as it spreads.
- **Free-text hygiene.** Free-text claim values (names, display
  names, genres, labels) are length- and charset-capped at
  minting. Claims are untrusted input all the way into SoulSync's
  web UI, and §13 step 8 escapes every network-sourced string when
  rendering.
- **Sizes.** An answer batch is capped at 5,000 claims (mirroring
  SIN's report caps) and at a byte cap, so a batch always fits a
  single relay frame even before §10's compression; a query's
  total answer window is capped by the asking node's merge budget
  (§10). Exact lookups are small by nature; text-query answers are
  capped per answering node so one broad match cannot flood the
  asker.
- **Legacy IDs:** pre-v2 installs know only legacy SoulIDs, so in
  v1 they effectively **answer nothing** — their exports can't form
  kinded_v2 name claims. They may still ask. The legacy→v2 mapping
  is learned from *upgraded* installs, whose export serves both IDs
  for the same row; a node verifies the mapping by recomputing v2
  from the row's name claim. Legacy subjects are accepted only as
  alias entries, never as v2 subjects. A responder answers an
  `exact` query naming a legacy subject **only** when it holds a
  verified mapping for it in its own alias table (from its own
  upgraded SoulSync, or learned from a name claim it verified);
  otherwise it stays silent on that subject — no guessing, no
  answering from an unverified alias.

## 7. Verification — local, at the node that relies on it

Majority voting is weak at 30 nodes — an attacker with a handful of
keys outvotes the 1–2 honest holders of any niche item — and most of
what matters doesn't need voting at all. There is no central
verifier; verification happens **on the asking node**, which spends
its provider access (its fetchers' credentials are §13 step 6's
open sub-decision) only on answers it is about to rely on.

Two strength tiers:

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
  A strong-verified claim is trusted at N=1 (§9).
- **`album_track` verification:** fetch the tracklist of a
  strong-verified provider release (one call per album — cheap) and
  match each claimed (disc, position) against the response's track
  at that position by normalized title (via the track's name
  claim). A variant is strong-verified when its full tracklist
  matches a fetched release. **Variant equivalence across
  providers:** two provider releases are the same variant when they
  share a MusicBrainz release ID, or a UPC, or a *variant
  fingerprint* — a hash of the ordered (disc, position, normalized
  title) sequence.
- **Plausible only:** name + artist + duration match, but no ISRC
  or MBID cross-match. Live versions, remasters, re-recordings, and
  clean/explicit pairs pass this test while being the *wrong* item,
  so plausible-only claims are treated as unverifiable: their
  weight comes from §9's multi-node support (labels and depot
  ranking), and they are labelled plausible in provenance.

Per provider in v1:

- **MusicBrainz, Deezer, Spotify:** the node's fetchers verify
  against these. MBID kinds are explicit per entity — artist →
  artist MBID, album → **release-group** MBID, track →
  **recording** MBID. SoulSync exposes *release* MBIDs; release →
  release-group resolution is one extra MusicBrainz call, cached.
- **Tidal, iTunes, Discogs, Hydrabase:** claims accepted but held
  as unverifiable in v1 (their weight is §9 support only). Their
  fetchers are later work.

**Fetcher credentials are an open sub-decision** (§13 step 6), not
an assumption: the node has no Spotify or Deezer credentials of its
own. Where SoulSync exposes provider fetch through its local API,
the node's fetchers ride the user's existing SoulSync connection;
where it doesn't, v1 verification is MusicBrainz-only. An optional
future is a **mirror verifier**: a node running a local MusicBrainz
mirror (public dumps and replication packets) verifies MB-anchored
claims with no API rate limit, the API path as fallback — opt-in and
disk-heavy (tens of GB), so most nodes won't run one.

### 7.1 Budgets and terms — now per node, not central

Verification costs land on the asking node, which is the right
place: a node verifies what *it* relies on, at the moment it relies
on it, and caches the verdict with the claim.

- **Etiquette is mandatory:** MusicBrainz ~1 req/s per node,
  per-provider caps, a verified ID is never re-fetched inside
  90 days except under §6's re-verification-on-reliance rule and
  §8's bounded sanity rule. A node that hammers a provider gets
  its own credential banned — its problem, not the network's, and
  the caps keep it from becoming one.
- **Spot-check, don't census.** A node verifies claims it is about
  to display as fact or rank as top depot candidates — not every
  claim that streams past. Display can stream in plausible answers immediately and
  upgrade them as checks land (§10's answer window).
- **Terms:** Spotify's and Deezer's developer terms restrict
  storing and redistributing their catalogue data. The claims model
  keeps the safest possible posture. The licensing facts, stated
  precisely: MusicBrainz **core** data is CC0; MusicBrainz **tags**
  are CC BY-NC-SA supplementary data — and most genre values derive
  from tags, so genres are *not* CC0. Re-servability is a
  **computed, positive rule**, never a flag on the batch: a batch
  is re-servable **iff every claim in it is on this allowlist** —
  (i) claims sourced from **MusicBrainz core** data; (ii) bare
  **provider ID** claims (facts: "SoulID X is Deezer track Y");
  (iii) **`provider_link`** claims (§6), the same kind of bare
  link; (iv) the entity's **name claim** (§6), which rides in both
  of an answer's batches so each is independently verifiable.
  Consequences of the rule, spelled out: `album_track` tracklists
  travel **only when MusicBrainz-sourced** (a provider's tracklist
  is its catalogue data); durations, ISRCs, years, genres, and
  display names likewise travel only when MusicBrainz-sourced;
  `network`-sourced claims (§2.4) are never re-servable. The
  receiver computes all of this from the claims present — a
  forwarder's say-so is worth nothing. Whether the held-back
  fields may *also* travel from other sources (contributor-sourced
  durations, ISRCs, years, genres, and provider display names
  usually *originated* in provider data upstream of the
  contributor's files — "no provider owns them" is an assertion,
  not a fact) is an explicit terms-check item for the §13 step-0
  human terms read; until a field clears, it is answered from a
  node's own library but **not re-served** from cache unless
  MusicBrainz-sourced. Year values come only from
  MusicBrainz-sourced claims until the same check clears (§8).
- **Absence needs positive evidence.** A provider 404 from the
  node's region is *unreachable*, not *deleted* (geo-restriction
  is real). A cached verified claim is dropped only on positive
  evidence: a not-found from a second lookup path, or MusicBrainz
  showing the entity merged/removed.

## 8. The merge — one library, run on every node

Merging is a pure, versioned library (`merge_rules_version`),
written **once in Python** and run by every node over the claims it
holds: its own library's claims, answers received live, and its
answer cache. One input shape only — *claims → a merged view* — and
the library includes the SoulID normalizer. SoulSync's `kinded_v2`
normalizer is Python; the node copy is held byte-identical by
**golden test vectors** generated from SoulSync's implementation
(name tuples → SoulIDs, including the script-aware mark-stripping
cases). A vector failure blocks the node from answering or merging
until fixed — a wrong normalizer mints wrong IDs everywhere.

Field rules:

| Field | Rule |
|---|---|
| Provider IDs | A **set** per provider. Same-name artists share an artist SoulID (artist = hash(name)); two bands called Nirvana have two true Spotify artist IDs. Never pick a winner for artists/albums. |
| MBIDs | Kind-explicit (§7). One release-group/recording/artist MBID per entity is expected; multiples are kept and flagged, as with provider IDs. |
| Tracklist | Variants are legitimate (deluxe vs standard share an album SoulID, since album = hash(artist, title)). Variants are keyed by the `album_track` claims' `variant` (verified provider release ID) or, failing that, by (disc count, track count). Never collapse variants. |
| ISRC | Set-valued. Strong-verified when it matches a verified provider ID's report. |
| Duration | Track SoulIDs ignore album (track = hash(artist, title)), so durations merge **per album_track context**, not per bare track: median of verified reports within a variant; outliers beyond ±2s of the median are dropped. A bare-track duration is used only when all contexts agree within ±2s. |
| Year | MusicBrainz release-group year (from an MB-sourced claim) or **absent**. Provider years stay local-only until the §7.1 terms check clears re-serving them. Edition years are a separate field and never feed this one. |
| Genres | Not a conflict. Union them; display rank order and §9 coarse support, never exact per-genre counts. Disputed genres are marked never promoted on agreement alone. |
| Anything unverifiable and disputed | Show all values **with** the disagreement visible, marked display/depot only. |
| Everything | The merged view never overwrites local data. Promotion (§14.1) fills empty fields only. |

**Multi-valued promotion rule:** provider IDs are sets at *every*
kind, tracks included — a track SoulID (hash of artist + title) is
shared by the live version, the remaster, and the single edit, and
each has its own true Spotify ID. When the merged view holds a set
for a field SoulSync stores singly, a SoulSync workflow may promote
a candidate only if exactly one survives disambiguation against
the user's local data (§14.1):

- **Tracks:** disambiguate by `album_track` context — the user's
  album, its variant (§7 fingerprint), and the track's position in
  it. The candidate is the ID the verified release's tracklist
  carries at that position. No context, no promotion.
- **Artists/albums:** disambiguate by the `provider_link` relations
  (§6): follow the user's local albums' provider IDs to the artist
  ID those albums link to on the provider's side. A **fetched**
  provider link settles it with **one** album: if one of the user's
  local albums carries a provider ID and fetching that album
  returns its artist ID, the candidate is settled — one call,
  already budgeted in §7. The counting rule applies only to
  *unverified* links: a candidate survives if **≥3** of the user's
  local albums for that artist name link to it, and no other
  candidate reaches 3. (Album SoulIDs alone cannot do this — both
  same-name artists' albums hash under the same name.)
  Survival by counting settles the candidate for **display
  only** — nothing more. Counting is cheap to fake (the links
  may be `provider_link` claims from a single key with no
  standing), so counting never promotes anything by itself.
  Promotion needs the floor below: leg (a), leg (b), or an
  anchored leg (c).
- A shared strong-verified MBID settles any kind immediately.

If zero or ≥2 candidates survive, the field stays display/depot
only. This rule, not a vote, resolves the Nirvana problem at the
point of use.

**Promotion floor — design-level, not per-workflow.** The rule
above disambiguates *which* candidate may be promoted; this floor
sets *whether* promotion may happen at all, for every workflow
(§14.1). A depot value may be promoted into a library only when
at least one of these holds: **(a)** it is strong-verified tier
(§7, §9) — a tier this node earned with its own check, which is
also the only way a `network`-sourced value (§2.4) ever
promotes; **(b)** the workflow spends a **local provider
confirmation fetch** on it — one exact lookup by the candidate's
own ID, answered by the provider — and the response passes an
**independent key check**, at the same strength as §7's strong
tier: the response must be checked against a **key**, not
against a name. The keys that count are the file's **ISRC**,
an **MBID**, or **album + position** checked against a local
album/track ID. Duration only ever *supports* such a check
(§7); it never carries one — and **name agreement is never
the independent check**: a remaster, a re-recording, and a
clean/explicit twin share a name and near enough a duration,
so a fetch that agrees with the local name and duration has
passed exactly the test this floor exists to refuse. A
**second provider's** response serves as the independent side
only when it supplies the same kind of key — its ISRC or MBID
agreeing with the first response's — never when it merely
agrees on name or duration; §7's rule, that both sides of a
cross-match come from provider responses and never from the
claim, applies here unchanged, and agreement with the
candidate claim itself counts for nothing, the claimant
controlling both sides of that comparison. "The user's own
local data" in this leg means data that is **not
`network`-marked** (§2.4): an earlier promotion never vouches
for a later one. For **artists and albums**, which carry no
ISRC or position of their own, this leg is satisfiable **only
through a fetched link from a local album or track ID** — leg
(c)'s verified-anchor mechanism: fetch one of the user's own
albums by its local provider ID and let the provider's own
link name its artist. A lone claim equating an artist name
with an artist ID cannot pass here on the name alone; or
**(c)** it is the **single
survivor** of the disambiguation rule above, picked by a
**verified anchor** — a verified release tracklist (§7), a
fetched provider link (the one-album rule above), or a shared
strong-verified MBID — with no conflicting candidate. A candidate
set that happens to hold one value is not a survivor: a lone
claim with zero support promotes nothing on this leg — and a
`network`-sourced value never promotes on this leg at all,
lone or otherwise: for `network` values, leg (a) is the only
route (§2.4). Survival by the unverified counting
rule alone is display-only (above). Name + duration
agreement alone **never** promotes: it is exactly the weak test
§7 warns passes for the wrong item (live versions, remasters,
clean/explicit pairs), and a depot hit does not become exempt
from it by having arrived from the network. Per-workflow policy
(§14.1) tunes only **above** this floor — preference order,
eagerness, which fields a workflow bothers with — never below it.

**Interim state (SoulSync side, Oct 28, 2026):** leg (c)'s slot
anchor is switched **off for provider-ID fields** until the
per-slot provider ID is recorded at verification time and the
anchor checks the candidate against the ID the verified
tracklist carries at that slot — this section's original intent
(the track rule above: "the candidate is the ID the verified
release's tracklist carries at that position"). As the depot's
code stands, the anchor checks the slot, not the ID the slot
carries: it proves which track this is and says nothing about
whether the provider ID attached to it is right, and on a niche
item a single answering node is automatically the single
survivor. Provider IDs promote via legs (a) and (b) meanwhile.

**Provenance:** every merged field carries value, support
(strong-verified by this node / plausible / coarse node bucket §9),
and `merge_rules_version`. Asserting node keys are protocol-visible
to the receiving node's *software* — signatures, dedupe (§6), and
reputation (§9) are impossible without them — but they are never
shown to the user and never passed to SoulSync: the user and
SoulSync see coarse support buckets only. That binds **every**
user surface, the node's own pages included: exact support
counts and asserting node keys are deliberately never exposed
to a user anywhere (Oct 28, 2026). SoulSync stores that
provenance **in the depot** with the merged record, and when a
workflow promotes a value (§14.1) the provenance travels with it
into the library record — including the mark §2.4 requires, so a
promoted value is never mistaken for the user's own data.
Promoted network values never become "user data": a later answer
can update or retract them **in the depot** (§1), and §2.1's
"local wins" applies to the user's own data only. Retraction
stops at the depot's edge, honestly: once a value sits in a file's
tags, the network does not rewrite the file. Invalidation flags
the entry and ends future promotions; correcting written tags is
SoulSync's own workflows' business, on their own schedule.

**Sanity rule (replaces the old compile canary):** there is no
compiler, so there is no canary — but a node never lets fresh
claims silently change a field it previously strong-verified. A
conflicting answer triggers re-verification of the stored value
first; the verified value stands until the check says otherwise.
Because any peer can *send* a conflicting answer, this trigger is
bounded, or it becomes a way to spend the asker's provider budget
and credentials from across the network: conflict-triggered
re-verification is **rate-limited per origin** (at most one
triggered re-check per origin per field per 24 hours) and
**gated by local reputation** — an origin with no verified
history on this node cannot trigger re-fetches at all. Its
conflict is recorded, the verified value stands, and no call is
spent.

## 9. Local trust rules — what a node shows and stores

There is no publication and no global corpus, so there are no
publication gates. Each node decides, locally and visibly:

- **Strong-verified (§7) claims are trusted at N=1** — one node's
  claim, once this node has cross-checked it against providers, is
  displayable as fact and is the depot's strongest candidate.
- **Everything unverifiable needs support: ≥3 distinct asserting
  node keys** (dedupe per §6 — re-served copies of one node's claim
  count once) before it earns the `3+ nodes` label and top depot
  ranking. Below that it carries its coarse support label
  (`1–2 nodes`) and stays a lower depot candidate. Exact counts
  are shown only as those buckets — a precise count next to a
  sparse item is a fingerprint of how many libraries hold it, and
  the buckets are enough to judge by. Claims with source
  `network` (§2.4) are not counted at all — **zero support**,
  however many keys assert them and whatever their standing; they
  are the network looking at its own reflection. Zero support
  does not bar promotion, though: a `network` claim this node
  **strong-verifies itself** (§7) is trusted at N=1 like any
  other self-checked claim and is promotable under §8's floor
  (a) on the strength of that check alone (§2.4).
- **Peer agreement orders display and depot candidacy. It never,
  by itself, makes data library truth.** Promotion into a library
  happens only inside SoulSync's own workflows, and only above
  §8's promotion floor (§14.1). This is
  the load-bearing sentence of the trust model: voting decides
  what is *shown* and *stored as a candidate*, never what is
  *believed into a library*.
- **Support only counts keys with standing — and standing is
  attested, not inferred.** Portal accounts are free to create, so
  "3 keys" alone would cost an attacker three signups; §7's
  warning would eat §9 alive. Standing therefore rests on the one
  input an attacker cannot fabricate locally: the **attested
  binding age** of the asserting key, carried inside the batch
  itself.
  - **Issuance.** The pool signs a short-lived **binding
    attestation** `{issuer, node_key, bound_at, issued_at,
    expires}` for each bound key. Two of its times do two
    different jobs, and the shape keeps them apart on purpose:
    the **lifetime is 7 days from issuance** — the validity
    window is `issued_at`..`expires`, how long this one
    attestation stays embeddable — while `bound_at` is the
    moment the key was bound, fixed across renewals, and used
    **only** by the standing rule below. (An earlier draft ran
    the window from `bound_at`; that made standing unreachable,
    since a 7-day window starting at the binding never contains
    an assertion made 30 days after it.) Standing cannot be
    forward-dated because neither of its inputs is the asserting
    node's to choose: `bound_at` is the issuer's signed
    statement, and `asserted_at` is capped by §6's receipt rule.
    The `issuer` field names the signing pool — the same issuer
    id the revocation lists carry (§10) — inside the signature,
    so an asker holding several issuers' lists can pick the one
    that governs this attestation. It is
    issued and renewed at the **token exchange** (§10):
    the exchange response carries the node's current attestation
    alongside its token, and a node fetches a fresh one before
    its current one expires. No live lookup at answer time, no
    account data in it; askers verify it **offline** against the
    pool key.
  - **Carriage.** The origin embeds its current attestation in
    every batch it signs (§6 header), so the attestation travels
    with the batch for the batch's whole life — through the
    answer cache and through re-serving, whose forwarding
    envelope (§6) needs no attestation field of its own. The
    origin is the only party that can renew it, and embedding is
    what keeps that from stranding re-served batches — and the
    origin re-mints its batches when it renews (§6), so what it
    serves carries a live attestation rather than a fossil.
  - **Validation.** An asker checks four things: the
    attestation's pool signature verifies; its `node_key` equals
    the batch's `header.node`; the batch's `asserted_at`
    falls **inside the attestation's validity window**
    (`issued_at`..`expires`) — that the batch was minted while
    this attestation was live; and the batch's origin key —
    `header.node` — is **not named on the highest-sequence
    revocation list the asker holds for the attestation's
    issuer** (§10). The revocation check runs at **admission**,
    on every batch, direct or re-served — it is not only a
    sweep run when a list arrives — so a forwarder re-serving
    an old copy of a revoked key's batch cannot resurrect it:
    the copy is refused whenever it shows up, however valid its
    window still looks. At admission the attestation itself is
    not yet verified — verification is the first of the four
    checks above, run where standing is judged — so the issuer
    its header names cannot select the governing list there: a
    revoked origin would name an issuer nobody holds a list for
    and walk straight past the check. Admission and the
    re-serve filter therefore use the union over every list the
    node holds — in v1, with one configured issuer, that is the
    issuer's verdict — and issuer-scoped admission returns only
    once admission verifies the attestation against the
    configured issuer key. The same check gates the depot at
    promotion time, enforced through §5's invalidation pushes
    and the reconnect reconcile: a depot field whose origin key
    is listed is dead for promotion, not merely dropped from
    the cache. The check is *not* that the
    attestation is unexpired when the asker looks: a batch
    minted inside its window keeps the standing it earned for
    its whole cache life (§6 aging), and **revocation** (§10) is
    the kill path for batches whose window later turns out to
    belong to a revoked key. A batch carrying no attestation, or
    one whose window does not cover its `asserted_at`, is
    displayed like any other answer but counts **zero support**.
  - **Standing is one number.** An asserting key has standing
    when its attested binding age at assertion —
    `asserted_at − bound_at` — is at least **30 days**. Both
    inputs are beyond the asserting node's power to fake:
    `bound_at` is the issuer's, and `asserted_at` is capped by
    §6. There is no assertion-history requirement and no
    per-claim-class split — an earlier draft required both, and
    both failed on contact: assertion history is either
    backdatable (if measured from `asserted_at`) or a 30-day
    cold start for every fresh *asking* node (if measured from
    first sight), and per-class standing in the unverifiable
    class rests on outcomes that class cannot observe. Under
    this rule a fresh asking node counts support correctly from
    its first day, because standing is a property of the pool's
    signature, not of the asker's own memory. Whether a key's
    answers *hold up* is reputation's job (below) — local,
    per-node, and never a gate on support.
  - **Unbinding is revocation.** Unbinding a key at the portal, or
    deleting the account, adds the key to the revocation list
    (§10) — expiry alone is not the kill path for that case. A
    key cut loose by its owner does not get to spend its last
    attestation window minting batches that still carry standing:
    the list, delivered on every exchange and pushed live (§10),
    is what ends it.
  - The relay also **stamps** each direct answer with the
    authenticated sender's key (§10). The stamp's job is narrow
    and is stated so nobody mistakes it for more: it binds the
    answer to the connection for the relay's own byte accounting,
    and lets an asker pre-filter a batch whose `header.node`
    differs from the stamp *before* paying for signature checks.
    Support never rests on the stamp — **every** batch counted
    for support, direct or re-served, needs its embedded
    attestation (above). Authorship is the signature's job;
    standing is the attestation's.
  - There is **no fast route** to standing. The earlier draft let
    keys earn it by passing §7 checks; that is dropped — it is
    reputation wearing a gate's clothes, and checkable claims are
    cheap to farm from public data. Passing §7 checks still builds
    *reputation* (below), which never gates support.
  - This does not make Sybil attacks impossible — three aged,
    well-behaved sockpuppets still pass — it raises the price from
    minutes to months of good behavior. The operator's power to
    revoke portal keys is the backstop, and revocation now reaches
    caches, depots, and live sessions, not just future connections
    (§10).
- **Reputation (§5) never gates support; it spends the node's own
  scarce resources.** Concretely, a node's reputation for a key decides:
  (a) which answers get §7 verification first within the answer
  window's budget, (b) cache eviction order under size pressure
  (low-reputation claims evict first), and (c) display order among
  *competing unverifiable values* (presentation only — §8's merge
  itself is deterministic and reputation-free). A node whose
  answers repeatedly fail §7 checks sinks on all three — reputation
  cannot make one node an authority, only make it cheaper to
  ignore.
- **Node modes** (the answering opt-in, §2.7), set per node:
  - *Full:* answer from own library + re-serve cache (§7.1's
    re-serving limits apply).
  - *Own library only:* answer, never re-serve.
  - *Leech:* never answer; asking still works.
  Default for a node that enables answering: **own library only**,
  until the user turns re-serving on — re-serving spreads data
  beyond its origin node's session, and that choice belongs to the
  user, node by node. (The network-wide recommendation is on: it is
  what makes a small network answer like a big one. It is still the
  user's switch.)
- **Asking needs no approval from anyone.** Any node with a bound
  SIN key may connect and ask. There is no contributor gate because
  there is no server-side corpus to protect; abuse is handled by
  relay budgets (§10) and by every node's local trust rules above.

## 10. The relay and the query protocol

**The relay is a dumb switchboard, and that is a specification, not
an aspiration.** It:

- accepts persistent outbound connections (WebSocket) from nodes;
- authenticates each connection (see Identity, below) and keeps the list of
  connected node keys **in memory only** — never written to disk or
  database; a restart empties it and nodes reconnect;
- forwards queries to connected nodes and forwards their answers
  back to the asker, matching them by query ID;
- enforces per-node budgets — **byte-based**: answer bytes per node
  per query and per hour, plus a query count per hour — so one node
  cannot scrape or flood the network. Violations **decay** over
  time, and answers that arrive late are **dropped, never
  charged**: honest slowness is not an abuse signal;
- **counts, never content:** it keeps aggregate counters (queries
  routed, share answered, answers per query, connections) so the
  network's usage is measurable, and it logs no query or answer
  content, ever — operational logs are those counters and error
  classes only;
- carries the **issuer-signed revocation list** (key status only
  — revoked, unbound, and deleted keys, §9; signed by the
  portal/pool key — the relay transports it and cannot forge or
  edit it) in its `welcome` message and as a pushed
  **`revocations`** message on live connections whenever its
  copy changes — a list delivered only at connect would never
  reach the long-lived sessions in-band re-authentication
  exists to keep alive. Delivery is the **issuer's**, not the
  relay's: the exchange is the issuer's own endpoint (Identity,
  below), and its response carries the current list directly to
  the node; every in-band re-authentication is preceded by such
  a fresh exchange, which is the delivery moment — the
  re-authentication itself only shows the new token to the
  relay. That split bounds what a withholding relay can do: it
  can sit on its `welcome` copy and its pushed messages, but
  the node's next exchange hands it the issuer's current list
  over a channel the relay does not sit on — so a withholding
  relay delays a revocation's arrival by **at most one token
  lifetime** (24 hours; Identity, below). That bound covers
  lists from the node's **own** issuer, whose exchanges the node
  makes directly. A list from another trusted issuer (§11)
  reaches the node only via relays, and a relay can hold it back
  indefinitely; admission (§9) therefore cannot let the issuer
  a batch claims select its governing list — the claim is
  unverified at receipt — and checks the union over held lists
  instead, so a key named on no held list is unrevoked for now
  — v1 runs one official pool with one issuer, and a staleness
  bound for foreign lists is a named precondition for
  federation (§11), not a v1 mechanism.
  The
  list carries a **monotonic sequence number** and `issued_at`;
  nodes keep the highest sequence they have seen from each
  issuer and reject
  anything older, so a relay — including one run by another
  party (§11) — can withhold a newer list but cannot roll one
  back. The high-water mark is **durable node state**, written
  down across restarts: an in-memory-only mark would hand a
  withholding relay the rollback the moment the node restarts
  or its network is toggled off and on. Token expiry forces in-band re-authentication, which a
  revoked key fails — so a revocation reaches live sessions and,
  via §6's drops and §1's pushes, caches and depots;
- needs **no database access**: the guest-list check happens at
  the token exchange (see Identity), and the relay verifies tokens
  offline (attestations are askers' business, §9);
- stores no metadata of any kind, in any table, at any time.

**Hosting:** the relay must stay awake — it holds connections open —
so it cannot be a Neon function (those sleep between requests). It
is a small always-on program on the **operator's server
infrastructure** (the same family as the SIN backend — never on
users' machines, which §3 establishes cannot accept inbound
connections; the host vendor is the operator's choice, §14.3). The
relay needs no schema of its own and no database connection at all.

**Identity:** a node connects with the SIN exchange (§3A.2 rails:
pool key + Ed25519 signature → a token with a **24-hour
lifetime** — the SIN exchange token's lifetime — scoped
`meta:read` for asking; answering runs over the same
connection). The exchange
token's claims are exactly `{key_id, node_id, scopes, iat, exp}` —
no account anywhere in it. The 24 hours is a security bound,
not a nicety: it caps how long a withholding relay can delay a
revocation (§10, above) and how long a session can outlive its
token before re-authentication. **The guest-list check lives at the
exchange**: the exchange knows the binding and issues `meta:read`
tokens only for bound, unrevoked keys, so the relay never peeks at
`portal_keys` — it verifies the token's signature offline. The
exchange response also carries the node's current **binding
attestation** (§9), which nodes embed in their batches and
*asking* nodes verify; the relay never handles attestations. The
exchange response also carries the current **revocation list**
(§9, Messages below) — sequenced, issuer-signed — so every
exchange and every re-authentication refreshes the node's copy,
not just its first connect. The
relay therefore holds no
path from a node key to an account: that join exists only in the
portal's account database, which the relay never touches (§12 Q2).
At token expiry the connection **re-authenticates in band** — a
fresh exchange token presented over the same connection — where
the client supports it, so live sessions survive their tokens; a
revoked key fails that re-authentication, so its session ends at
its token's `exp` at the latest. The re-authentication's
signature binds the relay's identity (like the hello's), so a
re-auth captured at one relay cannot be replayed at another to
keep a hijacked session alive, and the relay rate-limits
re-authentication per connection: a healthy client re-auths
about once per token lifetime, and a client spinning on a cached
token (re-sending the token the relay already acknowledged)
is refused and struck instead of costing the relay a signature
verification per round trip. Where in-band re-authentication
is not supported, the fallback is disconnect and reconnect, and
the cost is stated plainly: queries in flight on a connection
that closes **die with it**. Nothing is corrupted by that —
answers are idempotent claims — and the asker simply re-asks.

**Messages** (JSON over the connection; a binary framing is a later
optimization, not a v1 need):

- `hello` — node key, protocol version **range**, node mode (§9).
  Before it, the relay issues a per-connection **challenge** (a
  random nonce, single-use); the hello's node signature binds that
  nonce, **the relay's identity, and the node's declared mode** —
  so a captured hello cannot be replayed on a fresh connection, to
  a different relay, or under a different mode (the federation
  impersonation hole, §11). The relay's identity, for this
  binding, is its **configured canonical origin**
  (`RELAY_ORIGIN`) — the `wss` scheme, host, and port nodes dial
  it at, lowercased, no path; the node computes the identical
  string from the origin it dialed, so the two sides agree with
  no relay key to distribute. A relay with **no configured
  origin refuses v2 hellos**: the fallback of deriving the
  identity from the request's `Host` / `X-Forwarded-Proto`
  headers would compute it from values the connecting party
  itself supplies, and a malicious relay could pass another
  relay's challenge to a node, collect the hello signed for the
  victim's identity as derived from its own headers, and replay
  it at the victim to impersonate the node — the binding would
  protect nothing. Failing closed costs a misconfigured relay
  its v2 service (v1 is unaffected) and nothing else. The relay
  replies `welcome` with the current peer count (coarse: exact
  under 100, rounded above — the count is a presence hint, not a
  census) and the issuer-signed **revocation list** (§9), or
  `refused` with a reason class.
- `query` — `{query_id, kind, mode, reply_key, …}` where
  `reply_key` is an **ephemeral public key minted per query** (see
  Sealing, below) and mode is:
  - `exact` — one or more SoulIDs (or legacy aliases of them): "who
    holds claims about these subjects?"
  - `text` — `{kind, terms}`: "who holds claims about an artist /
    album / track matching this?" Answered from a node's own name
    index over its claims stores (own library + cache), never by
    scanning raw exports per query.
- `answer` — `{query_id, batch}` where batch is a §6 signed claim
  batch, or a §6 forwarding envelope for re-served claims. Answers
  stream: a node sends when it has something, not when it has
  everything. One answer message may **pack several batches** —
  answers are not one message per batch (the per-message budget
  accounting that punished big libraries, §13 step 5, is gone with
  byte-based budgets).
- `done` — `{query_id}`: an answering node signals it has no more.
  A node's first answer does **not** close its stream — only its
  `done` (or the window) does.
- `complete` — `{query_id}`, sent by the **relay**: the answer
  window is over. The relay knows the set it routed the query to;
  the asker does not (the `welcome` peer count is coarse, and
  leech-mode nodes are never routed queries), so the asker never
  counts `done`s itself. The relay sends `complete` when every
  node it routed to has sent `done`, or after ~6 seconds,
  whichever is first. After `complete`, a short **grace period**
  admits answers already in flight — they still land in the
  asker's cache, enriching the next query even when they miss this
  one's display — and then the query is **closed** (the one
  definition, used by the admission rules below): answers for a
  closed query are dropped, silently. The relay states its grace
  in the `welcome`, and the asker holds the query open for that
  same grace after `complete`: the two ends must agree, or the
  relay goes on admitting (and metering) answers the asker has
  already stopped collecting. A purged query's id is remembered
  for a while after the purge, so an answer that arrives after
  it is a silent late drop — never a strike: honest slowness is
  not an abuse signal.
- `revocations` — `{issuer, seq, issued_at, keys, sig}`: the
  issuer-signed revocation list (§9) — `sig` is the issuer's
  signature over the canonical message, verified before the
  list is applied; an unsigned or badly signed list is no list
  at all. The relay pushes it on live connections
  whenever its copy changes; it is also carried in `welcome` and
  in every token-exchange response — the fresh exchange that
  precedes each re-authentication (the issuer's own delivery,
  above). Sequence numbers are **per
  issuer**: a node tracks the highest `seq` it has seen **from
  each issuer** and applies a list only when its `seq` exceeds
  that issuer's highest. An issuer **never reuses a sequence
  number** — not after a restart, not after a backup restore:
  the sequence counter is part of the issuer's durable state,
  restored with its backups and never rewound, because a
  rewound counter would let an old list outrank the current one
  at every node that kept its high-water mark. A node
  drops the named keys' batches from its cache and,
  via §1's pushes, from SoulSync's depot.
- `bye` / heartbeat — liveness only.

**Sealing and compression.** Answer payloads are **sealed** to the
query's `reply_key`: the `reply_key` is an ephemeral **X25519**
public key minted per query, and sealing is **HPKE (RFC 9180)
base mode** with the full suite named — **KEM 0x0020**
(DHKEM(X25519, HKDF-SHA256)), **KDF 0x0001** (HKDF-SHA256),
**AEAD 0x0003** (ChaCha20-Poly1305) —
so the KDF and nonce derivation are the standard's, not an
ad-hoc sealed-box construction a second implementer would have
to guess at (or get subtly wrong: libsodium's sealed box, the
obvious off-the-shelf reading, is XSalsa20, not ChaCha20). The
HPKE **`info` string is the query's `query_id`**: a sealed
answer is cryptographically bound to the query it answers, so
a ciphertext replayed under a different query does not open.
Answering nodes encrypt their batches to that key and the relay
forwards ciphertext; it still sees the asker↔answerer pairing,
timing, and sizes, but never answer content (§12). A query
carrying a reply key is answered **sealed or not at all** — no
hop may substitute or accept plaintext for it: an answerer that
cannot seal refuses (its `done` still lands), the relay never
forwards a plaintext answer for a sealed query, and the asker
drops any that somehow arrives. The exact key
encodings and HPKE byte format are pinned in the §10 conformance
corpus, so a second implementation seals and opens
byte-identically. (Queries are not sealed: broadcast means every
answering node must be able to read them.) Inside the seal, batches
are **compressed with zstd and a shared protocol dictionary** —
the dictionary is versioned with the protocol and negotiated in
`hello`, and a hard decompressed-size cap bounds every batch.
Compression applies **after** signing: signatures always cover the
canonical uncompressed bytes, so compression can never blur what a
signature attests.

**Query IDs and answer admission (the protocol's front door):**
`query_id` is 128 bits of randomness minted by the *asking* node,
scoped to its connection. The relay forwards a query only to nodes
connected at that moment and routes answers only for query IDs it
actually routed, only back to the connection that asked, and only
inside the window + grace period. An asking node admits answers
**only for query IDs it minted and has not closed** — *closed*
means `complete` plus the grace period (above), the one
definition — and anything arriving for any other query ID is
dropped on arrival, **silently**. No charging runs from asker to
relay: an asker has no message that could charge a sender, and
giving it one would let askers burn answerers' budgets. The only
budget accounting is the relay's own (§10 budgets), on the
messages it carries. The relay **stamps** each direct answer with
the authenticated sender's node key — for its own byte accounting,
and so an asker can pre-filter a batch whose `header.node`
differs from the stamp before paying for signature checks (§9).
Support is judged by the batch's embedded attestation (§9),
direct and re-served alike, never by the stamp. Total admitted answer bytes per query are
capped — per node (§10 budgets) and in total; past the cap,
answers are dropped (cache included) until the query closes, and
hitting the total cap is a silent drop, not a charge against
whoever arrived last. One residual, stated plainly: answering
needs no standing (§9), so a set of cheap keys can crowd the
total cap inside the window and crowd out honest answers;
per-node byte budgets bound how much of the cap any one key can
eat, and at v1 scale that residual is accepted, not solved.
A replayed batch under a fresh query is not an attack by itself —
claims are claims — but it buys the replayer nothing: dedupe (§6)
collapses it, support counts asserting *origin* keys only, and a
forwarding envelope adds no support. Signed junk aimed at poisoning
a cache is bounded by the byte cap, by admission being
query-scoped, and by §7/§9: nothing enters a *view* as fact on the
strength of arriving.

**Merging while answers stream:** the asker's view updates as
answers arrive (SoulSync shows the result filling in, the way the
§1 experience describes), with provenance (§8) marking support as
it stands. Set expectations plainly: at §7.1's provider etiquette,
almost nothing inside the 6-second window is strong-verified yet —
verification is calls, and calls take seconds each. Streamed
results are presented as network answers with their support
labels; per-field strong-verified marks land as §7 checks complete,
during and after the window. SoulSync treats a field as verified
only when its mark has landed, never because the window closed.

**Forward compatibility.** Nodes **ignore unknown fields and
unknown claim shapes** — a batch is never rejected for containing
them — and `hello` carries a protocol version range, so
mixed-version networks keep working. A **SoulID migration
playbook** is written down before it is needed: a dual-ID period
(both ID spaces served), alias claims linking old and new subjects,
and a sunset rule for the old space; the legacy→v2 transition (§6)
is its template. A byte-exact **conformance corpus** — normalizer
vectors, JCS batch bytes, signatures, forwarding envelopes, merge
outputs — is a named deliverable (§13 step 1): any second
implementation must pass it byte for byte.

## 11. Federation and the road beyond one relay

- **Pools are relays.** SIN already has pools; a metadata relay is
  run per pool (v1: the official pool's relay). A node may connect
  to several relays at once; queries fan out to all of them.
  **Support is never summed across relays** — the same node key on
  two relays is still one assertion, and dedupe (§6) is by node
  key, so cross-relay duplication collapses naturally.
- **Relays may be run as separate parties.** A pool's relay need
  not be run by the portal operator: with attestations (§9)
  verified offline, a relay needs no account access to do its job.
  Nodes configure **which attestation issuers they trust, per
  pool**. Separation is the structural version of §12's honesty —
  joining a node key to an account requires the portal's database,
  and a relay run by someone else never holds it.
- **Independent relays are independent trust domains for presence
  and connection accounting.** There is no corpus to disagree about —
  every node merges for itself — so federation here is trivial
  compared to the old snapshot design. A relay stamps the direct
  answers it carries (§10), but the stamp is accounting and a
  pre-filter only (§9): support rests on **attestations** and
  signatures, which are pool- and node-signed and no relay can
  forge, so a relay's trustworthiness never enters what an asker
  believes. A relay *can* drop answers
  — censorship by silence, which no routing layer can prevent —
  but it cannot assert data: sealed, signed batches it didn't
  author fail verification at the asker. (An earlier draft claimed
  "a relay cannot assert data, only carry it" as if that were the
  whole story; the rest of it is that a relay is a trust domain
  for presence and accounting, never for data.)
- **A DHT remains a possible later swap for the relay's *discovery*
  job** (who is online), if a relay ever becomes a bottleneck or an
  unwanted center. The query/answer/claim formats do not change —
  only routing does. Nothing in v1 is built on the assumption that
  a relay exists forever; nothing in v1 pays the DHT's costs
  either (§3).

## 12. Privacy

SIN's published privacy rules (PORTAL-DESIGN §4, P1–P7) are SIN's
and SIN keeps them — this service does not weaken them, because
this service barely touches the server at all.

**Named adversaries.** The rules below are written against four,
not against a generic "attacker": a **curious operator** (reads
what the system lets them read), a **compelled operator** (ordered
to start logging going forward), a **malicious peer** (asks and
answers in order to learn or to poison), and a **rights-holder
running nodes** (a peer with a legal department).

**Metadata privacy rules (Q-rules):**

- **Q1.** The server stores **no metadata**: no claims, no queries,
  no answers, no holdings, no corpus — nothing to hand over, leak,
  or lose. The only metadata-shaped thing on the server is the
  relay's in-memory presence list, which is never persisted. What
  the relay *sees in flight* is Q2's subject, and it is not
  nothing; this rule is about storage, and it does not pretend to
  cover sight.
- **Q2.** The relay transiently *sees* every query's content —
  broadcast means answering nodes must be able to read a query, so
  a query cannot be sealed from the network it is asked of — and
  the asker↔answerer pairing for each query, while it is in
  flight. Answer *content* it does not see: answers are sealed to
  the asker's ephemeral key (§10) and pass through as ciphertext.
  The service rule for what the relay does see is **routes and
  forgets**: no content logging, no retention. Policy binds
  retention, not visibility — a compelled operator could begin
  logging queries going forward, and this design does not pretend
  otherwise. That is why the structural separations exist: the
  relay holds no account access (§10), so joining a node key to an
  account requires the portal's separate database; a compelled
  relay yields pseudonymous keys, questions, and pairings, not
  names — and a relay run as a separate party (§11) doesn't hold
  even that join's other half. The opt-in copy says this, in plain
  words, before the node connects to a relay for the first time.
- **Q3.** Answering reveals holding — to the asker (as a node key,
  not an account or an IP) and to the relay (as a pairing). Budgets
  (§10) slow enumeration; they do not make a stable key's library
  unprofileable — over time, a determined asker can build a good
  picture of what a long-lived answering key holds, and the opt-in
  copy says exactly that: *your library is profileable under a
  stable pseudonymous ID*. Node modes (§9) let a user answer less
  or never. Re-serving (§5) blurs the signal — a re-served answer
  means "learned", not "held". There is no setting that makes
  answering anonymous, and the copy does not pretend there is.
- **Q4.** SIN's tables are off-limits to metadata code and vice
  versa. The single shared artifact is the portal account + bound
  node key, used by the token exchange as its guest-list check
  (§10); the relay reads none of them.

| Who | Learns what |
|---|---|
| Asking node | The merged answers to its query. Its *software* sees asserting node keys (pseudonymous — required to verify signatures); its *user* sees coarse support buckets only (§8). |
| Answering nodes | The query (kind + terms/subjects) — and nothing about who asked. Queries carry no asker key, account, or IP; the only identity an answerer sees is the ephemeral reply key (§10), useless once the query closes. |
| The relay | Which node keys are connected, their IP addresses, and connect/disconnect timing — it cannot route without seeing these. In flight, per query: the query's content and the asker↔answerer pairing — i.e. who is asking about what, and who answered, network-wide, in real time. Answer content: ciphertext only (§10). All of it never logged or stored (Q2) — but *seen*, and §3's "the relay hides IPs" means hidden from other nodes, never from the relay itself. Accounts: none — the relay has no account access; the key→account join lives only in the portal's database (§10, §11). |
| The operator's database | Nothing new. Accounts and keys it already held for SIN. |
| Non-participants | Nothing. There is no published corpus to inspect. |

Contrast with the retired snapshot design: no operator holds a
*stored* combined knowledge — there is no corpus to seize, leak,
or mine, including for Boulder. The honest residue is flight, not
storage: questions exist in the open while they travel, because
broadcast requires it, and §10's sealing covers answers, not
questions. That residue is named here because a design that claims
more privacy than its mechanisms deliver will eventually be caught
by its own users. It is also the design's honest cost, alongside
§3's availability.

## 13. What v1 builds, in order

1. **This design + frozen specs:** claim canonical form and batch
   format (done — PR #10: name, display-name, `album_track`,
   provider-link claims; golden SoulID vectors). The §10 message
   envelope and §6 forwarding envelope are specified in this doc.
   A byte-exact **conformance corpus** (normalizer vectors, JCS
   batch bytes, signatures, forwarding envelopes, merge outputs)
   is a named deliverable of this step (§10).
2. **The relay (v1):** the §10 switchboard as first specified —
   presence, connect-time key check, routing by query ID, budgets,
   no-content-logging. **Done** (code shipped, PR #13). Its
   fairness and protocol gaps are step 5's work, not a reopening of
   this step.
3. **Node relay transport:** persistent connection, reconnect,
   heartbeat, hello/query/answer/done client — in the node's
   existing networking layer beside the SIN pool client. **Done**
   (PR #14).
4. **Node responder — done-pending:** the local claims store built
   from the SoulSync export (PR #12's builder, push transport
   deleted), name index for text queries, answer construction
   honoring §9 modes and §6 caps, preview UI ("what your node
   would answer") carried over from PR #12. Two folds from the
   review night land with it before step 6 builds on it: the
   builder mints `provider_link` claims (§6 — without them §8's
   disambiguation and step 8's tab dedupe have no input), and the
   responder signs each subject's claims as **two batches** — one
   whose claims all sit on §7.1's allowlist (re-servable, by the
   computed rule) and an own-library-only one — so one held-back
   field can't poison a whole subject's spread. Each batch carries
   the subject's name claim (§6), so each stands on its own.
5. **Relay hardening + protocol v2:** everything the review night
   found in the v1 relay, landed as one protocol revision *before*
   step 6's querier hardens against the old protocol: binding
   attestations issued at the token exchange and embedded in
   batches (§9), plus relay sender stamping for accounting (§9);
   sealed, compressed answers (§10); `hello` binding relay
   identity and node mode (§10); byte-based budgets, violation
   decay, packed answers, late answers dropped never charged
   (§10); the issuer-signed, sequenced revocation list —
   delivered at every exchange and re-authentication and pushed
   live as `revocations` (§10) — and in-band re-authentication
   at token expiry (§10); the
   guest-list check moved to the token exchange, leaving the relay
   with no database access; aggregate counters (§10).
6. **Node querier + merge — the core build.** This step is the
   system; it is sized as several times steps 2–4 combined, and
   earlier drafts that listed it as one line were wrong. It is:
   query fan-out; streaming answers into the §8 merge — a **merge
   rewrite**, not a call into PR #10's library (that library is a
   starting point whose input contract predates the real claim
   shapes), including the layer that groups claims, dedupes by
   origin key, counts re-served copies once, and applies §9's
   tiers; the answer cache with origin aging (§6) and
   eviction by reputation; attestation verification (§9);
   reputation itself (§5); and §7's verification fetchers.
   **Open sub-decision, flagged not assumed:** fetcher credentials
   ride the node's existing SoulSync connection (SoulSync exposes
   provider fetch through its local API), or v1 verification is
   MusicBrainz-only (§7). Decide before this step starts.
7. **Relay deployment:** a host, a public `wss://` address with
   TLS, restart + monitoring, and the official relay URL shipped
   as the node's default (no pasted URLs). Before any public node
   ships.
8. **SoulSync depot + surfaces:** the **depot first** (§1) — the
   SoulSync extension holding network-collected metadata with its
   provenance — then the SoulSync surfaces, all governed by one
   rule (Boulder, Oct 28, 2026): **the network is asked only on
   real interest — a click, a wishlist add, a download. A search
   result appearing on screen never asks.**
   - **Tagging never asks.** Tagging at post-processing reads the
     depot only; it never sends a question. Depot data arrives
     from asks made earlier, at moments of interest, and from
     the daily reconcile — never from a tagging-time miss. (If
     any tagging-time fallback is ever kept, it is local-only —
     `network: false` — under a hard per-file budget of ~2s.)
   - **The detail-page jump-start and the combined search tab
     are one mechanism: ask on click.** Clicking a result sends
     the node one exact question, by the IDs the sources already
     returned; the page renders immediately from the active
     source + MusicBrainz, and the network's answer fills a
     "From the network" section when it lands — a section that
     renders **only when non-empty**: nobody answered, no box.
     Wishlist adds and queued downloads ask the same way, in the
     background, skipping the ask when the depot already holds a
     fresh answer.
   - **The combined tab** shows the active source + MusicBrainz
     as one deduplicated list (the depot's link facts may inform
     the merge); its **badges are read from the depot only** —
     an item never asked about shows no badge and sends no
     question. Dedupe merges must not block first results:
     source rows render first, network extras arrive later.
   Every network-sourced string is escaped at render (§6). The
   depot's per-field provenance and the §2.4 network mark
   originate here (SoulSync-side storage and export bit, §5.1
   G1), and the §2.4 tag-travel limit — values in file tags
   re-importing as provider-sourced — is the accepted risk
   recorded there; if SoulSync ever tracks re-import provenance,
   it lands in this step. Its own SoulSync PR(s), against dev.
9. **Enrichment view:** merged record vs local gaps,
   display-only. It is a user surface like any other: §9's
   buckets only — exact counts and origin keys are never exposed
   on any user surface, this one included.
10. **Scaffold cleanup — DO NOW,** in the first PR that touches
    the tree, not last: delete `src/metasync/` (per-item server
    records — the design refuses them) and the PR #11 branch's
    remains; migration 0011 never ships.

Step 0 from the old draft (a one-library export spike) no longer
gates anything, but it gains two jobs: it builds the
**call-economics model** — provider calls per completed album with
and without the network, verification fetches counted on the network's
side (§15's targets come from this model, not from assertion) —
and it gets the §7.1 terms list **read by a human**. It still also
sizes the local claims store and the text index. It runs whenever
Boulder's export is available. Under the agreed plan (Oct 28,
2026) the network's value is better and more complete data — the
versions people own, the cross-provider links, the long tail —
more than fewer calls, so the model matters less than it did
when step 0 was written. It is still owed: on a few hundred real
albums from Boulder's library, measure **calls today** (what
tagging costs now), **fields filled** (what the depot would fill
above the floor), and **verification cost** (what verifying them
costs).

**Scaling trigger (written down, not pre-built):** broadcast (§3)
holds until roughly 1,000 concurrently answering nodes at
sustained query budgets. Past that trigger, routing is redesigned
(§11) — a named future decision, taken when the numbers force it.

## 14. Open decisions (Boulder)

1. **Depot promotion policy:** the depot itself is decided
   (Boulder, Oct 27, 2026 — §1, §13 step 8). There is no node-side
   write path into library records, and none is planned. The
   promotion **floor** is also decided, and is not per-workflow:
   §8's floor (strong-verified tier, or a local confirmation
   fetch checked against an independent key, or the
   verified-anchor single-survivor rule) is the minimum for every
   workflow. Open: only what sits **above** the floor —
   per-workflow preference inside SoulSync, how strongly each
   workflow (tagging first) prefers a depot candidate over a live
   fetch. Settled per workflow as step 8 builds them.
2. **Genres in v1:** included (§8) — unverifiable, union-merged,
   never promoted on agreement alone when disputed. They are also
   on §7.1's re-serving hold list until the terms read clears
   them. The alternative is dropping the noisiest field from v1
   entirely.
3. **Relay host:** operator's choice (§10) — the properties
   (always-on, outbound-only nodes, no storage) are decided; the
   vendor is not. Deployment is a build step now (§13 step 7).

Decided and therefore *not* open: no server-side metadata storage
(§2.3); the depot model for SoulSync (§1); peer agreement never
promotes data by itself (§9); binding attestations + relay sender
stamping (§9, §10); sealed, compressed answers (§10); the relay
holds no database access (§10); relay instead of DHT for v1
routing (§11); answering defaults to own-library-only with
re-serving as the user's switch (§9); asking is ungated beyond a
bound SIN key (§9).

**Known limits (accepted for v1):**

- **Revocations reach the depot late.** The node has no durable
  invalidation outbox yet, so a revocation reaches SoulSync's
  depot only through the daily reconcile — up to ~24 hours in
  which a revoked origin's depot fields remain promotable. The
  node itself purges on receipt (§9); the lag is SoulSync-side
  only.
- **Duration is held, never promoted.** The depot stores
  durations, but no workflow promotes one — SoulSync's
  `tracks.duration` has mixed-unit history — so the node need
  not ship duration to SoulSync yet.
- **The depot as built is v1's ceiling.** New edge cases go on
  this list, not into code, until real traffic shows they
  matter (Boulder, Oct 28, 2026).

## 15. Success measures

V1 measures what v1 actually changes — measured locally on test
nodes, plus the relay's aggregate counters (§10) for the
network-level numbers. There is no query telemetry; counts are
not content.

- **Answer rate:** on a fixed test query set (mainstream +
  long-tail), the share of queries that return a merged record —
  at 5, 15, 30 answering nodes. The long-tail number is expected
  to start low and grow; that growth curve *is* the network
  working. Expect long-tail items to rarely strong-verify and
  rarely reach 3 standing holders — for them, labelled depot data
  is the success, and the measures count it as such.
- **Depot hit rate:** on a fresh test library, the share of empty
  fields for which the depot holds a network answer after exact
  lookups.
- **Tagging economics:** provider calls per newly imported album
  at tagging time, depot-first vs live-only, judged against the
  step-0 call-economics model. This replaces the old draft's
  "~5× fewer calls" figure, which was an assertion: mandatory
  verification spends calls too (a strong-verified candidate costs
  roughly 2–3 fetches), the network's real saving is replacing
  *fuzzy searches* with *fetches by ID*, and the honest target is
  whatever the model says.
- **Result richness:** median fields per merged result vs the best
  single provider's result for the same item.
- **Responsiveness:** time to first answer rendered, against the
  §10 answer window.
- **Zero network-sourced overwrites of local data.** (Invariant,
  not a target.)

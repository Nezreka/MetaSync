# DESIGN-METADATA.md — the MetaSync metadata network

Status: design, rewritten Oct 26, 2026 to Boulder's model. The first
draft staged a centrally compiled snapshot as v1 and live peer queries
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
SoulSync, which presents it as a source of its own: a super-source,
because it is effectively all sources combined.

The merged result is packed the way no single service's result is:
the complete tracklist, every provider ID anyone has matched
(MusicBrainz, Spotify, Deezer, Tidal, iTunes, Discogs), ISRCs,
durations, year, genres.

Searching and enriching are the same flow at different depths:

- **Search** finds the item (exact ID lookup, or fuzzy text) and
  shows the merged record.
- **Enrich** takes something already in the user's SoulSync library
  and fills its empty fields from the merged record. It never
  overwrites anything (§8).

Gap-filling by exact lookup is expected to be the everyday workhorse:
SoulSync already knows the IDs of what the user owns, so "who can
complete this album?" is a precise question the network can answer
usefully even when it is small. Text search is the headline; exact
lookup is the daily value.

## 2. Principles

1. **Local data always wins.** The network fills gaps and offers; it
   never overwrites a user's own metadata. Fields the network filled
   are tagged network-sourced forever (§8) and can be corrected or
   dropped when the network's answer changes.
2. **No audio. Ever.** Metadata only — no files, no file paths, no
   listening data.
3. **No metadata on the server. Ever.** The server stores accounts
   and keys (it already does, for SIN) and nothing else. Music data
   lives on nodes. The relay (§10) keeps the who's-online list in
   its own memory only and logs no query content — *routes and
   forgets* is a written rule of the service, not a vibe.
4. **Verify, don't vote.** Anything a claim asserts that can be
   checked against a public source gets checked (§7) — by the node
   that is about to rely on it. Counting agreeing nodes is the
   fallback for what cannot be checked, never the first resort.
5. **The network is a hint cache, not an authority.** A bad hint
   costs one verification call and gets dropped. SoulSync works
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
  query budgets so nobody can enumerate the network by asking about
  everything; nodes choose how much to answer (§9 modes).
  *Remaining cost:* a determined asker learns that *some node key*
  holds an item it asked about. Stated plainly in §12.
- **Text search over peers.** A DHT cannot do it, and broadcasting
  every keystroke is abuse. *Answer:* two query kinds (§10) — exact
  lookups by ID (cheap, precise), and text queries broadcast whole
  (one per search, not per keystroke) with a short answer window.
  At the network's realistic sizes — tens to low hundreds of nodes —
  broadcast is fine. Smarter routing is a later problem (§11).

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
                                                       relay reads, at
                                                       connect time only:
                                                       the existing DB —
                                                       accounts + node keys
                                                       (SIN's guest list)
```

- **SoulSync ↔ node:** local, the pairing that already exists for
  SIN charts. SoulSync relays a search to the node and receives one
  merged result. SoulSync never talks to the relay or to peers.
- **Node → relay:** one persistent outbound connection (WebSocket),
  authenticated at connect by the node's SIN key exchange (§10).
  The node registers, the relay checks the presented node key
  against the bound key in the existing database, and from then on
  the connection carries queries and answers. If the connection
  drops, the node reconnects; the relay's presence list rebuilds
  itself.
- **The node's local stores** (all on the user's machine):
  - *Own-library claims:* the node walks its SoulSync's metasync
    export and builds claims (§6) exactly as PR #12's contributor
    did — but instead of pushing them anywhere, it keeps them in a
    local claims store and answers queries from it.
  - *Answer cache:* every answer the node receives is stored with
    its provenance (which node key asserted it, the claims' own
    sources). Cached claims may be **re-served**: when a later query
    matches, the node may answer with cached claims it learned from
    others, labeled as re-served, with the original signatures
    intact (a re-served bundle is the origin node's signed bundle,
    forwarded — never re-signed as the forwarder's own).
  - *Reputation:* a local score per node key, from how that key's
    answers fared under §7 checks. Private to this node, never
    shared, never leaves the machine.
- **The relay:** §10. It stores nothing, holds presence in memory,
  and its only database access is reading the guest list when a
  node connects.

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
  "source": "deezer",             // see G1; enum incl. "user_edit", "file_tags"
  "soulid_version": "kinded_v2"
}
```

Plus three special claim shapes:

- **Name claim (mandatory, one per entity per answer):** the exact
  SoulID input tuple — the normalized parts `kinded_v2` hashes
  (artist: [name]; album: [artist, title]; track: [artist, title]).
  Any node recomputes the SoulID from the tuple; a mismatch
  invalidates the whole entity's claims in that answer. This makes
  name claims self-verifying and supplies the names §7 compares
  against providers.
- **Display-name claim:** the entity's names as humans write them
  ("OK Computer", not the normalized "ok computer" the hash
  consumes). Display names are presentation, not identity — never
  used for matching. Whether provider-sourced display names
  (Deezer/Spotify spellings) may be re-served to other nodes is a
  terms item (§7.1); until it clears, nodes re-serve MusicBrainz
  spellings and their own, and use the rest locally only.
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
  asserted_at, nonce}` — `batch_id` is the batch's content digest,
  `node` the asserting node's key, `asserted_at` the node's clock
  at minting (informational; §6's cache ages run from *receipt*).
  The batch is **not bound to any query**: the same batch may answer
  many queries over its life, which is what makes re-serving (§5)
  possible. The query binding lives one layer out, in the unsigned
  `answer` message (§10) that carries the batch — routing metadata,
  not part of the signed claim. The answering node signs the batch
  hash with its SIN-bound Ed25519 key, exactly like SIN request
  signing (PORTAL-DESIGN §3A.2; keys and signatures are base64url of
  the raw 32 bytes, as in `portal_keys`). Signing binds claims to a
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
- **Retractions are local and simple.** A node whose library changed
  simply stops asserting the claim. There is no network-wide recall —
  stated plainly, because pretending otherwise would be lying: a
  claim already cached by other nodes ages out under their cache
  policy, measured from when *they* received it:
  - *Unverified* cached claims expire after **90 days** and are
    re-asked for if still needed.
  - *Verified* cached claims (§7) do not expire on a timer, but are
    **re-verified before being relied on again** once their check
    is more than 90 days old, and dropped when that check fails or
    when §7.1's positive-evidence-of-absence test is met. A
    verified claim is therefore never immortal: reliance always
    eventually re-runs the check.
- **Sizes.** An answer batch is capped at 5,000 claims (mirroring
  SIN's report caps); a query's total answer window is capped by
  the asking node's merge budget (§10). Exact lookups are small by
  nature; text-query answers are capped per answering node so one
  broad match cannot flood the asker.
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
verifier; verification happens **on the asking node**, which has its
own provider access (the same public APIs SoulSync already uses) and
spends it only on answers it is about to rely on.

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
  so plausible-only claims are treated as unverifiable: they need
  §9's multi-node support, and are labelled plausible in provenance.

Per provider in v1:

- **MusicBrainz, Deezer, Spotify:** the node's fetchers verify
  against these. MBID kinds are explicit per entity — artist →
  artist MBID, album → **release-group** MBID, track →
  **recording** MBID. SoulSync exposes *release* MBIDs; release →
  release-group resolution is one extra MusicBrainz call, cached.
- **Tidal, iTunes, Discogs, Hydrabase:** claims accepted but held
  as unverifiable in v1 (multi-node-gated). Their fetchers are
  later work.

### 7.1 Budgets and terms — now per node, not central

Verification costs land on the asking node, which is the right
place: a node verifies what *it* relies on, at the moment it relies
on it, and caches the verdict with the claim.

- **Etiquette is mandatory:** MusicBrainz ~1 req/s per node,
  per-provider caps, a verified ID is never re-fetched inside
  90 days except under §6's re-verification-on-reliance rule. A
  node that hammers a provider
  gets its own credential banned — its problem, not the network's,
  and the caps keep it from becoming one.
- **Spot-check, don't census.** A node verifies claims it is about
  to display as fact or fill from — not every claim that streams
  past. Display can stream in plausible answers immediately and
  upgrade them as checks land (§10's answer window).
- **Terms:** Spotify's and Deezer's developer terms restrict
  storing and redistributing their catalogue data. The claims model
  keeps the safest possible posture: what travels is **MusicBrainz
  data (CC0) and bare ID links** (facts: "SoulID X is Deezer track
  Y") plus contributor-sourced values (durations, ISRCs, years as
  found in the contributor's own library). Whether
  contributor-sourced durations and ISRCs may be re-served (they
  usually *originated* in provider data upstream of the
  contributor's files — "no provider owns them" is an assertion,
  not a fact) and whether provider display names may be re-served
  (§6) are explicit terms-check items; until they clear, those
  fields are answered from a node's own library but **not
  re-served** from cache. Year fills only from MusicBrainz-sourced
  claims until the same check clears (§8).
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
| Year | MusicBrainz release-group year (from an MB-sourced claim) or **not filled**. Provider years stay local-only until the §7.1 terms check clears re-serving them. Edition years are a separate field and never feed this one. |
| Genres | Not a conflict. Union them; display rank order and §9 coarse support, never exact per-genre counts. Disputed genres are marked never-auto-fill. |
| Anything unverifiable and disputed | Show all values **with** the disagreement visible, marked never-auto-fill. |
| Everything | Fill empty fields only. Never overwrite local data. |

**Multi-valued fill rule (enrich):** provider IDs are sets at
*every* kind, tracks included — a track SoulID (hash of artist +
title) is shared by the live version, the remaster, and the single
edit, and each has its own true Spotify ID. When the merged view
holds a set for a field SoulSync stores singly, enrich fills only
if exactly one candidate survives disambiguation against the
user's local data:

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

**Provenance:** every merged field carries value, support
(strong-verified by this node / plausible / coarse node bucket §9),
and `merge_rules_version`. Asserting node keys are protocol-visible
to the receiving node's *software* — signatures, dedupe (§6), and
reputation (§9) are impossible without them — but they are never
shown to the user and never passed to SoulSync: the user and
SoulSync see coarse support buckets only. When write-back ships (§14.1), SoulSync
stores that provenance with the filled value. Network-filled fields
never promote to "user data": a later answer can update or retract
them, and §2.1's "local wins" applies to the user's own data only.

**Sanity rule (replaces the old compile canary):** there is no
compiler, so there is no canary — but a node never lets fresh
claims silently change a field it previously strong-verified. A
conflicting answer triggers re-verification of the stored value
first; the verified value stands until the check says otherwise.

## 9. Local trust rules — what a node shows and fills

There is no publication and no global corpus, so there are no
publication gates. Each node decides, locally and visibly:

- **Strong-verified (§7) claims are trusted at N=1** — one node's
  claim, once this node has cross-checked it against providers, is
  displayable as fact and fillable.
- **Everything unverifiable needs support: ≥3 distinct asserting
  node keys** (dedupe per §6 — re-served copies of one node's claim
  count once) before it may auto-fill. Below that it is shown
  labelled with its coarse support (`1–2 nodes`, `3+ nodes`) and
  never auto-fills. Exact counts are shown only as those buckets —
  a precise count next to a sparse item is a fingerprint of how
  many libraries hold it, and the buckets are enough to judge by.
- **Support only counts keys with standing — the Sybil price.**
  Portal accounts are free to create, so "3 keys" alone would cost
  an attacker three signups; §7's warning would eat §9 alive. An
  asserting key therefore counts toward support on a node only if
  it has *standing there*: its key binding is at least 30 days old,
  or it has previously given this node answers that passed §7
  checks. Fresh keys' unverifiable claims are displayed (labelled)
  but cannot push a field to auto-fill until their keys earn
  standing. This does not make Sybil attacks impossible — three
  aged, well-behaved sockpuppets still pass — it raises the price
  from minutes to months of good behavior, and the operator's
  existing power to revoke portal keys is the backstop. Stated
  plainly because a design that hides its Sybil price is lying
  about its trust model.
- **Reputation (§5) never gates; it spends the node's own scarce
  resources.** Concretely, a node's reputation for a key decides:
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
- enforces per-node budgets (queries per hour, answer bytes per
  query) so one node cannot scrape or flood the network;
- **logs no query content and no answers, ever** — operational logs
  are connection counts and error classes only;
- stores no metadata of any kind, in any table, at any time.

**Hosting:** the relay must stay awake — it holds connections open —
so it cannot be a Neon function (those sleep between requests). It
is a small always-on program on the **operator's server
infrastructure** (the same family as the SIN backend — never on
users' machines, which §3 establishes cannot accept inbound
connections; the host vendor is the operator's choice, §14.3). Its
only database access is a read-only peek at connect time: is this
node key a bound `portal_keys` row? That guest list already exists
for SIN. The relay needs no schema of its own.

**Identity:** a node connects with the SIN exchange (§3A.2 rails:
pool key + Ed25519 signature → short-lived token scoped `meta:read`
for asking; answering runs over the same connection). The exchange
token's claims are exactly `{key_id, node_id, scopes, iat, exp}` —
no account anywhere in it. The relay's connect-time lookup reads
the bound key's *status* only; it does not need, and its queries do
not select, the account column. That is minimization by mechanism
where mechanism can do it — and §12 Q2's policy ("routes and
forgets", no joining traffic to accounts) covers the rest, because
the same operator runs both the relay and the account database and
no message format can change that. The doc does not claim
construction where only policy exists.

**Messages** (JSON over the connection; a binary framing is a later
optimization, not a v1 need):

- `hello` — node key, protocol version, node mode (§9). Before it,
  the relay issues a per-connection **challenge** (a random nonce,
  single-use); the hello's node signature must bind that nonce, so
  a captured hello cannot be replayed on a fresh connection. Relay
  replies `welcome` with the current peer count (coarse: exact
  under 100, rounded above — the count is a presence hint, not a
  census) or `refused` with a reason class.
- `query` — `{query_id, kind, mode, …}` where mode is:
  - `exact` — one or more SoulIDs (or legacy aliases of them): "who
    holds claims about these subjects?"
  - `text` — `{kind, terms}`: "who holds claims about an artist /
    album / track matching this?" Answered from a node's own name
    index over its claims stores (own library + cache), never by
    scanning raw exports per query.
- `answer` — `{query_id, batch}` where batch is a §6 signed claim
  batch, or a §6 forwarding envelope for re-served claims. Answers
  stream: a node sends when it has something, not when it has
  everything.
- `done` — `{query_id}`: an answering node signals it has no more.
  Askers do not wait for all `done`s; the **answer window** closes
  after ~6 seconds or when every connected node has answered or
  signalled, whichever is first. Late answers (inside a short grace
  period) still land in the asker's cache — they enrich the next
  query even when they miss this one's display.
- `bye` / heartbeat — liveness only.

**Query IDs and answer admission (the protocol's front door):**
`query_id` is 128 bits of randomness minted by the *asking* node,
scoped to its connection. The relay forwards a query only to nodes
connected at that moment and routes answers only for query IDs it
actually routed, only back to the connection that asked, and only
inside the window + grace period. An asking node admits answers
**only for query IDs it minted and has not closed** — anything else
is dropped on arrival and charged against the sender's relay
budget. Total admitted answer bytes per query are capped; past the
cap, answers are dropped (cache included) until the query closes.
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

## 11. Federation and the road beyond one relay

- **Pools are relays.** SIN already has pools; a metadata relay is
  run per pool (v1: the official pool's relay). A node may connect
  to several relays at once; queries fan out to all of them.
  **Support is never summed across relays** — the same node key on
  two relays is still one assertion, and dedupe (§6) is by node
  key, so cross-relay duplication collapses naturally.
- **Independent relays are independent trust domains for presence
  only.** There is no corpus to disagree about — every node merges
  for itself — so federation here is trivial compared to the old
  snapshot design: a relay cannot assert data, only carry it.
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

**Metadata privacy rules (Q-rules):**

- **Q1.** The server stores **no metadata**: no claims, no queries,
  no answers, no holdings, no corpus. Q.E.D. by architecture — the
  only metadata-shaped thing on the server is the relay's in-memory
  presence list, which is never persisted.
- **Q2.** The relay operator runs the switchboard, so the relay
  *transiently sees* queries and answers pass through. The service
  rule is **routes and forgets**: no content logging, no retention,
  no joining traffic to accounts. There is no mode that hides a
  query from the relay carrying it — asking *is* announcing
  interest in an item, to the relay and to answering nodes. The
  opt-in copy says exactly this, in plain words, before the node
  connects to a relay for the first time.
- **Q3.** Answering reveals holding — to the asker (as a node key,
  not an account or an IP) and to the relay. Budgets (§10) blunt
  enumeration; node modes (§9) let a user answer less or never.
  There is no setting that makes answering anonymous against a
  determined asker, and the copy does not pretend there is.
- **Q4.** SIN's tables are off-limits to metadata code and vice
  versa. The single shared artifact is the portal account + bound
  node key, used by the relay only as a connect-time yes/no (§10).

| Who | Learns what |
|---|---|
| Asking node | The merged answers to its query. Its *software* sees asserting node keys (pseudonymous — required to verify signatures); its *user* sees coarse support buckets only (§8). |
| Answering nodes | The query (kind + terms/subjects), and that some node key asked. Not the asker's account or IP. |
| The relay | Which node keys are connected, their IP addresses, and connect/disconnect timing — it cannot route without seeing these. In flight, per query: the asker's key, the answering keys, and the content — i.e. who holds what, network-wide, in real time. All of it never logged or stored (Q2) — but *seen*, and §3's "the relay hides IPs" means hidden from other nodes, never from the relay itself. Not accounts (the token carries none, §10). |
| The operator's database | Nothing new. Accounts and keys it already held for SIN. |
| Non-participants | Nothing. There is no published corpus to inspect. |

Contrast with the retired snapshot design: no operator ever holds
the network's combined knowledge — including Boulder. That is the
point of the design, and also its honest cost (§3's availability).

## 13. What v1 builds, in order

1. **This design + frozen specs:** claim canonical form and batch
   format (done — PR #10: name, display-name, `album_track`,
   provider-link claims; merge-rules v1; golden SoulID vectors).
   The §10 message envelope and §6 forwarding envelope are
   specified in this doc; they freeze as the first task of step 3,
   implemented around PR #10's batch bytes unchanged.
2. **The relay:** the §10 switchboard — presence, connect-time key
   check against `portal_keys`, routing by query ID, budgets,
   no-content-logging. Runs as its own small service; ships with an
   integration test that proves it stores nothing (restart wipes
   presence; no tables touched beyond the key peek).
3. **Node relay transport:** persistent connection, reconnect,
   heartbeat, hello/query/answer/done client — in the node's
   existing networking layer beside the SIN pool client.
4. **Node responder:** the local claims store built from the
   SoulSync export (PR #12's builder, push transport deleted),
   name index for text queries, answer construction honoring §9
   modes and §6 caps, preview UI ("what your node would answer")
   carried over from PR #12.
5. **Node querier + merge:** query fan-out, streaming answers into
   the §8 merge, §7 spot-verification during the window, answer
   cache writes with provenance, reputation updates.
6. **SoulSync super-source:** SoulSync relays searches to the node
   and renders the merged result as a first-class source (its own
   SoulSync PR, against dev).
7. **Enrichment view:** merged record vs local gaps, display-only.
8. **Scaffold cleanup:** delete `src/metasync/` (per-item server
   records — the design refuses them) and the PR #11 branch's
   remains; migration 0011 never ships.

Step 0 from the old draft (a one-library export spike) is still
useful but no longer gates anything: it sizes the local claims
store and the text index, and re-checks the §7.1 terms items. It
runs whenever Boulder's export is available.

## 14. Open decisions (Boulder)

1. **Enrichment write-back:** v1 is display-only. Write-back needs
   (a) a SoulSync API write path for fill-empty fields and (b)
   §8's provenance tagging as a precondition. Its own SoulSync PR,
   after v1 proves the flow. (Recorded so it isn't lost.)
2. **Genres in v1:** included (§8) — unverifiable, union-merged,
   never-auto-fill when disputed. The alternative is dropping the
   noisiest field from v1 entirely.
3. **Relay host:** operator's choice at build time (§10) — the
   properties (always-on, outbound-only nodes, no storage) are
   decided; the vendor is not.

Decided and therefore *not* open: no server-side metadata storage
(§2.3); relay instead of DHT for v1 routing (§11); answering
defaults to own-library-only with re-serving as the user's switch
(§9); asking is ungated beyond a bound SIN key (§9).

## 15. Success measures

V1 measures what v1 actually changes — all measured locally on
test nodes, since there is deliberately no query telemetry:

- **Answer rate:** on a fixed test query set (mainstream +
  long-tail), the share of queries that return a merged record —
  at 5, 15, 30 answering nodes. The long-tail number is expected
  to start low and grow; that growth curve *is* the network
  working.
- **Gap-fill rate:** on a fresh test library, the share of empty
  fields the network fills via exact lookups.
- **Result richness:** median fields per merged result vs the best
  single provider's result for the same item.
- **Responsiveness:** time to first answer rendered, against the
  §10 answer window.
- **Zero network-sourced overwrites of local data.** (Invariant,
  not a target.)

Deferred to the write-back phase (§14.1): provider API calls per
newly imported album — target ~5× fewer than today. It cannot be a
v1 metric: while enrich is display-only, SoulSync still makes its
own calls.

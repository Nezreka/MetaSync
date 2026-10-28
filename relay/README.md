# MetaSync metadata relay

The relay is the switchboard for the MetaSync metadata network
(DESIGN-METADATA.md section 10). Nodes hold persistent WebSocket
connections to it. A node sends a query, the relay forwards it to the
other connected nodes, and their answers travel back to the asker,
matched by query ID. SoulSync searches and gap-filling ride on this;
the claims themselves are the nodes' business, never the relay's.

## The no-storage, no-database rule

This is a specification, not a goal:

- The relay stores no metadata of any kind, in any table, at any time.
  No claims, no queries, no answers, no holdings.
- Presence (which node keys are connected) lives in process memory
  only. It is never written to disk or to a database. A restart empties
  it and nodes reconnect.
- The relay has NO database access at all (protocol v2 removed the
  one connect-time peek the first version had). Whether a node's
  portal key is valid and bound to it is checked at the TOKEN
  EXCHANGE, before the token is issued; the relay verifies the issued
  token offline against the issuer public key and checks locally
  that the token's node_id is the SHA-256 of the hello public key.
- Sealed answers (protocol v2) are opaque bytes to the relay: it
  meters and stamps envelopes, and never sees claim plaintext.
- Logs carry connection counts and error classes only — no node ids,
  in whole or in part. Query terms, subjects, and claim content are
  never logged (the operator rule is "routes and forgets", Q2).

## How a connection works

1. The node exchanges its pool key for a SIN access token as usual
   (PORTAL-DESIGN section 3A.2). The token must carry the `meta:read`
   scope, and the exchange — not the relay — checks the key's
   validity and binding.
2. The node opens a WebSocket to the relay. The relay speaks first:
   a `challenge` message carrying a fresh 32-byte random nonce,
   issued for this connection only.
3. Protocol v1 hello: node public key, mode (`full`, `own_library`,
   or `leech`), the access token, a timestamp, the nonce echoed
   back, and an Ed25519 signature over `"metasync-relay-hello\n" +
   token + "\n" + timestamp + "\n" + nonce`. Protocol v2 hello:
   a version RANGE (`version_min`/`version_max`), the same fields,
   declared capabilities (sealing suites, payload encodings,
   dictionary id), and a signature that additionally binds the
   relay identity (the canonical dialed origin: scheme, host, port,
   lowercased, no path) and the declared mode — a hello captured
   for one relay or mode cannot be replayed to another.
4. The relay checks the echoed nonce (and spends it either way),
   verifies the token offline against the issuer public key, checks
   the token's node_id against the SHA-256 of the presented node
   key, verifies the hello signature for the negotiated version,
   and checks the node against its revocation list. Every failure
   gets the same `refused` reply with reason class `auth`, then the
   connection closes. No detail leaks about which check failed.
5. On success the relay replies `welcome`: the negotiated version,
   the relay identity, a coarse peer count (exact under 100, rounded
   above that), and the current revocation list (empty when none is
   configured). When the list changes, the relay pushes a
   `revocations` message to connected v2 nodes.

After that: `query`, `answer`, `done`, `bye`. An answer in v2 is one
packed message (a `batches` array) or one `sealed` payload (HPKE to
the query's ephemeral reply key, over the zstd-dictionary-compressed
signed batches — opaque here). A query carrying a `reply_key` fans
out to v2 peers only: a v1 peer cannot seal, so its answers would
come back as plaintext — rather than silently downgrading the
query's confidentiality, v1 peers are excluded from that query's
fan-out (and its expected set) entirely. The relay stamps every forwarded
answer with the answerer's authenticated node id (`stamp.from`), and
sends the asker a `complete` when every routed node has signalled
done or the window has ended. Before the token expires, the client
re-exchanges and sends `reauth` on the same connection; only a
missing or failed re-auth at expiry plus a short grace closes it.

## Revocation

The relay holds the issuer-signed revocation list
(`{issuer, seq, issued_at, keys, sig}`), fetched from
`RELAY_REVOCATIONS_URL` on an interval and verified — signature
against the issuer key, and a per-issuer sequence that never goes
backwards. With no URL configured the list is empty. Revoked nodes
are refused at hello and dropped if connected when a list lands.

## Budgets and the answer window

Per node, enforced by the relay, all byte-based for answers:

- queries per hour (default 600);
- answer bytes per hour (default 128MB) and per node per query
  (default 2MB); total admitted answer bytes per query capped
  (default 8MB);
- an answer is admitted only for a query ID the relay actually
  routed, only back to the asker, and only inside the answer window
  (default 6 seconds, closing early once every asked node has
  answered or signalled `done`) plus a grace period (default 10
  seconds), during which in-flight answers still reach the asker;
- late answers (after window+grace) are dropped and NEVER charged;
  unsolicited, self-directed, or over-budget answers are dropped and
  counted as violations, which DECAY — each strike ages out after
  the decay period (default 1 hour) instead of accumulating forever;
  a connection past the violation limit (default 10) is closed;
- every send goes through a per-connection queue with a per-send
  timeout, so one stalled peer can never freeze the relay or the
  other nodes.

Aggregate counters only (queries routed, answers forwarded, bytes
in/out, active connections, share of queries that got at least one
answer) are exposed via a snapshot method and a periodic structured
log line — never content, never per-node.

Heartbeat is the WebSocket ping/pong from the `websockets` library.
A dead connection drops out of presence when it closes.

## Configuration

Environment only. The relay refuses to start when a required variable
is missing or malformed, and says which one (never its value).

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `RELAY_POOL_PUBLIC_KEY` | yes | none | Issuer Ed25519 public key: base64url of the raw 32 bytes, or PEM (verifies access tokens offline; also the default issuer key) |
| `RELAY_ISSUER_PUBLIC_KEY` | no | the pool key | Issuer key for revocation lists, when different from the token key |
| `RELAY_ISSUER_ID` | no | none | Issuer id (with the key, enables the revocation feed) |
| `RELAY_REVOCATIONS_URL` | no | none | Where to fetch the signed revocation list |
| `RELAY_REVOCATIONS_INTERVAL_SECONDS` | no | `300` | Revocation fetch interval |
| `RELAY_ORIGIN` | for v2 | none | Canonical relay identity `scheme://host:port` bound into v2 hellos — **required for protocol v2**: without it the relay refuses v2 hellos rather than deriving its identity from client-supplied Host / X-Forwarded-Proto headers (that derivation is the federation impersonation hole); v1-only operation may omit it |
| `RELAY_HOST` | no | `127.0.0.1` | Bind address (the Dockerfile sets `0.0.0.0`) |
| `RELAY_PORT` | no | `8791` | Bind port |
| `RELAY_QUERIES_PER_HOUR` | no | `600` | Per-node query budget |
| `RELAY_ANSWER_BYTES_PER_HOUR` | no | `134217728` | Per-node hourly answer byte budget |
| `RELAY_ANSWER_NODE_QUERY_CAP_BYTES` | no | `2097152` | Per-node per-query answer byte cap |
| `RELAY_ANSWER_CAP_BYTES` | no | `8388608` | Total admitted answer bytes per query |
| `RELAY_WINDOW_SECONDS` | no | `6` | Answer window length |
| `RELAY_GRACE_SECONDS` | no | `10` | Post-window grace for late answers |
| `RELAY_SEND_TIMEOUT_SECONDS` | no | `5` | Per-send timeout on connection send queues |
| `RELAY_SEND_QUEUE_MESSAGES` | no | `128` | Per-connection send queue depth |
| `RELAY_SEND_QUEUE_BYTES` | no | `16777216` | Per-connection send queue byte cap (16MB; the burst headroom the Fly VM is sized for — see the `[[vm]]` comment in fly.toml) |
| `RELAY_MAX_VIOLATIONS` | no | `10` | Live violations before the relay closes a connection |
| `RELAY_VIOLATION_DECAY_SECONDS` | no | `3600` | How long a strike stays live |
| `RELAY_REAUTH_GRACE_SECONDS` | no | `30` | Grace after token expiry for a re-auth to land |
| `RELAY_COUNTERS_LOG_SECONDS` | no | `60` | Aggregate counters log interval (0 disables) |

## Running it

Local:

```sh
pip install -r requirements.txt
RELAY_POOL_PUBLIC_KEY=... python -m metasync_relay
```

Docker:

```sh
docker build -t metasync-relay .
docker run -e RELAY_POOL_PUBLIC_KEY=... -p 8791:8791 metasync-relay
```

It must run somewhere always-on with a public address: nodes in homes
cannot accept inbound connections, which is the entire reason the
relay exists. It cannot be a Neon function, because those sleep
between requests and the relay holds connections open.

## Deploying on Fly.io

The production relay is ONE always-on Fly.io machine, configured by
`fly.toml` in this directory. Everything below is run from `relay/`.
The account is Boulder's. Deployment is one command, always with
the same flag — `fly deploy --ha=false` — and the FIRST deploy
adds a count check (step 3), because Fly's defaults create
machines in ways this app cannot tolerate. Why `--ha=false` rides
every deploy, not just the first, is in "Day to day".

Why one machine, and why Fly's defaults had to be overridden, is
commented line by line in `fly.toml`. The short version: presence
and every node's socket live in this process's memory, so the
machine never sleeps (`auto_stop_machines = "off"`,
`min_machines_running = 1`), the connection limits are set in the
thousands (Fly's unset default soft limit is 20), and the machine
count is held at 1 — two machines would split the network in
half with no way to join the halves. Note that fly.toml cannot
pin the count itself: Fly decides it at deploy time, which is
why step 3 below exists.

### From account-ready to live

1. `fly auth login`, then create the app:
   `fly apps create metasync-relay`
   (if the name is taken, choose another and update `app =` in
   fly.toml — the name is not part of the protocol).
2. Set the one required secret — the pool (issuer) PUBLIC key, the
   public half of the SIN function's `SIN_TOKEN_PRIVATE_KEY`, in
   the same formats the node accepts for `meta_issuer_public_key`
   (base64url of the raw 32 bytes, or PEM):
   `fly secrets set RELAY_POOL_PUBLIC_KEY=<public key>`
   Get the value from SIN itself: `GET /v1/auth/public-key` on the
   SIN function returns `{"public_key": <base64url>}` — that
   response is the source for this secret AND for a node's
   `meta_issuer_public_key` (step 5). Do not transcribe the key
   from anywhere else.
   The relay refuses to start without it; that is deliberate.
3. First deploy — with the machine count pinned, BEFORE the
   domain step. Fly's default for an HTTP/TCP-service app is
   high availability: TWO machines on the first deploy, and
   fly.toml has no way to pin the count — it is decided here,
   at deploy time. Two machines would each hold half the
   network's WebSockets with no join between the halves, and
   with auto-stop off a spare created now would live forever.
   So:
   - `fly deploy --ha=false`
   - Confirm exactly one machine exists and is up BEFORE going
     on: `fly scale show` (count 1) and `fly status` (one
     machine, started). If anything ever shows more, run
     `fly scale count 1` immediately; see the one-machine rule
     in fly.toml.
   Deploying before the certificate exists is harmless: the
   machine boots with the final origin already in its config
   (step 4 explains why that value matters), but nothing can
   dial that origin until DNS points at Fly — and a smoke test
   is only meaningful once the count is known to be 1.
4. Custom domain. The relay's identity (`RELAY_ORIGIN` in
   fly.toml) is bound into every v2 hello signature, and nodes
   sign for the origin they dialed — so the production origin
   is the custom domain from the very first boot (it already
   is: the step 3 deploy carried it), and the `<app>.fly.dev`
   hostname is never a working v2 address once this is set
   (its hellos sign for the wrong origin and are refused).
   - `fly certs add relay.metasync.ssync.net`
   - In Cloudflare DNS for ssync.net, add the records Fly prints
     for the name `relay.metasync` (that name in the ssync.net
     zone IS relay.metasync.ssync.net): the A and AAAA records to
     the app's Fly IPs with the proxy OFF (DNS only — a proxied
     record breaks Fly's certificate validation and the WebSocket
     path), plus the ACME challenge record if Fly asks for one.
   - Wait until `fly certs show relay.metasync.ssync.net` reports the
     certificate issued.
5. Smoke test with a real node. Two preconditions, both easy to
   miss:
   - The node must have JOINED the pool with its pool key AND had
     its binding APPROVED in the portal. An unapproved node gets
     a 202 `binding_pending` from the token exchange and no token
     at all — it can never reach the relay, and no relay-side
     debugging will show why.
   - The step 4 certificate must report issued and DNS must be
     live. The node release carrying the shipped default URL is
     only useful once that is true: the default IS the custom
     domain, so before cert + DNS the default points nowhere.
   Then: leave the node's `meta_relay_url` unset (it now defaults
   to `wss://relay.metasync.ssync.net`), set `meta_issuer_public_key` from
   `GET /v1/auth/public-key` (step 2), turn `meta_network` on,
   and read the node's relay status (`connected`, `peers`,
   `last_error`):
   - **Success:** `connected: true` with `peers` a whole number
     (0 is fine — you may be the only node). The node refuses any
     welcome that is not v2, so a connected session IS a v2
     session; status carries no version field.
   - **`connected: false`, `last_error: "token: …"`:** the node
     never reached the relay — it could not get an access token
     from the pool, so it never dialed. The common case is the
     binding: `"token: waiting for approval in the portal — no
     access token has been issued"` means the exchange answered
     202 `binding_pending`. Approve the binding in the portal
     (compare the fingerprint the node's status shows), then do
     nothing: the node retries the exchange on its own, with
     backoff that tops out at a minute, and connects once the
     token lands. `"token: pool access token does not carry
     the meta:read scope"` means the pool issued the node a
     token without the metadata scope — the node checks the
     scope itself before it ever dials, so this never reaches
     the relay; it is a pool-side issuance problem, and the
     node's pool status shows what the exchange last returned.
     Any other `token: …` suffix is an exchange-side failure
     (pool unreachable, pool key rejected); the node's pool
     status carries which.
   - **`connected: false`, `last_error: "refused: auth"`:** the
     relay checked the hello and refused it. Every cause lands
     in this one reason class, and `fly logs` cannot separate
     them — a refusal logs only `error_class=auth`. The causes,
     each with the check that actually narrows it down:
     - **Wrong origin:** the node dialed a name other than the
       one in `RELAY_ORIGIN` (the v2 hello is signed for the
       origin the node dialed; see the fly.dev section below).
       Check the node's effective relay URL.
     - **Clock skew:** the hello carries the node's timestamp,
       and more than five minutes from the relay's clock
       refuses. Check the node host's clock — it is the first
       check because it is the one cause visible from the node
       alone.
     - **Revoked key:** the issuer's signed revocation list
       names this node. Check the key's state in the portal.
       (Not yet a possible cause on the Fly deployment: no
       `RELAY_REVOCATIONS_URL` is configured there, so the
       list the relay checks is empty.)
     - **A token the relay cannot verify:** issued by a
       different pool than the relay's `RELAY_POOL_PUBLIC_KEY`,
       or expired. Confirm the relay secret came from
       `GET /v1/auth/public-key` on the same pool the node
       belongs to (step 2). The node re-exchanges as its token
       ages, so a pure expiry refusal clears on its own.
     - **Issued for a different node key than the node
       presents:** a pool-side issuance problem; the node's
       pool status shows what the exchange last returned.
     - **A relay with no `RELAY_ORIGIN`:** a relay run
       without one refuses every v2 hello as auth — it will
       not derive its identity from request headers. fly.toml
       sets it, so this cannot happen on Fly, only on a plain
       `docker run` that forgot the variable; check the
       relay's environment.
     A token missing the `meta:read` scope never gets here
     either — the node checks the scope before dialing and
     reports it as `token: …` above. (The relay refuses a
     wrong-scope token too, but only if the exchange's
     response disagreed with the signed token.)
     An unbound or unapproved node never gets here — it holds
     no token, and shows as `token: …` above.
   - **`last_error: "refused: version"`:** the relay answered but
     did not negotiate v2 — it is running pre-v2 code. Upgrade
     the relay; the node will not degrade to v1.
   - **`last_error: "connect: …"`:** the session never got
     established. The suffix separates the sub-cases:
     - An OS or TLS error as the suffix: the WebSocket never
       opened — DNS, TLS (certificate not issued yet), or the
       machine/proxy down. Check `fly status` and
       `fly certs show`.
     - A bare `"connect: "` (empty suffix): the socket opened
       but no welcome arrived within 30 seconds — something
       accepted the connection and then said nothing. Check
       `fly logs` for the relay's periodic counters line: its
       absence means the relay process is not the thing
       answering on that port.
     - `"connect: relay did not open with a challenge"`:
       something answered that does not speak the relay
       protocol — it never even sent the opening challenge.
       Same first check as the not-welcome case below: what
       the dialed name actually resolves to.
     - `"connect: relay hello reply was …, not welcome"`:
       something answered that does not speak the relay
       protocol — the wrong service is on the address, or a
       proxy is mangling the upgrade. Check what the dialed
       name actually resolves to.
     A transport drop AFTER a session was established also
     lands here, with the drop's error as the suffix. The node
     reconnects on its own, so a drop that does not repeat is
     noise, not a smoke-test failure.
   - `meta_issuer_public_key` has NO effect on connecting: a
     wrong or missing one drops attestations and revocation
     lists on the node side, but the session still connects. If
     the session is up, that setting is not your problem.

### If a node was ever pointed at the fly.dev name — or the old relay.ssync.net name

A stored `meta_relay_url` wins over the shipped default —
forever. A node whose settings once carried
`wss://<app>.fly.dev` (from an early test, say), or the retired
`wss://relay.ssync.net` from before the rename, keeps dialing it:
it signs its v2 hello for that origin, the relay's
identity is `wss://relay.metasync.ssync.net`, and the hello is refused.
The node looks healthy in every other way; it just never gets a
v2 session. There is no UI field for this setting, so the fix
is by hand — and order matters: STOP the node first, then edit,
then start it. (A running node rewrites `settings.json` from its
in-memory settings whenever it saves, silently overwriting a
hand edit.) With the node stopped, in its state directory edit
`settings.json` — either delete the `meta_relay_url` key (the
node falls back to the shipped default, `wss://relay.metasync.ssync.net`)
or set it to `wss://relay.metasync.ssync.net` — then start the node.

### Day to day

- **Logs:** `fly logs`. By the no-storage rule these carry
  connection counts, error classes, and the aggregate counters
  line (queries routed, answers forwarded, bytes, active
  connections, share of queries answered) — never node ids, never
  content. If a log line ever shows more, that is a bug.
- **Restarts and deploys:** deploy with plain
  `fly deploy --ha=false`. Fly's default strategy (rolling)
  updates the existing machine IN PLACE: the process restarts on
  the same machine, presence empties, every socket drops, and
  nodes reconnect on their own (step 3 transport) — a brief
  blip, not an outage. Rolling creates no second machine; the
  strategies that ADD machines are `bluegreen` and `canary`,
  and neither is ever used here (two machines is a split
  network, each serving only its own nodes). Two rules ride
  every deploy:
  - `--ha=false` is on EVERY deploy command, not just the
    first. It only matters when a deploy creates machines — and
    if this machine is ever lost (destroyed, host failure), the
    next deploy starts from zero, where the high-availability
    default would create TWO.
  - **Escape hatch:** `fly deploy --ha=false --strategy
    immediate` skips flyctl's wait for the machine to report
    started. That wait is the ONLY difference between the two
    strategies here: with one machine updated in place and
    health checks deliberately unconfigured (the relay has no
    HTTP surface to check — see fly.toml), neither strategy
    keeps the old release running while the new one proves
    itself; the old process is gone before anything could
    check its replacement. A broken release goes live under
    rolling too — recovery is the rollback below, not the
    deploy strategy. Use immediate only when a normal deploy
    is stuck, never as the habit.
- **Rollback:** `fly releases` lists deploy images; redeploy the
  previous one with
  `fly deploy --ha=false --image <previous image ref>`
  (or check out the previous relay/ state and
  `fly deploy --ha=false`).
- **Cost shape:** the machine is most of the bill (a few dollars
  a month); at the design's message sizes, bandwidth is noise.
  The machine is sized `shared-cpu-1x` with 1GB — not for idle
  presence, which is nearly free, but for the relay's own
  send-queue caps at burst time (see the `[[vm]]` comment in
  fly.toml).

## Portal follow-up: attestation issuance and the revocation feed

Two portal-side pieces pair with this deployment. Both are now
BUILT in `src/sin/` (attestation issuance in `logic.ts`, the
exchange carriage and the revocation feed in `index.ts`), pending
deploy: migration `0012_meta_revocations.sql` must be applied
first, and the relay gains `RELAY_REVOCATIONS_URL` (below) at its
next deploy. Both consumers were finished and waiting, and this
section pins the contract the build followed. Nothing in the
relay or the node needs to change when these ship.

### One issuer, one key, one id

In v1 the pool is the only issuer: the SIN function signs access
tokens, binding attestations, and revocation lists with ONE
Ed25519 key — the one already in its environment as
`SIN_TOKEN_PRIVATE_KEY` (see `loadPoolTokenPrivateKey` in
`src/sin/logic.ts`). No new secret is needed. Its public half is
what nodes hold as `meta_issuer_public_key` and what the relay
takes as `RELAY_POOL_PUBLIC_KEY` (and, unset,
`RELAY_ISSUER_PUBLIC_KEY` falls back to it). SIN already serves
that public half: `GET /v1/auth/public-key` returns
`{"public_key": <base64url>}` — it is the source for both the
relay secret and the node's setting (runbook step 2).

The `issuer` string is the same in every signed object. No code
pins its value today (the conformance vectors use a synthetic
`"issuer-1"`). fly.toml adopts `RELAY_ISSUER_ID = "metasync"`;
the portal build must sign with exactly that string, and it must
never change afterwards — attestations and lists naming any other
issuer are dropped by verifiers holding this one.

### Attestation issuance (at the token exchange)

The node already consumes this: on every successful exchange it
looks for an `attestation` field in the response, verifies it
against the issuer key (shape, signature, and that it names THIS
node's key), and stores it for embedding in batch headers
(`node/metasync_node/pool.py`, `_note_attestation`; the pinned
format is `node/metasync_node/attestation.py`). An absent or
unverifiable attestation is dropped silently — a pool that does
not emit the field changes nothing for nodes, which is why this
can ship after the relay does. It gates STANDING (§9), not
connectivity.

The portal change, in `POST /v1/auth/exchange`
(`src/sin/index.ts`), on the existing 200 success response only
(never on the 202 binding-pending or any error response):

- Add `attestation` alongside the current fields
  (`access_token`, `token_type`, `expires_at`, `scopes`,
  `key_prefix`, `node_id`), only for a BOUND key:
  - Exchanges authenticated with ENV credentials (the admin and
    shared tokens) have no portal row and no `bound_at`: they
    receive NO attestation, and that path never earns standing —
    the operator's own admin node included. Do not define an
    attestation for that case; standing is a property of a
    portal-bound key with a binding time, and env credentials
    have neither.
  - `issuer`: the issuer id above.
  - `node_key`: base64url (unpadded) of the raw 32-byte Ed25519
    public key the key is bound to. REBUILD it from the decoded
    raw bytes — decode `bound_node_public_key` to the 32 bytes
    and re-encode canonically — never copy the stored column
    verbatim: that column holds the `X-Node-Public-Key` header
    string exactly as the node sent it at binding time,
    un-normalised, and the node checks `node_key` against its
    own key byte for byte.
  - `bound_at`: integer Unix epoch seconds DERIVED FROM
    `portal_keys.bound_at` (migration 0004) — the row stores a
    timestamptz, so the issuer converts it; the value on the
    wire is an int, never a timestamp string. This is not a
    style point: the node demands an int epoch here and
    silently drops an attestation carrying any other shape
    (`node/metasync_node/attestation.py`). It never moves
    across renewals — standing is computed from it — so always
    derive it from the row, never substitute the current time.
  - `issued_at`: now, Unix seconds. `expires`: `issued_at` +
    7 days (§9's lifetime; nodes accept windows up to 14 days).
  - `sig`: base64url (unpadded) Ed25519 signature, with the pool
    key, over exactly these bytes (six lines, each
    newline-terminated including the last):
    `metasync-attestation\n<issuer>\n<node_key>\n<bound_at>\n<issued_at>\n<expires>\n`
    `node/tests/test_meta_conformance.py` pins these bytes
    literally; the TypeScript issuer must reproduce them byte for
    byte and pin the same vectors in its own tests.
- Optionally, the same response may carry `revocations`: the
  current signed list object defined below. The node applies it
  to its shared revocation store at its sequence
  (`_note_revocations`). This is the design's primary delivery
  channel (§10: the exchange is the issuer's own endpoint, which
  bounds a withheld revocation's delay to one token lifetime).

### The revocation feed (the relay's copy)

The relay ALREADY fetches this (`RevocationFeed` in
`revocations.py`): a plain HTTPS GET with `Accept:
application/json`, a 10-second timeout, and a 4 MiB body cap,
every `RELAY_REVOCATIONS_INTERVAL_SECONDS` (default 300). The
node's enforcement mirrors the same shape. What is missing is
only an endpoint that serves the list.

- Path: the relay fetches whatever `RELAY_REVOCATIONS_URL`
  names. Built as proposed: `GET /v1/meta/revocations` on the SIN
  function; when it deploys, set `RELAY_REVOCATIONS_URL` to its
  full URL and redeploy. The exchange's 200 response also carries
  the same signed list (the optional carriage above), so nodes
  refresh at every exchange even before the relay is wired. The
  revoked set is recomputed from `portal_keys` on every serve: a
  node bound only on revoked keys, and a node id in
  `meta_seen_bindings` (migration 0012) that is no longer bound
  anywhere — unbinding clears `bound_*`, and account deletion
  cascades the key row away, so the seen record is the only place
  that id survives. The per-issuer sequence persists in
  `meta_revocation_state` and advances only when the served set
  changes.
- Response: 200 JSON, the BARE list object — no envelope:
  `{issuer, seq, issued_at, keys, sig}`.
  - `issuer`: the issuer id above. The relay is configured with
    it (`RELAY_ISSUER_ID`) and drops lists naming anyone else.
  - `seq`: per-issuer sequence, strictly increasing. The portal
    must PERSIST the counter and increment it on every change to
    the list; relays and nodes apply a list only when its `seq`
    exceeds the highest they have seen, so a reused or regressed
    sequence is silently ignored by design.
  - `issued_at`: Unix seconds the list was issued.
  - `keys`: the revoked node ids — each the lowercase hex
    SHA-256 of a node's public key (64 hex chars). Key status
    only: unbound and deleted keys belong on the list too
    (§10), never account information.
  - `sig`: base64url (unpadded) Ed25519 signature, with the pool
    key, over the canonical text (context line, issuer, seq,
    issued_at, then the sorted de-duplicated keys, one per
    line, newline-terminated): pinned byte for byte in
    `relay/metasync_relay/revocations.py` (`list_signing_text`),
    mirrored in `node/metasync_node/revocations.py`, with shared
    conformance vectors on both sides.
- A list that fails verification is treated as NO list (the
  holder keeps whatever it already had). An empty `keys` array
  is a valid, signable list.

### Known gap this does not close

Standing only works for a node whose `meta_issuer_public_key`
setting holds the pool public key — without it the node drops
attestations and lists (the relay still enforces its own verified
copy at hello). Today that setting is manual. Shipping a default
for it, or delivering the key through the pool connection, is a
node-side decision still owed before the closed beta leans on
standing; it is not part of the portal build above.

## Tests

```sh
pip install pytest
python -m pytest tests/
```

The suite runs the real server on an ephemeral port with in-test
Ed25519 keypairs, and covers the token mirror, the hello door (v1 and
v2), presence, routing, byte budgets, violation decay, re-auth,
revocation, aggregate counters, and the no-storage rule.

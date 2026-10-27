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

## Tests

```sh
pip install pytest
python -m pytest tests/
```

The suite runs the real server on an ephemeral port with in-test
Ed25519 keypairs, and covers the token mirror, the hello door (v1 and
v2), presence, routing, byte budgets, violation decay, re-auth,
revocation, aggregate counters, and the no-storage rule.

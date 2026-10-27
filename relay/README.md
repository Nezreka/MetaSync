# MetaSync metadata relay

The relay is the switchboard for the MetaSync metadata network
(DESIGN-METADATA.md section 10). Nodes hold persistent WebSocket
connections to it. A node sends a query, the relay forwards it to the
other connected nodes, and their answers travel back to the asker,
matched by query ID. SoulSync searches and gap-filling ride on this;
the claims themselves are the nodes' business, never the relay's.

## The no-storage rule

This is a specification, not a goal:

- The relay stores no metadata of any kind, in any table, at any time.
  No claims, no queries, no answers, no holdings.
- Presence (which node keys are connected) lives in process memory
  only. It is never written to disk or to a database. A restart empties
  it and nodes reconnect.
- Its only database access is one read-only SELECT at connect time:
  is this node's portal key valid and bound? The query names only the
  columns that yes/no needs and never selects the account column.
- Logs carry connection counts and error classes only — no node ids,
  in whole or in part. Query terms, subjects, and claim content are
  never logged (the operator rule is "routes and forgets", Q2).

## How a connection works

1. The node exchanges its pool key for a SIN access token as usual
   (PORTAL-DESIGN section 3A.2). The token must carry the `meta:read`
   scope.
2. The node opens a WebSocket to the relay. The relay speaks first:
   a `challenge` message carrying a fresh 32-byte random nonce,
   issued for this connection only. The node replies with one `hello`
   message: its node public key, its mode (`full`, `own_library`, or
   `leech`), the access token, a timestamp, the nonce echoed back,
   and an Ed25519 signature by the node key over the string
   `"metasync-relay-hello\n" + token + "\n" + timestamp + "\n" + nonce`.
   Binding the nonce means a captured hello cannot be replayed onto
   another connection.
3. The relay checks the echoed nonce against the one it issued (and
   spends it either way), verifies the token against the pool public
   key, checks that the token's node_id is the SHA-256 of the
   presented node key, verifies the hello signature, and peeks at
   `portal_keys`: the row
   must be valid (not revoked, not expired) and bound to that node.
   Every failure gets the same `refused` reply with reason class
   `auth`, then the connection closes. No detail leaks about which
   check failed.
4. On success the relay replies `welcome` with a coarse peer count
   (exact under 100, rounded above that) and the node is present.

After that, messages are `query`, `answer`, `done`, and `bye`, as
specified in DESIGN-METADATA.md section 10 and validated strictly by
`metasync_relay/protocol.py`. Answers are opaque to the relay: the
`batch` object is forwarded without its claims being parsed.

## Budgets and the answer window

Per node, enforced by the relay:

- queries per hour (default 600) and answers per hour (default 2000);
  every answer is charged, admitted or not, so junk is never free;
- an answer is admitted only for a query ID the relay actually routed,
  only back to the asker, and only inside the answer window (default
  6 seconds, closing early once every asked node has answered or
  signalled `done`) plus a grace period (default 10 seconds), during
  which late answers still reach the asker's cache;
- total admitted answer bytes per query are capped (default 8MB);
- unsolicited, late, self-directed, or over-cap answers are dropped
  and counted as violations; a connection past the violation limit
  (default 10) is closed.

Heartbeat is the WebSocket ping/pong from the `websockets` library.
A dead connection drops out of presence when it closes.

## Configuration

Environment only. The relay refuses to start when a required variable
is missing or malformed, and says which one (never its value).

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | none | Postgres URL for the connect-time key peek (read-only use) |
| `RELAY_POOL_PUBLIC_KEY` | yes | none | Pool Ed25519 public key: base64url of the raw 32 bytes, or PEM |
| `RELAY_HOST` | no | `127.0.0.1` | Bind address (the Dockerfile sets `0.0.0.0`) |
| `RELAY_PORT` | no | `8791` | Bind port |
| `RELAY_QUERIES_PER_HOUR` | no | `600` | Per-node query budget |
| `RELAY_ANSWERS_PER_HOUR` | no | `2000` | Per-node answer budget |
| `RELAY_ANSWER_CAP_BYTES` | no | `8388608` | Admitted answer bytes per query |
| `RELAY_WINDOW_SECONDS` | no | `6` | Answer window length |
| `RELAY_GRACE_SECONDS` | no | `10` | Post-window grace for late answers |
| `RELAY_SEND_TIMEOUT_SECONDS` | no | `5` | Per-recipient timeout on query fan-out sends |
| `RELAY_MAX_VIOLATIONS` | no | `10` | Violations before the relay closes a connection |

## Running it

Local:

```sh
pip install -r requirements.txt
DATABASE_URL=postgres://... RELAY_POOL_PUBLIC_KEY=... python -m metasync_relay
```

Docker:

```sh
docker build -t metasync-relay .
docker run -e DATABASE_URL=postgres://... -e RELAY_POOL_PUBLIC_KEY=... -p 8791:8791 metasync-relay
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

The suite runs the real server on an ephemeral port with a fake key
store and in-test Ed25519 keypairs, and covers the token mirror, the
hello door, presence, routing, budgets, and the no-storage rule.

## What connects next

Step 3 of the build order (DESIGN-METADATA.md section 13) adds the
node side: a persistent client in the node's networking layer that
keeps this connection alive with reconnect and heartbeat, and speaks
hello/query/answer/done exactly as validated here.

# MetaSync node

The program that runs alongside a user's SoulSync. Python + Flask, like
SoulSync itself. Design: [../DESIGN-NODE.md](../DESIGN-NODE.md).

## Run it (bare metal)

```bash
cd node
pip install -r requirements.txt
METASYNC_STATE_DIR=./data python3 run.py          # UI + scheduler
```

Open http://localhost:8790 — first run asks you to set an admin password.
Then connect your SoulSync and, optionally, join a pool. Other one-shot
commands: `python3 run.py preview|report|refresh`.

## Run it (Docker)

See `docker-compose.example.yml` — MetaSync sits next to SoulSync, its UI
on **8790** (SoulSync stays on 8000), state in its own volume.

## Connect SoulSync — two paths

MetaSync talks to SoulSync with SoulSync's URL + a SoulSync `sk_` API
key. SoulSync talks back to MetaSync with MetaSync's own local API key.
The local key is shown once in MetaSync's **SoulSync connection** area;
only its SHA-256 hash is stored.

1. **Manual copy-paste:** paste the SoulSync `sk_` key into MetaSync, and
   paste MetaSync's local key into SoulSync's MetaSync settings.
2. **One-click pairing:** SoulSync calls `POST /local/pair/request`,
   MetaSync shows the returned 6-digit code on its dashboard for a
   person to approve, then SoulSync calls `POST /local/pair/claim` with
   the same code and its `sk_` key. The claim returns MetaSync's local
   key exactly once. Codes are single-use and expire after 10 minutes.

Authenticated local endpoints (Bearer = MetaSync local key):

- `POST /local/handoff/pool-key` — SoulSync hands over `{pool_url,
  pool_api_key}`; MetaSync stores it and attempts the pool exchange.
- `GET /local/status` — connection, binding, token and report status.
- `POST /local/disconnect` — clear the stored pool key + access token.

## Pool authentication

The long-lived pool key is presented only to
`POST /v1/auth/exchange`, together with this node's Ed25519 request
signature (derived from its persistent root identity). Routine report,
index, playlist and whoami calls use the short-lived returned token plus
a fresh node signature. If the pool answers the exchange with **202**,
the node is waiting for binding approval in the portal and sends no
report. A pool that 404s the exchange is used only in a clearly labelled
legacy direct-key development fallback.

## Defaults that matter

- **Dry run is ON** until you turn it off: the node computes the exact
  weekly report, shows it on the Payload preview page, and sends/syncs
  nothing.
- Weekly report, daily playlist refresh.
- User nodes only push their report and pull pool data; publishing the
  shared playlist data is reserved for the official MetaSync
  (see DESIGN-NODE.md §2 and §9).

## Tests

```bash
python3 -m unittest discover -s tests -v   # all against localhost mocks
```

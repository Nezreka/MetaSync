# MetaSync

A peer-to-peer music metadata network built alongside
[SoulSync](https://github.com/Broque-N-Roll/SoulSync).

Music metadata is not scarce — MusicBrainz, Discogs and Wikidata are open. What
is scarce is the **resolved cross-provider id graph**: the mapping that says
this artist on Spotify is that artist on Deezer is that one on Tidal. No open
dataset holds it. Every install rebuilds it from scratch, one rate-limited
fuzzy search at a time, and each of those searches is a *decision* that can be
wrong.

SoulSync produces that graph as a byproduct of enriching a real library. Across
thousands of installs, collectively, it covers most music that anyone actually
holds. MetaSync is the layer that lets those installs pool the graph — without
pooling anything else.

## Status

Early. A node can build and store its own claims; there is no network yet.

- [x] SoulID port + conformance tests against the real SoulSync worker
- [x] Canonical encoding (MCJ/1), Ed25519 node identity, signed claims
- [x] SoulSync export endpoint ([spec](docs/soulsync-export-spec.md); landed in SoulSync)
- [x] Importer + local claim store + CLI
- [x] Claim packs — sign, export, verify, merge (the network, by hand)
- [ ] Serving claims back to SoulSync as a metadata source
- [ ] Peer transport

## Usage

```bash
cp .env.example .env        # then paste a SoulSync API key into it
python3 -m metasync sync    # walk the library, build claims (resumable)
python3 -m metasync stats
python3 -m metasync show track soul_0dd312db592d67a3
python3 -m metasync pack --out mylibrary.pack   # share what this node observed
python3 -m metasync import theirs.pack          # verify and merge someone else's
python3 -m metasync verify                      # re-check every stored signature
```

A pack is gzipped JSON Lines: a header, then one signed claim per line. It is
verified claim by claim on import, so a pack from a stranger needs no more trust
than a pack from a friend. Relaying is opt-in (`--relay`), and a relayed claim
keeps its original signature — a pack passing through ten hands still counts as
one observation.

## Design commitments

**Claims are self-verifying.** A claim's key is a pure hash of the normalized
name parts the claim carries. Every receiver re-derives it. A peer cannot bind a
payload to a key it did not legitimately derive.

**Claims are never re-signed.** A relayed claim keeps its original observer's
signature. Corroboration is a count of *distinct observers*, not of copies —
without this, the network would echo its own output back as consensus and the
whole "thousands of people really touched this music" premise would collapse
into one confident node repeated a thousand times.

**Only independent observations are published.** A node signs a claim only for
ids its own enrichment resolved. Ids MetaSync itself supplied are never
re-exported.

**The export is an allowlist.** `metasync/schema.py` enumerates every field that
may travel. A column added to SoulSync's `tracks` table next year cannot
silently join the export. Lyrics, bios, file paths, media-server URLs, listening
history, and anything in SoulSync's config/credential tables are out — most of
it is also re-fetchable from the provider once a peer has the id.

See [docs/soulid.md](docs/soulid.md) for why artist keys need care and album and
track keys do not.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The SoulID conformance tests import SoulSync directly from a sibling checkout
(`../../SoulSync`) and skip if it is absent.

# SoulID as a network key

MetaSync keys every claim on a SoulSync SoulID. This is what the key actually
is, verified against `core/soulid_worker.py` (SoulSync v3.2.2 dev, Aug 2026) —
not what the docstrings say, which is stale in one important place.

## Derivation

`generate_soul_id(*parts)` = `soul_` + first 16 hex chars of
`sha256(''.join(normalize(p) for p in parts if p))`.

`normalize()` lowercases, strips accents (NFD, drop combining marks), removes
parenthetical/bracketed segments, and deletes everything outside `[a-z0-9]`.
It is idempotent, which is what lets a claim carry pre-normalized parts and
still re-derive its own key.

| Key | Parts | Cross-node deterministic? |
|---|---|---|
| album (`albums.soul_id`) | artist, album | **Yes** — local fields only |
| track, song-level (`tracks.soul_id`) | artist, title | **Yes** — local fields only |
| track, release-level (`tracks.album_soul_id`) | artist, album, title | **Yes** — local fields only |
| artist (`artists.soul_id`) | artist, *qualifier* | **Only on the canonical path** |

## The artist key problem

The module docstring and `SOULSYNC_DB_MAP.md` §4c both describe the artist key
as `normalize(name) + debut_year`. The code does something else
(`soulid_worker.py:242-263`), with three paths:

1. **canonical** — `name + max(deezer_id, itunes_id)`, from a live lookup that
   searches `"<artist name> <track title>"` on both services.
2. **album fallback** — `name + alphabetically-first album title in this
   library`, used when the lookup returns nothing.
3. **name only** — when the artist has no albums.

Only path 1 is reproducible across installs, and even it is not airtight: the
verification track is drawn from the local library, so two nodes issue different
search queries, and `max()` over one id differs from `max()` over two if a
service times out for one of them.

Path 2 is **library-dependent by construction** — the answer to "which album
sorts first" depends on what that user owns. Two nodes holding the same artist
routinely compute different keys.

There is also precedent for the scheme changing under you: the
`soulid_artist_version` flag in the `metadata` table (currently
`debut_year_api_v2`) exists because a previous scheme change **reset every
artist soul ID in the database**.

### What MetaSync does about it

- Every artist claim declares its `key_path`. Claims are structurally valid on
  any path, but `Claim.is_cross_node_key` is false for `album` and `name`, and
  those keys are never published or matched on as rendezvous points.
- Artist identity on the network rests on the provider ids in the payload
  (Deezer, MusicBrainz, Spotify), not on the artist key.
- Album and track claims carry no such caveat and are the backbone of the
  network.

## Collisions are a feature, not a conflict

Because `normalize()` strips parentheticals, the deluxe and standard editions of
an album share one album key, and every remaster of a song shares one song key.
So a single key legitimately carries **several correct provider ids at once**.
This is why `ids` maps a provider to a *set*, never a single value: a claim that
Spotify id A and Spotify id B both live under this key is usually two true
statements, not a disagreement to resolve.

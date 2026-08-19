# SoulSync: MetaSync export endpoint

**Audience:** whoever is working in the SoulSync repo.
**Scope:** additive, read-only. New blueprint + one DB method + a few serializer lines.
No schema changes (except one optional item at the end), no worker changes, no
behaviour change to any existing endpoint.

---

## Why

MetaSync is a peer-to-peer metadata network being built as a SoulSync sidecar.
It reads a user's resolved metadata over SoulSync's public API, packages it into
signed claims, and trades those with peers — so that entity resolution one
install paid thousands of API calls for doesn't have to be redone by every other
install. MetaSync never touches `music_library.db` directly and never writes.

Two things block it today:

1. **`soul_id` is not exposed anywhere in `api/`.** It is the cross-install
   identity the entire network keys on. Without it there is nothing to key on.
2. **There is no way to page through the library.** The existing endpoints are
   search-shaped (`?title=&artist=`, capped at 200, offset-based). MetaSync needs
   to walk every artist/album/track once, then fetch only what changed since.

---

## Task 1 — expose `soul_id` in the serializers

`api/serializers.py`. Add to the existing dicts, alongside the other external IDs:

- `serialize_artist` → `"soul_id": d.get("soul_id")`
- `serialize_album` → `"soul_id": d.get("soul_id")`
- `serialize_track` → `"soul_id": d.get("soul_id")`, `"album_soul_id": d.get("album_soul_id")`

Nothing else in those functions changes.

---

## Task 2 — `MusicDatabase.api_export_entities()`

Add to `database/music_database.py`, near the other `api_*` methods
(`api_search_tracks` is at ~8605; `api_list_albums` at ~17859 — follow whichever
is closest in style).

```python
def api_export_entities(self, entity: str, cursor: str = "",
                        since: str = "", limit: int = 500) -> List[Dict[str, Any]]:
```

- `entity` is `"artist"`, `"album"`, or `"track"`. Reject anything else.
- Keyset pagination: `WHERE id > :cursor ORDER BY id ASC LIMIT :limit`.
  Do **not** use OFFSET — the tables are large and rows shift under enrichment
  workers, so OFFSET both skips and duplicates rows mid-walk.
- `since` (optional, ISO-8601): adds `AND updated_at >= :since` for incremental
  syncs.
- **Select columns explicitly by name** — never `SELECT *`. Column *order*
  differs between a fresh install and an upgraded one.
- Read every value through the existing `_row_value()` helper. Upgraded DBs can
  lag the code by a migration; a missing column must yield `None`, not an
  `IndexError` that fails the whole export.
- Tracks and albums need the joined artist name:
  ```sql
  -- tracks
  SELECT ... FROM tracks t
  JOIN artists ar ON ar.id = t.artist_id
  LEFT JOIN albums al ON al.id = t.album_id
  ```
  Return it as `artist_name`, and the album title as `album_title`.

  **Use `artists.name`, not `tracks.track_artist`.** `soulid_worker._process_tracks`
  (line ~553) computes the soul_id from the joined `ar.name`, so MetaSync must
  normalize that exact string or it will derive a different key than the one
  stored in the row.

### Rows to skip

Skip any row where `soul_id` is NULL, empty, or **starts with `soul_unnamed_`**.

That fallback (`soulid_worker.py:263` and `:580`) embeds the library primary key
— a Plex ratingKey or Jellyfin GUID — into the id. Those are install-local and
must never leave the box. MetaSync rejects them on ingest too, but they should
not be served in the first place.

---

## Task 3 — `api/metasync.py`

New module, same shape as the other sub-modules (`register_routes(bp)`), wired
into `api/__init__.py::create_api_blueprint()` exactly like `reg_library`.

```
GET /api/v1/metasync/export
```

Query params:

| param | default | notes |
|---|---|---|
| `entity` | — | required; `artist` \| `album` \| `track` |
| `cursor` | `""` | opaque; echo back `next_cursor` from the previous page |
| `since` | `""` | ISO-8601; only rows with `updated_at >= since` |
| `limit` | 500 | clamp to 1..1000 |

Auth: `@require_api_key`, same as every other route. It inherits the blueprint's
60/min rate limit; that is fine — a 50k-track library is ~100 requests.

Response, using the standard `api_success` envelope:

```json
{
  "success": true,
  "data": {
    "entity": "track",
    "items": [ ... ],
    "next_cursor": "…",
    "has_more": true
  },
  "error": null,
  "pagination": null
}
```

`next_cursor` is the last row's primary key, base64-encoded so callers treat it
as opaque. It stays between MetaSync and SoulSync on localhost and is never
published to peers. `has_more` is `len(items) == limit`.

Errors: `api_error("BAD_REQUEST", ..., 400)` for a bad `entity` or malformed
`since`; `api_error("EXPORT_ERROR", str(e), 500)` on failure.

### Serializers — allowlist, not blocklist

Add `serialize_metasync_artist/album/track` to `api/serializers.py`. **Do not
reuse `serialize_track`** — it returns `file_path`, `genius_lyrics`, and
`genius_description`, none of which may go to MetaSync.

Build each dict by naming the permitted fields explicitly, so that a column
added to `tracks` next year cannot silently join the export.

**artist**
```
soul_id, name, genres, updated_at
musicbrainz_id, spotify_artist_id, itunes_artist_id, deezer_id, discogs_id,
amazon_id, tidal_id, qobuz_id, audiodb_id, genius_id, jiosaavn_id
<provider>_match_status for each of the above
```

**album**
```
soul_id, title, artist_name, year, release_date, track_count, record_type,
label, genres, updated_at
musicbrainz_release_id, spotify_album_id, itunes_album_id, deezer_id,
discogs_id, amazon_id, tidal_id, qobuz_id, audiodb_id, jiosaavn_id,
bandcamp_url, upc
canonical_source, canonical_album_id, canonical_score
<provider>_match_status for each of the above
```

**track**
```
soul_id, album_soul_id, title, artist_name, album_title,
track_number, disc_number, duration, bpm, explicit, year, isrc, updated_at
musicbrainz_recording_id, spotify_track_id, itunes_track_id, deezer_id,
amazon_id, tidal_id, qobuz_id, audiodb_id, genius_id, jiosaavn_id, bandcamp_url
<provider>_match_status for each of the above
```

The `*_match_status` values are load-bearing, not optional — MetaSync only
publishes ids whose status is `'matched'`, and drops the rest.

### Must NOT appear in any export row

| Excluded | Why |
|---|---|
| `id`, `artist_id`, `album_id` | media-server primary keys; install-local and meaningless off-box |
| `file_path`, `file_size`, `bitrate` | local filesystem, reveals the user's library layout |
| `thumb_url`, `banner_url` | media-server URLs leak the server's address/paths |
| `genius_lyrics`, `genius_description`, `summary`, `discogs_bio`, `lastfm_wiki`, `alt_names`, `aliases` | publisher/provider-owned prose; re-fetchable from the provider once a peer has the id |
| `play_count`, `last_played` | listening behaviour |
| `server_source`, `verification_status`, `repair_*`, `quality_profile_id` | local state, no meaning to a peer |
| anything from `metadata`, `profiles`, `service_credentials` | config, tokens, credentials |

The exclusion of `soul_id` from the *ids* list is deliberate too: `soul_id` is
`source_ids`' `hydrabase` column, but on the network it is the **key**, never a
claimable payload id. Serving it as both would let network-sourced identity be
laundered back in as a local observation.

---

## Task 4 (optional, recommended) — persist the artist soul_id derivation path

Only relevant to artist claims. Album and track claims work fine without it.

`soulid_worker._process_artists` (lines 242-263) derives an artist soul_id three
different ways and keeps no record of which one it used:

1. `name + max(deezer_id, itunes_id)` from the canonical lookup
2. fallback: `name + alphabetically-first album title **in this library**`
3. fallback: `name` alone

Only (1) is reproducible on another install — (2) depends on what that user
happens to own, so two nodes holding the same artist compute different keys.
MetaSync cannot tell them apart from the outside, so today it simply will not
publish artist claims at all.

The fix is one nullable column, following the established
`PRAGMA table_info` → `ALTER TABLE ADD COLUMN` pattern in `_initialize_database`:

```sql
ALTER TABLE artists ADD COLUMN soul_id_path TEXT   -- 'canonical' | 'album' | 'name'
```

Set it in `_process_artists` on the branch that already knows which path it took,
and include it in the artist export. Existing rows stay NULL and are treated as
unknown (not published), which is the safe default.

Worth doing on its own merits — right now nothing in SoulSync records how much
to trust a given artist soul_id.

---

## Conventions to follow

- Use `get_database()`; do not open a new connection. WAL means the read will
  not block the enrichment workers.
- Read-only. No INSERT/UPDATE/DELETE anywhere in this change.
- `api/library.py` is the closest existing model for both the route and the DB
  method; match its structure and error handling.

## Tests

Add to the existing API test suite:

- pagination walks a seeded library exactly once — no duplicates, no gaps, and
  it terminates
- `since` filters correctly
- rows with NULL, empty, or `soul_unnamed_*` soul_ids are omitted
- **the excluded-fields table above is asserted key by key** — the important
  test; it is what stops a future column from leaking into the export
- a missing column (simulating an upgraded DB that lags a migration) yields
  `None` rather than raising
- `entity=bogus` → 400; missing API key → 401

## Acceptance

```bash
curl -s -H "Authorization: Bearer $KEY" \
  "http://localhost:8008/api/v1/metasync/export?entity=track&limit=2" | jq
```

returns two track rows, each with a `soul_id` and `album_soul_id`, provider ids
with their match statuses, and **no** `id`, `file_path`, or `genius_lyrics`.

---

## Addendum — notes from reviewing the implementation (2026-08-19)

The endpoint was implemented as specified and its 22 tests pass. Two things
found during review that belong on the MetaSync side, recorded here so the
context is not lost:

1. **`duration` is milliseconds.** `tracks.duration` is INTEGER milliseconds
   (schema comment at `music_database.py:368`; `api_get_playlist_tracks` at
   :8495 converts to seconds for the same reason). MetaSync's wire format
   carries **seconds** — providers disagree constantly at millisecond
   granularity, and comparing raw ms across nodes would manufacture
   disagreement out of rounding noise. The importer divides by 1000.
   No change needed to the endpoint.

2. **`canonical_score` is a float.** MetaSync's canonical encoding (MCJ/1)
   rejects floats outright, because `1.0` vs `1` vs `1.0000000000000002` is the
   classic way signature verification breaks across languages. It is not in the
   claim schema today. If the canonical-version pin is ever put on the wire, the
   score has to be scaled to an integer first. Again, no change needed here —
   the endpoint serving it as a float is correct for an HTTP API.

Also noted: `api_export_entities` LEFT JOINs artists for albums, while
`soulid_worker._process_albums` INNER JOINs them. Any album carrying a soul_id
therefore had an artist when the id was computed, so `artist_name` should never
be NULL in practice — but MetaSync skips rows where it is, since without it the
key cannot be re-derived.

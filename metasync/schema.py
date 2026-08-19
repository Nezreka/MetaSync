"""What a claim is allowed to contain — an allowlist, never a blocklist.

Every field MetaSync puts on the wire is enumerated here. A column added to
SoulSync's `tracks` table next year cannot silently join the export, because
nothing is exported unless it appears below.

Excluded on purpose, and why:
  file_path, file_size, bitrate  local filesystem / user's own copy
  thumb_url, banner_url          media-server URLs leak the user's server layout
  genius_lyrics, *_bio, summary, publisher-owned prose; re-fetchable from the
  *_description, *_wiki          provider once a peer has the id
  play_count, last_played        listening behaviour
  anything from `metadata`,      config, tokens, credentials
  `profiles`, `service_credentials`
"""

from __future__ import annotations

ENTITIES = ("artist", "album", "track")

# Providers whose ids may be claimed, per entity. Mirrors the canonical columns
# in SoulSync's core/source_ids.py, minus `hydrabase` — its column IS soul_id,
# which is the claim key, not a payload id.
ID_PROVIDERS = {
    "artist": frozenset({
        "spotify", "itunes", "deezer", "musicbrainz", "discogs",
        "amazon", "tidal", "qobuz", "audiodb", "genius", "jiosaavn",
    }),
    "album": frozenset({
        "spotify", "itunes", "deezer", "musicbrainz", "discogs",
        "amazon", "tidal", "qobuz", "audiodb", "jiosaavn", "bandcamp",
        "upc",   # not a provider — a standards-body identifier, strongest anchor
    }),
    "track": frozenset({
        "spotify", "itunes", "deezer", "musicbrainz",
        "amazon", "tidal", "qobuz", "audiodb", "genius", "jiosaavn", "bandcamp",
        "isrc",  # ditto
    }),
}

# Identifiers issued by a standards body rather than resolved by a fuzzy match.
# They are worth more in conflict resolution than any provider id.
STRONG_IDS = frozenset({"isrc", "upc", "musicbrainz"})

# Structured, factual, provider-agnostic fields. Types are enforced; `str` fields
# are length-capped. Nothing free-text or prose-shaped belongs here.
FACT_FIELDS = {
    "artist": {
        "genres": ("strlist", 24),
    },
    "album": {
        "year": ("int", (1000, 2200)),
        "release_date": ("date", None),
        "track_count": ("int", (0, 2000)),
        "record_type": ("str", 32),
        "label": ("str", 200),
        "genres": ("strlist", 24),
    },
    "track": {
        # SECONDS. SoulSync's `tracks.duration` is MILLISECONDS (see the schema
        # comment at music_database.py:368) — the importer divides by 1000.
        # Seconds is the wire unit on purpose: providers disagree at the
        # millisecond level constantly, and comparing raw ms across nodes would
        # manufacture disagreement out of rounding noise.
        "duration": ("int", (0, 86400)),
        "track_number": ("int", (0, 2000)),
        "disc_number": ("int", (0, 200)),
        "bpm": ("int", (0, 400)),
        "explicit": ("bool", None),
        "year": ("int", (1000, 2200)),
    },
}

# Normalized name parts a claim carries so its key can be re-derived locally.
NORM_FIELDS = {
    "artist": ("artist", "qualifier"),   # qualifier: canonical id or album title
    "album": ("artist", "album"),
    "track": ("artist", "album", "title"),
}

REQUIRED_NORM = {
    "artist": ("artist",),
    "album": ("artist", "album"),
    "track": ("artist", "album", "title"),
}

# Structural limits — a claim is a fact about one entity, not a payload channel.
MAX_IDS_PER_PROVIDER = 8
MAX_ID_LENGTH = 256
MAX_NORM_LENGTH = 256
MAX_GENRES = 24
MAX_GENRE_LENGTH = 64

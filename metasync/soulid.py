"""SoulID derivation — a byte-exact port of SoulSync's ``core/soulid_worker``.

MetaSync must compute soul IDs itself, never trust a peer's. Because a soul ID
is a pure function of normalized name parts, any node can recompute a claim's
key from the normalized parts the claim carries and reject it if they disagree.
That makes keys self-verifying: a peer cannot bind arbitrary metadata to a key
it did not legitimately derive.

Ported rather than imported on purpose — MetaSync has no dependency on the
SoulSync codebase, only on its HTTP API. ``tests/test_soulid.py`` diffs this
implementation against the real one when a SoulSync checkout is present.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

# Derivation paths for an artist key, in the order soulid_worker tries them.
# Only CANONICAL is reproducible across installs: the ALBUM fallback hashes the
# alphabetically-first album in *that user's* library, and NAME collides every
# artist sharing a name. See docs/soulid.md.
ARTIST_PATH_CANONICAL = "canonical"   # normalize(name) + max(deezer_id, itunes_id)
ARTIST_PATH_ALBUM = "album"           # normalize(name) + normalize(first album title)
ARTIST_PATH_NAME = "name"             # normalize(name)

ARTIST_PATHS = (ARTIST_PATH_CANONICAL, ARTIST_PATH_ALBUM, ARTIST_PATH_NAME)

# Only artist keys derived this way may be used as a network rendezvous key.
CROSS_NODE_ARTIST_PATHS = frozenset({ARTIST_PATH_CANONICAL})

_PARENTHETICAL_RE = re.compile(r'\s*[\(\[][^)\]]*[\)\]]')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')


def normalize_for_soul_id(text: str) -> str:
    """Lowercase, strip accents, drop parentheticals, keep only [a-z0-9].

    Idempotent: normalizing an already-normalized string is a no-op, which is
    what lets a claim carry pre-normalized parts and still re-derive its key.
    """
    if not text:
        return ''
    s = text.lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = _PARENTHETICAL_RE.sub('', s)
    return _NON_ALNUM_RE.sub('', s)


def generate_soul_id(*parts: str) -> str:
    """``soul_`` + first 16 hex chars of SHA-256 over the concatenated parts."""
    combined = ''.join(normalize_for_soul_id(p) for p in parts if p)
    if not combined:
        return ''
    return f'soul_{hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]}'


def artist_key(name: str, qualifier: Optional[str] = None) -> str:
    """Artist soul ID. ``qualifier`` is the canonical provider id or album title."""
    return generate_soul_id(name, qualifier) if qualifier else generate_soul_id(name)


def album_key(artist: str, album: str) -> str:
    """Album soul ID. Deterministic across installs — local fields only."""
    return generate_soul_id(artist, album)


def song_key(artist: str, title: str) -> str:
    """Song-level track soul ID: links every version of a song across releases."""
    return generate_soul_id(artist, title)


def release_track_key(artist: str, album: str, title: str) -> str:
    """Release-level track soul ID (``tracks.album_soul_id``) — one specific
    track on one specific release. This is what provider track ids identify, so
    it is MetaSync's primary key for track claims."""
    return generate_soul_id(artist, album, title)

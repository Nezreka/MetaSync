"""Verification — DESIGN-METADATA.md §7/§8.

Claims arrive unverified. This engine checks them against the
providers themselves and writes VERDICTS (claim_verdicts, keyed by
claim identity) that the merge turns into tiers. The rules that
matter:

- STRONG verification is a cross-match where BOTH sides come from
  provider responses, never from the claim: two providers' answers
  agree on key material (an ISRC both report, a MusicBrainz
  response linking to the very Deezer id the Deezer response
  carries, a shared MBID), and both answers' titles/artists agree
  with the subject's name claim. A single provider response that
  LINKS the subject's anchor id to the claimed id (MusicBrainz
  url-rels) is the design's other strong form (§7's own example).
- PLAUSIBLE is name+artist+duration agreement with ONE provider
  response and no key cross-match. It never marks strong and
  never promotes — live/remaster/re-record pairs pass it while
  being the wrong item.
- Duration ±2 s only ever SUPPORTS: a duration claim goes strong
  only when its recording's identity was cross-matched on key
  material AND a provider response reports a duration within 2 s.
- Fetcher etiquette (§7.1): fetchers are rate-limited, every
  response is cached in the store for 90 days (a verified id is
  never re-fetched inside that window outside §6's
  re-verification-on-reliance and §8's bounded sanity rule), and
  absence needs positive evidence — one provider's 404 is
  "unreachable", not "deleted"; only two independent lookup paths
  agreeing on not-found count as absence, and even then v1 clears
  verdicts and records the failed check rather than deleting
  another node's cached claims.
- Conflict-triggered re-verification (§8's sanity rule) is
  bounded: at most one trigger per origin per field per 24 h, and
  an origin with no verified history (no passed check on record)
  cannot trigger re-fetches at all. Verifier.note_conflict is
  that gate; the querier calls it when a merged view disputes.

Fetchers sit behind one interface. v1 ships MusicBrainz (public
API) and Deezer (public lookup, no auth). Spotify is an explicit
not-configured stub: the credential route is an open sub-decision
(DESIGN §13 step 6), and an honest stub beats an invented one.
Tidal / iTunes / Discogs / Hydrabase claims stay unverifiable in
v1 (§7) — no fetchers, no verdicts.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from ..musicbrainz import Pacer as _Pacer
from . import merge as meta_merge
from . import soulid as _soulid
from .store import MetaStore, value_key

FETCH_FRESH_S = 90 * 24 * 3600
DURATION_TOLERANCE_MS = 2000

# §7 verification budget (gate SF-6): verification is queued
# work, not an unbounded reflex. One run (a verify_subjects call)
# may spend at most MAX_FETCHES_PER_RUN network fetches, and one
# subject at most MAX_FETCHES_PER_SUBJECT — cache hits are free,
# so the caps bind only what actually goes out. Subjects are
# scheduled best-evidence-first (verify_subjects ranks by §9
# reputation, standing origins first), so a flooded queue spends
# its budget on the claims most worth checking. A lookup that
# fails (transport, 5xx, throttling) is NEGATIVE-CACHED for
# UNREACHABLE_BACKOFF_S: a dark provider is not re-asked on every
# ask, and the marker is neither a response nor an absence proof.
# Within a subject, lookups are additionally ordered by the
# asserting origin's standing, then its local reputation, and
# capped per origin (gate r2 SF-4): one origin asserting forty
# ids that sort first must neither starve the honest ids behind
# them nor spend the whole subject budget on its own claims —
# an honest origin rarely asserts more than a few ids per
# subject, so the cap binds floods, not libraries.
MAX_FETCHES_PER_RUN = 200
MAX_FETCHES_PER_SUBJECT = 40
MAX_FETCHES_PER_ORIGIN = 10
UNREACHABLE_BACKOFF_S = 3600

# Which provider lookups an id claim feeds, per subject kind —
# how a lookup knows which origins it spends (the per-origin cap
# above) — field -> ((provider, method), ...).
_LOOKUP_SOURCES: dict[str, dict[str, tuple]] = {
    "track": {
        "recording_mbid": (("musicbrainz", "track_by_id"),),
        "deezer_id": (("deezer", "track_by_id"),),
        "isrc": (("musicbrainz", "track_by_isrc"),
                 ("deezer", "track_by_isrc")),
    },
    "album": {
        "release_group_mbid": (("musicbrainz", "album_by_id"),),
        "deezer_id": (("deezer", "album_by_id"),),
    },
    "artist": {
        "artist_mbid": (("musicbrainz", "artist_by_id"),),
        "deezer_id": (("deezer", "artist_by_id"),),
    },
}

# Fields §7 can ever write a verdict for. Claims in any other
# field (name, genres, display_name, the ids of providers with no
# fetcher — tidal/itunes/discogs/hydrabase) never receive
# verdicts; the querier's needs-verify test keys off this set so
# such claims don't keep a subject scheduled forever.
VERIFIABLE_FIELDS = frozenset({
    "isrc", "deezer_id", "spotify_id", "recording_mbid",
    "release_group_mbid", "artist_mbid", "duration_ms", "year",
    "provider_link", "album_track",
})

TIER_STRONG = "strong-verified"
TIER_PLAUSIBLE = "plausible"

_NOT_FOUND = object()
_UNREACHABLE = object()


class FetcherError(Exception):
    """A provider lookup failed in a way that says nothing about
    the entity (transport, 5xx, throttling): unreachable, never
    evidence of absence."""


class FetcherNotConfigured(FetcherError):
    """This provider has no configured fetcher in v1."""


# Pacing is musicbrainz.Pacer (imported above as _Pacer): ONE
# instance per node, shared with the musicbrainz.py resolver, so
# the node's aggregate MusicBrainz rate honors the ~1 req/s
# etiquette (R2-SF-6). The Deezer fetcher keeps a private pacer —
# a different provider, a different budget.


def _provider_id_from_url(url: str) -> tuple[str, str] | None:
    """(provider, external id) from a provider URL — how a
    MusicBrainz url-rel becomes a link claim's other side."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if host.endswith("open.spotify.com") and len(parts) >= 2:
        return "spotify", parts[1]
    if host.endswith("deezer.com") and len(parts) >= 2:
        return "deezer", parts[1]
    if host.endswith("tidal.com") and len(parts) >= 2:
        return "tidal", parts[-1]
    if host.endswith("musicbrainz.org") and len(parts) >= 2:
        return "musicbrainz", parts[1]
    if host.endswith("discogs.com") and len(parts) >= 2:
        return "discogs", parts[-1].split("-")[0]
    return None


def _links_from_url_rels(rels: Any) -> dict[str, str]:
    links: dict[str, str] = {}
    if not isinstance(rels, list):
        return links
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        url_obj = rel.get("url")
        resource = url_obj.get("resource") if isinstance(url_obj, dict) \
            else None
        if isinstance(resource, str):
            found = _provider_id_from_url(resource)
            if found is not None:
                links.setdefault(found[0], found[1])
    return links


def _artist_credit_name(credits: Any) -> str:
    if not isinstance(credits, list):
        return ""
    names = []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or (credit.get("artist") or {}).get(
                "name")
            if name:
                names.append(str(name))
    return ", ".join(names)


def _norm(text: Any) -> str:
    return _soulid.normalize_for_soul_id(str(text or ""))


def _isrc_set(resp: dict[str, Any]) -> set[str]:
    """Every ISRC a provider response asserts: the full set when
    the fetcher carried one (R2-SF-3), else the single value."""
    out = {str(i) for i in (resp.get("isrcs") or []) if i}
    if resp.get("isrc"):
        out.add(str(resp["isrc"]))
    return out


def tracklist_fingerprint(tracks: list[dict[str, Any]]) -> str:
    """§7's variant fingerprint: a hash of the ordered (disc,
    position, normalized title) sequence. Two tracklists with the
    same fingerprint are the same variant, whichever provider (or
    claim set) they came from."""
    sequence = "\n".join(
        f"{t.get('disc')}:{t.get('position')}:{_norm(t.get('title'))}"
        for t in sorted(tracks,
                        key=lambda t: (t.get("disc") or 0,
                                       t.get("position") or 0)))
    return hashlib.sha256(sequence.encode("utf-8")).hexdigest()[:16]


# -- fetchers -----------------------------------------------------------------


class MusicBrainzFetcher:
    """The public MusicBrainz API: ~1 req/s pacing, a real
    User-Agent, JSON responses normalized to the shared shapes."""

    name = "musicbrainz"

    def __init__(self, user_agent: str,
                 base_url: str = "https://musicbrainz.org/ws/2",
                 session: requests.Session | None = None,
                 min_interval_s: float = 1.1,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout_s: int = 20,
                 pacer: _Pacer | None = None):
        self.base = base_url.rstrip("/")
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        # The node's shared pacer when the service injects it
        # (R2-SF-6); a private one otherwise (tests, tools).
        self._pacer = pacer if pacer is not None else _Pacer(
            min_interval_s, sleep)

    def _get(self, path: str) -> dict[str, Any] | None:
        self._pacer.wait()
        try:
            res = self.session.get(
                f"{self.base}{path}",
                headers={"User-Agent": self.user_agent,
                         "Accept": "application/json"},
                timeout=self.timeout_s)
        except requests.RequestException as err:
            raise FetcherError(f"musicbrainz unreachable: {err}") from err
        if res.status_code == 404:
            return None
        if not res.ok:
            raise FetcherError(
                f"musicbrainz HTTP {res.status_code} for {path}")
        body = res.json()
        return body if isinstance(body, dict) else None

    def _recording(self, body: dict[str, Any]) -> dict[str, Any]:
        # R2-SF-3: a recording can carry SEVERAL ISRCs (one per
        # issuing label/territory). The full set rides along —
        # keeping only the first made cross-matches fail and,
        # worse, made the contradiction check condemn a claim
        # asserting a genuine second ISRC of the same recording.
        isrcs = [str(i) for i in (body.get("isrcs") or []) if i]
        return {
            "provider": "musicbrainz",
            "id": str(body.get("id") or ""),
            "title": str(body.get("title") or ""),
            "artist": _artist_credit_name(body.get("artist-credit")),
            "isrc": isrcs[0] if isrcs else None,
            "isrcs": isrcs,
            "duration_ms": body.get("length")
            if isinstance(body.get("length"), int) else None,
            "links": _links_from_url_rels(body.get("relations")),
            "album_id": None,
        }

    def track_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(
            f"/recording/{external_id}?inc=artists+isrcs+url-rels&fmt=json")
        return self._recording(body) if body else None

    def track_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        body = self._get(f"/recording/?query=isrc:{isrc}&fmt=json")
        recordings = (body or {}).get("recordings") or []
        if not recordings or not isinstance(recordings[0], dict):
            return None
        return self._recording(recordings[0])

    def album_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(
            f"/release-group/{external_id}"
            "?inc=artists+url-rels+releases&fmt=json")
        if not body:
            return None
        releases = [r for r in (body.get("releases") or [])
                    if isinstance(r, dict)
                    and r.get("status") in (None, "Official")]
        releases.sort(key=lambda r: str(r.get("date") or "9999"))
        release_id = str(releases[0]["id"]) if releases \
            and releases[0].get("id") else None
        date = str(body.get("first-release-date") or "")
        credits = [c for c in (body.get("artist-credit") or [])
                   if isinstance(c, dict)]
        first_artist = (credits[0].get("artist") or {}) \
            if credits else {}
        album = {
            "provider": "musicbrainz",
            "id": str(body.get("id") or external_id),
            "title": str(body.get("title") or ""),
            "artist": _artist_credit_name(body.get("artist-credit")),
            # The release's OWN artist id (first artist-credit) —
            # §7 grades album_of links against it (MF-2).
            "artist_id": (str(first_artist["id"])
                          if isinstance(first_artist, dict)
                          and first_artist.get("id") is not None
                          else None),
            "upc": None,
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "links": _links_from_url_rels(body.get("relations")),
            "release_id": release_id,
            "tracks": [],
        }
        if release_id:
            # R2-SF-5: the composite fetch is honest or it fails.
            # A tracklist lookup that 404s/errors must not become
            # a cached "album with no tracks" success — the empty
            # tracklist would fingerprint-match nothing, silently
            # downgrade tracklist verification for 90 days, and
            # pose as knowledge. Raising makes the engine treat
            # the whole lookup as unreachable and cache nothing.
            tracks = self.album_tracks(release_id)
            if tracks is None:
                raise FetcherError(
                    "musicbrainz tracklist lookup failed for "
                    f"release {release_id}; refusing a partial "
                    "album")
            album["tracks"] = tracks
        return album

    def album_tracks(self, release_id: str) -> list[dict[str, Any]] | None:
        body = self._get(f"/release/{release_id}?inc=recordings&fmt=json")
        if not body:
            return None
        out = []
        for medium in body.get("media") or []:
            if not isinstance(medium, dict):
                continue
            disc = medium.get("position") or 1
            for track in medium.get("tracks") or []:
                if not isinstance(track, dict):
                    continue
                recording = track.get("recording") or {}
                length = recording.get("length") or track.get("length")
                out.append({
                    "disc": disc,
                    "position": track.get("position"),
                    "title": track.get("title") or recording.get("title")
                    or "",
                    "duration_ms": length if isinstance(length, int)
                    else None,
                    "isrc": None,
                    "id": str(recording.get("id") or ""),
                })
        return out

    def artist_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(f"/artist/{external_id}?inc=url-rels&fmt=json")
        if not body:
            return None
        return {
            "provider": "musicbrainz",
            "id": str(body.get("id") or external_id),
            "name": str(body.get("name") or ""),
            "links": _links_from_url_rels(body.get("relations")),
        }


class DeezerFetcher:
    """The public Deezer lookup endpoints — no auth, paced politely
    anyway. Deezer answers errors as JSON bodies, not statuses."""

    name = "deezer"

    def __init__(self, base_url: str = "https://api.deezer.com",
                 session: requests.Session | None = None,
                 min_interval_s: float = 0.3,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout_s: int = 20):
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self._pacer = _Pacer(min_interval_s, sleep)

    def _get(self, path: str) -> dict[str, Any] | None:
        self._pacer.wait()
        try:
            res = self.session.get(f"{self.base}{path}",
                                   timeout=self.timeout_s)
        except requests.RequestException as err:
            raise FetcherError(f"deezer unreachable: {err}") from err
        if res.status_code == 404:
            return None
        if not res.ok:
            raise FetcherError(f"deezer HTTP {res.status_code} for {path}")
        body = res.json()
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if isinstance(error, dict):
            if error.get("code") == 800:  # "no data" — a true not-found
                return None
            raise FetcherError(
                f"deezer error {error.get('code')}: {error.get('message')}")
        return body

    def _track(self, body: dict[str, Any]) -> dict[str, Any]:
        artist = body.get("artist") or {}
        album = body.get("album") or {}
        duration = body.get("duration")
        return {
            "provider": "deezer",
            "id": str(body.get("id") or ""),
            "title": str(body.get("title") or ""),
            "artist": str(artist.get("name") or ""),
            "isrc": str(body["isrc"]) if body.get("isrc") else None,
            "duration_ms": int(duration) * 1000
            if isinstance(duration, (int, float)) else None,
            "links": {},
            "album_id": str(album["id"]) if album.get("id") else None,
        }

    def track_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(f"/track/{external_id}")
        return self._track(body) if body else None

    def track_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        body = self._get(f"/track/isrc:{isrc}")
        return self._track(body) if body else None

    def album_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(f"/album/{external_id}")
        if not body:
            return None
        artist = body.get("artist") or {}
        # R2-SF-5: same honesty rule as the MusicBrainz composite
        # — a failed tracklist call fails the fetch rather than
        # caching an empty-tracked album as a success.
        tracks = self.album_tracks(external_id)
        if tracks is None:
            raise FetcherError(
                "deezer tracklist lookup failed for album "
                f"{external_id}; refusing a partial album")
        album = {
            "provider": "deezer",
            "id": str(body.get("id") or external_id),
            "title": str(body.get("title") or ""),
            "artist": str(artist.get("name") or ""),
            # The album's OWN artist id — §7 grades album_of
            # links against it (MF-2).
            "artist_id": (str(artist["id"])
                          if artist.get("id") is not None else None),
            "upc": str(body["upc"]) if body.get("upc") else None,
            "year": None,
            "links": {},
            "release_id": None,
            "tracks": tracks,
        }
        nb_tracks = body.get("nb_tracks")
        if isinstance(nb_tracks, int) and len(tracks) < nb_tracks:
            # The payload says how much it is missing (R2-SF-5):
            # a truncated tracklist is labeled, never silent.
            album["tracks_truncated"] = True
        release_date = str(body.get("release_date") or "")
        if release_date[:4].isdigit():
            album["year"] = int(release_date[:4])
        return album

    def album_tracks(self, external_id: str) -> list[dict[str, Any]] | None:
        # Paginated (R2-SF-5): the endpoint caps a page at 100
        # and long albums (deluxe editions, box sets) run past
        # it; the walk follows `total` (or a short page) up to a
        # hard cap, after which album_by_id's nb_tracks check
        # records the truncation on the payload.
        out: list[dict[str, Any]] = []
        index = 0
        while True:
            body = self._get(
                f"/album/{external_id}/tracks?limit=100&index={index}")
            if not body:
                return out if out else None
            data = [t for t in (body.get("data") or [])
                    if isinstance(t, dict)]
            for track in data:
                duration = track.get("duration")
                out.append({
                    "disc": track.get("disk_number") or 1,
                    "position": track.get("track_position"),
                    "title": str(track.get("title") or ""),
                    "duration_ms": int(duration) * 1000
                    if isinstance(duration, (int, float)) else None,
                    "isrc": str(track["isrc"]) if track.get("isrc")
                    else None,
                    "id": str(track.get("id") or ""),
                })
            index += len(data)
            total = body.get("total")
            if len(data) < 100 or index >= 5000 \
                    or (isinstance(total, int) and index >= total):
                return out

    def artist_by_id(self, external_id: str) -> dict[str, Any] | None:
        body = self._get(f"/artist/{external_id}")
        if not body:
            return None
        return {
            "provider": "deezer",
            "id": str(body.get("id") or external_id),
            "name": str(body.get("name") or ""),
            "links": {},
        }


class SpotifyFetcher:
    """Explicitly not configured. The credential route — the node's
    own provider tokens, or SoulSync's provider access via its local
    API — is an open sub-decision (DESIGN §13 step 6); until it is
    decided, every lookup raises FetcherNotConfigured, which the
    engine treats as "no response", never as evidence."""

    name = "spotify"

    def _refuse(self) -> None:
        raise FetcherNotConfigured(
            "spotify fetcher is not configured: the credential "
            "route is an open sub-decision (DESIGN §13 step 6)")

    def track_by_id(self, external_id: str) -> None:
        self._refuse()

    def track_by_isrc(self, isrc: str) -> None:
        self._refuse()

    def album_by_id(self, external_id: str) -> None:
        self._refuse()

    def album_tracks(self, external_id: str) -> None:
        self._refuse()

    def artist_by_id(self, external_id: str) -> None:
        self._refuse()


# -- the engine -----------------------------------------------------------------


class Verifier:
    """Grades one subject's claims against provider responses and
    writes the verdicts. Synchronous core (fetchers pace
    themselves); the querier schedules it after answers land."""

    def __init__(self, store: MetaStore, fetchers: dict[str, Any], *,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.fetchers = fetchers
        self.clock = clock
        # Per-run state (verification runs are serialized — the
        # querier's single worker, or inline mode): _force makes
        # _fetch bypass the 90-day cache, and _evidence_at is the
        # OLDEST fetch time among the responses the current run
        # actually used; verdicts are stamped with it (MF-2).
        self._force = False
        self._evidence_at: float | None = None
        # SF-6 budget counters: network fetches spent by this run
        # (reset per verify_subjects call) and by the current
        # subject (reset per verify_subject).
        self._run_fetches = 0
        self._subject_fetches = 0
        # (provider, method, arg) lookups that answered NOT-FOUND
        # in the current subject's run — §7.1's absence evidence
        # (_apply_absence). Unreachable lookups never land here.
        self._not_found: set[tuple[str, str, str]] = set()
        # Negative cache (SF-6): (provider, cache key) -> the
        # time the lookup last FAILED. In-memory on purpose (see
        # _fetch): the fetch store holds responses only.
        self._unreachable: dict[tuple[str, str], float] = {}
        # Per-subject lookup-ordering state (gate r2 SF-4), reset
        # by verify_subject: fetches charged per asserting origin,
        # the origins' standing on this subject, their cached
        # reputations, and which origins each lookup spends.
        self._origin_fetches: dict[str, int] = {}
        self._subject_standing: dict[str, bool] = {}
        self._rep_cache: dict[str, dict[str, Any]] = {}
        self._lookup_asserters: dict[tuple[str, str, str],
                                     set[str]] = {}
        self._own_lookup_keys: set[tuple[str, str, str]] = set()

    # -- cached fetching (§7.1) --------------------------------------

    def _note_evidence(self, fetched_at: float) -> None:
        if self._evidence_at is None \
                or fetched_at < self._evidence_at:
            self._evidence_at = fetched_at

    def _fetch(self, provider: str, method: str, arg: str) -> Any:
        """One provider lookup through the 90-day fetch cache.
        Returns the normalized response, None for a not-found, or
        the _UNREACHABLE sentinel when the lookup said nothing.

        In a FORCED run — the §6 reliance re-check and the §8
        sanity re-check (MF-2) — the cache read is bypassed: a
        re-check that replays cached responses can never overturn
        anything, which is the entire point of re-checking. The
        fresh response still lands in the cache for later runs.

        Budget (SF-6): a lookup that would go to the network past
        the per-run/per-subject caps answers _UNREACHABLE — it
        says nothing, like a dark provider — and a lookup that
        FAILS is negative-cached for UNREACHABLE_BACKOFF_S, so it
        is not re-asked on every ask. The negative cache lives in
        this verifier's memory, NOT the fetch store: the store
        holds provider RESPONSES only (R2-SF-5's honesty rule — a
        failed lookup must never land there, where it could pose
        as knowledge). Not-found outcomes are remembered in
        _not_found for §7.1's absence rule."""
        fetcher = self.fetchers.get(provider)
        if fetcher is None:
            return _UNREACHABLE
        key = f"{method}:{arg}"
        now = self.clock()
        if not self._force:
            hit = self.store.fetch_cached(provider, key)
            if hit is not None \
                    and now - hit["fetched_at"] < FETCH_FRESH_S:
                if hit["not_found"]:
                    self._not_found.add((provider, method, arg))
                    self._note_evidence(hit["fetched_at"])
                    return _NOT_FOUND
                if hit["payload"] is not None:
                    self._note_evidence(hit["fetched_at"])
                    return hit["payload"]
                # payload None + not_found False is not a response
                # shape the store should hold; treat as a miss.
            failed_at = self._unreachable.get((provider, key))
            if failed_at is not None \
                    and now - failed_at < UNREACHABLE_BACKOFF_S:
                return _UNREACHABLE
        if self._run_fetches >= MAX_FETCHES_PER_RUN \
                or self._subject_fetches >= MAX_FETCHES_PER_SUBJECT:
            return _UNREACHABLE
        # Per-origin cap (gate r2 SF-4): a lookup whose asserting
        # origins have ALL spent their per-subject allowance says
        # nothing — the same _UNREACHABLE as a dark provider, so
        # it can never pose as absence evidence either. A lookup
        # of an id the user's OWN claims assert is exempt (gate
        # closing note): an attacker who co-asserts one of the
        # user's own ids and burns its allowance must not be able
        # to delay grading of the user's own id.
        asserters = self._lookup_asserters.get((provider, method, arg))
        if asserters \
                and (provider, method, arg) \
                not in self._own_lookup_keys \
                and all(
                    self._origin_fetches.get(o, 0)
                    >= MAX_FETCHES_PER_ORIGIN
                    for o in asserters):
            return _UNREACHABLE
        self._run_fetches += 1
        self._subject_fetches += 1
        if asserters:
            for o in asserters:
                self._origin_fetches[o] = \
                    self._origin_fetches.get(o, 0) + 1
        try:
            result = getattr(fetcher, method)(arg)
        except FetcherError:
            self._unreachable[(provider, key)] = now
            return _UNREACHABLE
        self.store.fetch_store(provider, key, now, result is None,
                               result)
        self._note_evidence(now)
        if result is None:
            self._not_found.add((provider, method, arg))
            return _NOT_FOUND
        return result

    def _response(self, provider: str, method: str, arg: str) -> Any:
        out = self._fetch(provider, method, arg)
        return None if out is _NOT_FOUND or out is _UNREACHABLE else out

    # -- verdict bookkeeping ------------------------------------------

    def _grade(self, claims: list[dict[str, Any]], field: str,
               value: Any, tier: str, detail: str,
               summary: dict[str, int]) -> None:
        """Write `tier` for every asserting origin of one
        (field, value). A passed check is counted for the origin
        on STRONG grades only (SF-8): checks_passed is the §8
        sanity gate's currency — the power to trigger re-fetches
        — and a plausible grade (one provider nodding at a name)
        must not mint it. Copying one public id and having it
        confirmed at name level earns no trigger power."""
        now = self.clock()
        # MF-2: verified_at is the EVIDENCE time — when the
        # provider responses this grading rests on were fetched —
        # not the grading moment. Merge's verification_stale
        # measures evidence age; stamping grading time would let a
        # verdict graded today from 80-day-old cached responses
        # pose as fresh for another 90 days.
        evidence_at = self._evidence_at \
            if self._evidence_at is not None else now
        for claim in claims:
            if claim["field"] != field or claim["value"] != value:
                continue
            origin = claim.get("origin")
            if origin is None:
                continue  # own claims are local; no verdict rows
            self.store.set_verdict(origin, claim["subject"], field,
                                   value, tier, evidence_at, detail)
            if tier == TIER_STRONG:
                self.store.record_check(origin, True, now)
            summary["strong" if tier == TIER_STRONG else "plausible"] += 1

    def _fail(self, claims: list[dict[str, Any]], field: str,
              value: Any, summary: dict[str, int]) -> None:
        """A claim the providers CONTRADICT (identity established,
        value different): the verdict clears and the origin's
        local record takes a failed check."""
        for claim in claims:
            if claim["field"] != field or claim["value"] != value:
                continue
            origin = claim.get("origin")
            if origin is None:
                continue
            self.store.clear_verdict(origin, claim["subject"], field,
                                     value)
            self.store.record_check(origin, False, self.clock())
            summary["failed"] += 1

    # -- subject assembly ----------------------------------------------

    def _claims_for(self, subject: str) -> list[dict[str, Any]]:
        rows = []
        for claim in self.store.own_claims(subject):
            rows.append({**claim, "origin": None})
        verdicts = self.store.verdicts_for_subjects([subject])
        now = self.clock()
        for row in self.store.cached_claims([subject]):
            verdict = verdicts.get(
                (row["origin_node"], subject, row["field"],
                 value_key(row["value"])))
            assembled = {
                "subject": row["subject"], "kind": row["kind"],
                "field": row["field"], "value": row["value"],
                "source": row["source"], "origin": row["origin_node"],
                "asserted_at": row["asserted_at"], "verdict": verdict,
            }
            # MF-1: expired rows are DEAD (§6) — filtered with
            # merge's own _row_expired predicate, so the verifier
            # and the merge can never disagree about what counts.
            # An expired, unverdicted row is never graded: a strong
            # verdict would exempt it from expiry and resurrect a
            # claim its origin may have retracted long ago.
            if meta_merge._row_expired(assembled, now):
                continue
            rows.append(assembled)
        return rows

    @staticmethod
    def _values(claims: list[dict[str, Any]], field: str) -> set:
        return {c["value"] for c in claims if c["field"] == field
                and isinstance(c["value"], str)}

    @staticmethod
    def _name_parts(claims: list[dict[str, Any]]) -> list[str] | None:
        for claim in claims:
            if claim["field"] == "name" and isinstance(
                    claim["value"], list):
                return [str(p) for p in claim["value"]]
        return None

    # -- lookup ordering + per-origin budget (gate r2 SF-4) ---------

    def _origin_rank(self, origin: str | None) -> tuple:
        """One asserting origin's rank for lookup ordering within
        a subject: the user's own data first (§3 — local wins),
        then standing origins, then by local reputation (checks
        passed minus failed, descending)."""
        if origin is None:
            return (0, float("-inf"))
        rep = self._rep_cache.get(origin)
        if rep is None:
            rep = self.store.reputation_for(origin)
            self._rep_cache[origin] = rep
        score = rep["checks_passed"] - rep["checks_failed"]
        return (0, -score) if self._subject_standing.get(origin) \
            else (1, -score)

    def _ordered_ids(self, claims: list[dict[str, Any]],
                     field: str) -> list[str]:
        """The distinct values claimed for `field`, in the order
        their lookups should be spent: by the BEST asserting
        origin's rank (standing, then reputation), the value
        itself breaking ties — deterministic, and an honest id
        is never stuck behind forty bogus ones that sort first
        as strings."""
        best: dict[str, tuple] = {}
        for claim in claims:
            if claim["field"] != field \
                    or not isinstance(claim["value"], str):
                continue
            rank = self._origin_rank(claim.get("origin"))
            value = claim["value"]
            if value not in best or rank < best[value]:
                best[value] = rank
        return [value for value, _rank in sorted(
            best.items(), key=lambda kv: (kv[1], kv[0]))]

    @staticmethod
    def _build_lookup_asserters(claims: list[dict[str, Any]],
                                kind: str,
                                ) -> tuple[dict[tuple[str, str, str],
                                                set[str]],
                                           set[tuple[str, str, str]]]:
        """(provider, method, arg) -> the network origins whose
        claims that lookup would spend — the per-origin cap's
        accounting map for one subject — plus the set of lookup
        keys the user's OWN claims assert. Own claims (origin
        None) are never charged and never capped: a lookup of an
        own-asserted id runs even when every network co-asserter
        has spent its allowance, so co-asserting the user's id
        and burning an allowance cannot delay its grading. A
        named album_track variant's release lookup spends the
        origins asserting slots under that variant (and the
        variant's release id is own-asserted when the user's own
        slots sit under it)."""
        table = _LOOKUP_SOURCES.get(kind, {})
        out: dict[tuple[str, str, str], set[str]] = {}
        own: set[tuple[str, str, str]] = set()
        for claim in claims:
            origin = claim.get("origin")
            field, value = claim["field"], claim["value"]
            keys: list[tuple[str, str, str]] = []
            if field in table and isinstance(value, str):
                keys += [(provider, method, value)
                         for provider, method in table[field]]
            if kind == "album" and field == "album_track" \
                    and isinstance(value, dict):
                variant = value.get("variant")
                if isinstance(variant, str) \
                        and variant.startswith("mb:"):
                    keys.append(
                        ("musicbrainz", "album_tracks", variant[3:]))
            if origin is None:
                own.update(keys)
            else:
                for key in keys:
                    out.setdefault(key, set()).add(origin)
        return out, own

    def verify_subject(self, subject: str, *,
                       force: bool = False) -> dict[str, Any]:
        """Grade one subject's claims. `force` is the MF-2
        re-check mode (§6 reliance aging, §8 sanity): provider
        responses are re-fetched past the 90-day cache so fresh
        evidence can OVERTURN a stored verdict — a contradiction
        clears it through the _fail path, while a merely lesser
        grade never overwrites it (the store's verdict writes are
        monotonic, R2-SF-1; a provider being unreachable writes
        nothing at all)."""
        claims = self._claims_for(subject)
        summary = {"subject": subject, "strong": 0, "plausible": 0,
                   "failed": 0}
        if not claims:
            return summary
        kind = claims[0]["kind"]
        self._force = force
        self._evidence_at = None
        self._subject_fetches = 0
        self._not_found = set()
        # Lookup ordering + per-origin budget state (SF-4).
        self._origin_fetches = {}
        self._rep_cache = {}
        self._subject_standing = {}
        verdicts = self.store.verdicts_for_subjects([subject])
        now = self.clock()
        for row in self.store.cached_claims([subject]):
            # The same expiry filter _subject_priority applies
            # (gate closing note): an expired row is dead (§6),
            # so it lends its origin no standing for lookup
            # ordering either — an origin whose only rows are
            # expired ranks as no-standing here too, and the
            # verifier's two rankings can never disagree.
            verdict = verdicts.get(
                (row["origin_node"], subject, row["field"],
                 value_key(row["value"])))
            if meta_merge._row_expired(
                    {"origin": row["origin_node"],
                     "asserted_at": row["asserted_at"],
                     "verdict": verdict}, now):
                continue
            origin = row["origin_node"]
            self._subject_standing[origin] = \
                self._subject_standing.get(origin, False) \
                or row["standing"]
        self._lookup_asserters, self._own_lookup_keys = \
            self._build_lookup_asserters(claims, kind)
        try:
            if kind == "track":
                self._verify_track(subject, claims, summary)
            elif kind == "album":
                self._verify_album(subject, claims, summary)
            elif kind == "artist":
                self._verify_artist(subject, claims, summary)
        finally:
            self._force = False
            self._evidence_at = None
        return summary

    def _subject_priority(self, subject: str) -> tuple:
        """The §9 ranking of the verification queue (SF-6):
        subjects holding claims from STANDING origins first, then
        by the best reputation score among their asserting
        origins; the subject id breaks ties, so the order is
        deterministic and the budget is spent best-evidence-first.
        EXPIRED rows contribute nothing (gate r2 leftover, MF-1's
        filter applied here too): a retracted claim's origin must
        not lend the subject its standing or its reputation — the
        expiry check runs on the assembled row shape (origin +
        verdict), exactly as _claims_for builds it."""
        standing = False
        best = 0
        seen: set[str] = set()
        verdicts = self.store.verdicts_for_subjects([subject])
        now = self.clock()
        for row in self.store.cached_claims([subject]):
            verdict = verdicts.get(
                (row["origin_node"], subject, row["field"],
                 value_key(row["value"])))
            if meta_merge._row_expired(
                    {"origin": row["origin_node"],
                     "asserted_at": row["asserted_at"],
                     "verdict": verdict}, now):
                continue
            if row["standing"]:
                standing = True
            origin = row["origin_node"]
            if origin in seen:
                continue
            seen.add(origin)
            rep = self.store.reputation_for(origin)
            best = max(best, rep["checks_passed"]
                       - rep["checks_failed"])
        return (0 if standing else 1, -best, subject)

    def verify_subjects(self, subjects, *,
                        force: bool = False) -> list[dict[str, Any]]:
        self._run_fetches = 0
        ranked = sorted(subjects, key=self._subject_priority)
        return [self.verify_subject(s, force=force) for s in ranked]

    # -- §7.1 absence (SF-10) -------------------------------------------

    def _apply_absence(self, subject: str,
                       claims: list[dict[str, Any]],
                       summary: dict[str, int],
                       asserted: set[tuple[str, Any]],
                       paths_by_claim: dict[tuple[str, Any],
                                            list[tuple[str, str, str,
                                                       Any]]],
                       ) -> None:
        """§7.1's absence rule: a PREVIOUSLY VERIFIED id is
        absent when two independent lookup paths agree it is
        not-found — the claim's own primary lookup among them —
        and no response in this run asserted the value. Its
        verdict clears and a failed check is recorded: absence
        is evidence, recorded as such. One path's 404 is
        "unreachable", never absence; an unreachable lookup
        counts for nothing here (it never enters _not_found).
        Without this, a strong verdict for a deleted entity
        would stay strong forever — verdicts exempt their
        claims from the 90-day expiry.

        Paths are (provider, method, arg, via) tuples; `via`
        names the id a SECONDARY path was built from (None for
        the claim's own primary lookup). Two guards keep the
        rule honest (gate r2 SF-3): the two paths must be two
        DIFFERENT PROVIDERS — one provider's by-id and by-isrc
        lookups are one path, not two — and a secondary path
        counts only when its via id is VOUCHED for: it carries
        its own strong verdict, or the SAME origin whose verdict
        is on trial asserted it. An id a stranger invented
        resolves nowhere and proves nothing; letting it serve as
        the second path would reduce the rule to one path, and
        one provider's silence (a region-blocked catalogue is a
        realistic not-found) would clear an honest verdict."""
        verdicts = self.store.verdicts_for_subjects([subject])
        if not verdicts:
            return
        strong_ids = {(f, vk) for (_o, _s, f, vk), v
                      in verdicts.items()
                      if (v or {}).get("tier") == TIER_STRONG}
        asserted_by: dict[Any, set[tuple[str, Any]]] = {}
        for c in claims:
            if isinstance(c["value"], (str, int)):
                asserted_by.setdefault(c.get("origin"), set()).add(
                    (c["field"], c["value"]))
        for claim in claims:
            origin = claim.get("origin")
            if origin is None:
                continue
            field, value = claim["field"], claim["value"]
            # Only id-shaped values have resolution paths (name
            # values are lists, link values dicts — neither can be
            # absent in §7.1's sense).
            if not isinstance(value, (str, int)):
                continue
            paths = paths_by_claim.get((field, value), [])
            if not paths:
                continue
            if (field, value) in asserted:
                continue
            if (origin, subject, field, value_key(value)) \
                    not in verdicts:
                continue
            if paths[0][:3] not in self._not_found:
                continue
            own_ids = asserted_by.get(origin, set())
            hits = [p for p in paths
                    if self._absence_path_counts(p, own_ids,
                                                 strong_ids)]
            if len({p[0] for p in hits}) < 2:
                continue
            self.store.clear_verdict(origin, subject, field, value)
            self.store.record_check(origin, False, self.clock())
            summary["failed"] += 1

    def _absence_path_counts(self, path: tuple,
                             own_ids: set[tuple[str, Any]],
                             strong_ids: set[tuple[str, str]],
                             ) -> bool:
        """One absence path counts toward the two-provider rule
        when its lookup answered not-found AND the id it was
        built from is vouched for (gate r2 SF-3): the claim's own
        primary lookup (via None) always counts; a secondary
        path counts when its via id was asserted by the origin on
        trial, or carries its own strong verdict."""
        if path[:3] not in self._not_found:
            return False
        via = path[3]
        if via is None:
            return True
        if via in own_ids:
            return True
        return (via[0], value_key(via[1])) in strong_ids

    # -- tracks ---------------------------------------------------------

    def _verify_track(self, subject: str, claims: list[dict[str, Any]],
                      summary: dict[str, int]) -> None:
        parts = self._name_parts(claims)
        if parts is None or len(parts) != 2:
            return
        artist_name, title = parts
        mbids = self._values(claims, "recording_mbid")
        deezer_ids = self._values(claims, "deezer_id")
        isrcs = self._values(claims, "isrc")

        mb_responses = []
        for mbid in self._ordered_ids(claims, "recording_mbid"):
            resp = self._response("musicbrainz", "track_by_id", mbid)
            if resp is not None:
                mb_responses.append(resp)
        for isrc in self._ordered_ids(claims, "isrc"):
            resp = self._response("musicbrainz", "track_by_isrc", isrc)
            if resp is not None and resp not in mb_responses:
                mb_responses.append(resp)
        dz_responses = []
        for deezer_id in self._ordered_ids(claims, "deezer_id"):
            resp = self._response("deezer", "track_by_id", deezer_id)
            if resp is not None:
                dz_responses.append(resp)
        for isrc in self._ordered_ids(claims, "isrc"):
            resp = self._response("deezer", "track_by_isrc", isrc)
            if resp is not None and resp not in dz_responses:
                dz_responses.append(resp)

        def identity_ok(resp: dict[str, Any]) -> bool:
            return _norm(resp.get("title")) == _norm(title) \
                and _norm(resp.get("artist")) == _norm(artist_name)

        mb_ok = [r for r in mb_responses if identity_ok(r)]
        dz_ok = [r for r in dz_responses if identity_ok(r)]

        strong_pairs: list[tuple[dict, dict]] = []
        for mb in mb_ok:
            for dz in dz_ok:
                # R2-SF-3: agreement is set intersection — the
                # Deezer ISRC need only be AMONG the recording's
                # ISRCs, not equal to the first one listed.
                isrc_agree = bool(_isrc_set(mb) & _isrc_set(dz))
                link_agree = (mb.get("links") or {}).get("deezer") \
                    == dz.get("id")
                if isrc_agree or link_agree:
                    strong_pairs.append((mb, dz))

        if strong_pairs:
            asserted_isrc: set[str] = set()
            asserted_deezer: set[str] = set()
            asserted_mbid: set[str] = set()
            asserted_spotify: set[str] = set()
            durations: list[int] = []
            for mb, dz in strong_pairs:
                asserted_isrc |= _isrc_set(mb)
                asserted_isrc |= _isrc_set(dz)
                asserted_deezer.add(dz["id"])
                asserted_mbid.add(mb["id"])
                links = mb.get("links") or {}
                if links.get("spotify"):
                    asserted_spotify.add(links["spotify"])
                if links.get("deezer"):
                    asserted_deezer.add(links["deezer"])
                for resp in (mb, dz):
                    if isinstance(resp.get("duration_ms"), int):
                        durations.append(resp["duration_ms"])
            for value in asserted_isrc:
                self._grade(claims, "isrc", value, TIER_STRONG,
                            "isrc cross-match (musicbrainz + deezer)",
                            summary)
            for value in asserted_deezer:
                self._grade(claims, "deezer_id", value, TIER_STRONG,
                            "deezer id cross-match", summary)
            for value in asserted_mbid:
                self._grade(claims, "recording_mbid", value, TIER_STRONG,
                            "recording mbid cross-match", summary)
            for value in asserted_spotify:
                self._grade(claims, "spotify_id", value, TIER_STRONG,
                            "musicbrainz link on a cross-matched "
                            "recording", summary)
            for claim in claims:
                if claim["field"] != "duration_ms":
                    continue
                if any(abs(claim["value"] - d) <= DURATION_TOLERANCE_MS
                       for d in durations):
                    self._grade(claims, "duration_ms", claim["value"],
                                TIER_STRONG,
                                "duration within 2s of cross-matched "
                                "provider reports", summary)
            # Set-valued ids are NOT contradictions (§8, SF-7):
            # track ids and ISRCs are SETS — a live/remaster/edit
            # twin's true Deezer id or ISRC is simply not part of
            # this pair, and failing it would clear an honest
            # origin's verdict and charge it a failed check for
            # asserting something true. Only the recording MBID
            # keeps the contradiction path: identity is
            # established by the pair, and an MBID the pair's
            # responses do not carry is a false assertion of
            # canonical identity, not a set member.
            for value in mbids - asserted_mbid:
                self._fail(claims, "recording_mbid", value, summary)
            self._absence_track(claims, summary, mb_responses,
                                dz_responses, isrcs, deezer_ids, mbids)
            return

        # No cross-match: a single identity-matching response makes
        # the values IT asserts plausible (name+duration class of
        # evidence — §7 treats it as unverifiable for gating).
        singles = mb_ok + dz_ok
        if singles:
            for resp in singles:
                for isrc_value in sorted(_isrc_set(resp)):
                    self._grade(claims, "isrc", isrc_value,
                                TIER_PLAUSIBLE,
                                f"single {resp['provider']} response",
                                summary)
                if resp["provider"] == "deezer":
                    self._grade(claims, "deezer_id", resp["id"],
                                TIER_PLAUSIBLE, "single deezer response",
                                summary)
                if resp["provider"] == "musicbrainz":
                    self._grade(claims, "recording_mbid", resp["id"],
                                TIER_PLAUSIBLE,
                                "single musicbrainz response", summary)
                duration = resp.get("duration_ms")
                if isinstance(duration, int):
                    for claim in claims:
                        if claim["field"] == "duration_ms" and abs(
                                claim["value"] - duration) \
                                <= DURATION_TOLERANCE_MS:
                            self._grade(claims, "duration_ms",
                                        claim["value"], TIER_PLAUSIBLE,
                                        "name+duration agreement",
                                        summary)
        # §7.1 absence runs on EVERY path (SF-10): a previously
        # verified id that two independent lookups now agree is
        # not-found loses its verdict even when this run graded
        # nothing at all.
        self._absence_track(claims, summary, mb_responses,
                            dz_responses, isrcs, deezer_ids, mbids)

    def _absence_track(self, claims: list[dict[str, Any]],
                       summary: dict[str, int],
                       mb_responses: list[dict[str, Any]],
                       dz_responses: list[dict[str, Any]],
                       isrcs: set[str], deezer_ids: set[str],
                       mbids: set[str]) -> None:
        """§7.1 absence for a track: claimed ids resolve through
        the by-isrc and by-id lookups this run performed. Two
        PROVIDERS must agree not-found, and secondary paths are
        marked with the ISRC they were built from — an MBID
        resolves through MusicBrainz alone, so its second path
        is the by-isrc lookup, at the OTHER provider, of a
        claimed ISRC that would have carried it (and that path
        counts only under _apply_absence's vouching rule)."""
        asserted: set[tuple[str, Any]] = set()
        for resp in mb_responses + dz_responses:
            for isrc in _isrc_set(resp):
                asserted.add(("isrc", isrc))
            links = resp.get("links") or {}
            for link_provider in ("deezer", "spotify"):
                if links.get(link_provider):
                    asserted.add((f"{link_provider}_id",
                                  links[link_provider]))
        for resp in mb_responses:
            if resp.get("id"):
                asserted.add(("recording_mbid", resp["id"]))
        for resp in dz_responses:
            if resp.get("id"):
                asserted.add(("deezer_id", resp["id"]))
        paths: dict[tuple[str, Any],
                    list[tuple[str, str, str, Any]]] = {}
        for isrc in isrcs:
            paths[("isrc", isrc)] = [
                ("musicbrainz", "track_by_isrc", isrc, None),
                ("deezer", "track_by_isrc", isrc, None)]
        for mbid in mbids:
            paths[("recording_mbid", mbid)] = [
                ("musicbrainz", "track_by_id", mbid, None)] + [
                ("musicbrainz", "track_by_isrc", isrc,
                 ("isrc", isrc)) for isrc in sorted(isrcs)] + [
                ("deezer", "track_by_isrc", isrc,
                 ("isrc", isrc)) for isrc in sorted(isrcs)]
        for deezer_id in deezer_ids:
            paths[("deezer_id", deezer_id)] = [
                ("deezer", "track_by_id", deezer_id, None)] + [
                ("deezer", "track_by_isrc", isrc,
                 ("isrc", isrc)) for isrc in sorted(isrcs)] + [
                ("musicbrainz", "track_by_isrc", isrc,
                 ("isrc", isrc)) for isrc in sorted(isrcs)]
        self._apply_absence(claims[0]["subject"] if claims else "",
                            claims, summary, asserted, paths)

    # -- albums ----------------------------------------------------------

    def _verify_album(self, subject: str, claims: list[dict[str, Any]],
                      summary: dict[str, int]) -> None:
        parts = self._name_parts(claims)
        if parts is None or len(parts) != 2:
            return
        artist_name, title = parts
        group_ids = self._values(claims, "release_group_mbid")
        deezer_ids = self._values(claims, "deezer_id")

        # R2-SF-2: EVERY claimed id is tried, and the response
        # used is the first that IDENTITY-MATCHES — not the first
        # that merely resolves. A wrong first id (a namesake
        # release group) used to poison the whole check: nothing
        # graded even though the claim's second id was the album.
        mb_responses = []
        for group_id in self._ordered_ids(claims,
                                          "release_group_mbid"):
            resp = self._response("musicbrainz", "album_by_id",
                                  group_id)
            if resp is not None:
                mb_responses.append(resp)
        dz_responses = []
        for deezer_id in self._ordered_ids(claims, "deezer_id"):
            resp = self._response("deezer", "album_by_id", deezer_id)
            if resp is not None:
                dz_responses.append(resp)

        def identity_ok(resp: dict[str, Any]) -> bool:
            return _norm(resp.get("title")) == _norm(title) \
                and _norm(resp.get("artist")) == _norm(artist_name)

        mb = next((r for r in mb_responses if identity_ok(r)), None)
        dz = next((r for r in dz_responses if identity_ok(r)), None)

        pair = mb is not None and dz is not None and identity_ok(mb) \
            and identity_ok(dz) \
            and (mb.get("links") or {}).get("deezer") == dz.get("id")
        if pair:
            self._grade(claims, "release_group_mbid", mb["id"],
                        TIER_STRONG, "release-group cross-match",
                        summary)
            self._grade(claims, "deezer_id", dz["id"], TIER_STRONG,
                        "deezer album cross-match", summary)
            links = mb.get("links") or {}
            if links.get("spotify"):
                self._grade(claims, "spotify_id", links["spotify"],
                            TIER_STRONG,
                            "musicbrainz link on a cross-matched album",
                            summary)
            link_ids = {"musicbrainz": mb["id"], **links,
                        "deezer": dz["id"]}
            for claim in claims:
                if claim["field"] != "provider_link":
                    continue
                value = claim["value"]
                if not isinstance(value, dict):
                    continue
                if value.get("relation") != "same_entity":
                    continue
                provider = value.get("provider")
                if link_ids.get(provider) == value.get("from_id") \
                        and value.get("to_id") in link_ids.values():
                    self._grade(claims, "provider_link", value,
                                TIER_STRONG,
                                "link asserted by the musicbrainz "
                                "response", summary)
            if mb.get("year"):
                self._grade(claims, "year", mb["year"], TIER_STRONG,
                            "musicbrainz release-group year", summary)
            self._grade_album_of(claims, mb, summary)
            self._grade_album_of(claims, dz, summary)
            self._verify_tracklist(claims, mb, dz, summary)
            self._absence_album(claims, summary, [mb, dz],
                                group_ids, deezer_ids)
            return

        # Single-response plausibility for the id the response is.
        for resp, field in ((mb, "release_group_mbid"),
                            (dz, "deezer_id")):
            if resp is not None and identity_ok(resp):
                self._grade(claims, field, resp["id"], TIER_PLAUSIBLE,
                            f"single {resp['provider']} response",
                            summary)
        # A MusicBrainz response linking to the claimed Deezer id
        # is §7's single-response strong form, even with no Deezer
        # response in hand.
        if mb is not None and identity_ok(mb):
            linked = (mb.get("links") or {}).get("deezer")
            if linked and linked in deezer_ids:
                self._grade(claims, "deezer_id", linked, TIER_STRONG,
                            "musicbrainz links to this deezer album",
                            summary)
                self._grade(claims, "release_group_mbid", mb["id"],
                            TIER_STRONG,
                            "musicbrainz album carrying the link",
                            summary)
        # album_of grading needs only an identity-matched
        # response: the link is the provider's own assertion
        # about its own record (MF-2 — §8's fetched-link rung
        # settles on exactly this).
        for resp in (mb, dz):
            if resp is not None and identity_ok(resp):
                self._grade_album_of(claims, resp, summary)
        self._absence_album(claims, summary,
                            [r for r in (mb, dz) if r is not None],
                            group_ids, deezer_ids)

    def _grade_album_of(self, claims: list[dict[str, Any]],
                        resp: dict[str, Any],
                        summary: dict[str, int]) -> None:
        """Grade album_of provider_link claims against one album
        response (MF-2). The response carries the artist id of
        its own artist — Deezer's album carries its artist object;
        the MusicBrainz release carries its artist-credit — so a
        claim whose from_id is this album and whose to_id is that
        artist was asserted by the provider itself: STRONG."""
        if resp is None:
            return
        artist_id = resp.get("artist_id")
        rid = resp.get("id")
        provider = resp.get("provider")
        if not artist_id or not rid or not provider:
            return
        for claim in claims:
            if claim["field"] != "provider_link" \
                    or not isinstance(claim["value"], dict):
                continue
            value = claim["value"]
            if value.get("relation") == "album_of" \
                    and value.get("provider") == provider \
                    and value.get("from_id") == rid \
                    and value.get("to_id") == artist_id:
                self._grade(claims, "provider_link", value,
                            TIER_STRONG,
                            "album_of asserted by the provider's "
                            "album response", summary)

    def _absence_album(self, claims: list[dict[str, Any]],
                       summary: dict[str, int],
                       responses: list[dict[str, Any]],
                       group_ids: set[str],
                       deezer_ids: set[str]) -> None:
        """§7.1 absence for an album: each claimed id resolves
        through BOTH providers' album lookups (MusicBrainz by
        release-group id, Deezer by album id), which is the
        independent second path."""
        if not claims:
            return
        asserted: set[tuple[str, Any]] = set()
        for resp in responses:
            if resp.get("provider") == "musicbrainz" and resp.get("id"):
                asserted.add(("release_group_mbid", resp["id"]))
            if resp.get("provider") == "deezer" and resp.get("id"):
                asserted.add(("deezer_id", resp["id"]))
            linked = (resp.get("links") or {}).get("deezer")
            if linked:
                asserted.add(("deezer_id", linked))
        paths: dict[tuple[str, Any],
                    list[tuple[str, str, str, Any]]] = {}
        for group_id in group_ids:
            paths[("release_group_mbid", group_id)] = [
                ("musicbrainz", "album_by_id", group_id, None)] + [
                ("deezer", "album_by_id", d, ("deezer_id", d))
                for d in sorted(deezer_ids)]
        for deezer_id in deezer_ids:
            paths[("deezer_id", deezer_id)] = [
                ("deezer", "album_by_id", deezer_id, None)] + [
                ("musicbrainz", "album_by_id", g,
                 ("release_group_mbid", g))
                for g in sorted(group_ids)]
        self._apply_absence(claims[0]["subject"], claims, summary,
                            asserted, paths)

    def _verify_tracklist(self, claims: list[dict[str, Any]],
                          mb: dict[str, Any], dz: dict[str, Any],
                          summary: dict[str, int]) -> None:
        """§7 album_track verification: the claimed variant whose
        (disc, position, normalized title) fingerprint — computed
        from the tracks' own name claims — equals a fetched
        tracklist's fingerprint is strong, slot by slot. Variant
        equivalence across providers is the shared fingerprint
        itself (or a shared MusicBrainz release id in the variant
        name).

        Grouping rules (R2-SF-11, extended by gate MF-4): claims
        group PER ASSERTING ORIGIN — variant-less ones labeled by
        their (disc count, track count) shape, named ones keeping
        their variant label per origin — never one shared group:
        two different releases' slots would fingerprint as a
        chimera, and one origin's extra or wrong slot must poison
        only ITS OWN fingerprint, never the variant's (two origins
        asserting the same variant and agreeing must both verify).
        And a variant NAMING a MusicBrainz release (mb:<release_id>)
        grades against THAT release's tracklist ALONE (R2-SF-10,
        tightened by gate MF-5): the release-group pick and the
        Deezer edition are different pressings — a variant whose
        slots match only the union, or only another pressing, is
        not the named release."""
        mb_print = tracklist_fingerprint(
            (mb or {}).get("tracks") or [])
        dz_print = tracklist_fingerprint(
            (dz or {}).get("tracks") or [])
        by_variant: dict[Any, list[dict[str, Any]]] = {}
        for claim in claims:
            if claim["field"] == "album_track" \
                    and isinstance(claim["value"], dict):
                by_variant.setdefault(
                    claim["value"].get("variant"), []).append(claim)
        groups: list[tuple[Any, list[dict[str, Any]]]] = []
        for variant, group in by_variant.items():
            per_origin: dict[Any, list[dict[str, Any]]] = {}
            for slot_claim in group:
                per_origin.setdefault(
                    slot_claim.get("origin"), []).append(slot_claim)
            for origin_group in per_origin.values():
                if variant is not None:
                    groups.append((variant, origin_group))
                    continue
                slots = {(c["value"]["disc"], c["value"]["position"])
                         for c in origin_group}
                discs = max((int(c["value"]["disc"] or 0)
                             for c in origin_group), default=0)
                groups.append(
                    (f"shape:{discs}x{len(slots)}", origin_group))
        for variant, group in groups:
            titles = []
            for slot_claim in group:
                value = slot_claim["value"]
                name_claims = self._claims_for(value["track"])
                parts = self._name_parts(name_claims)
                titles.append({
                    "disc": value["disc"],
                    "position": value["position"],
                    "title": parts[1] if parts and len(parts) == 2
                    else "",
                })
            claim_print = tracklist_fingerprint(titles)
            if isinstance(variant, str) and variant.startswith("mb:"):
                # MF-5: the named release's tracklist ALONE. If the
                # fetch says nothing, the variant grades against no
                # tracklist at all — falling back to the group's
                # other pressings would grade a different release.
                prints = set()
                named = self._response("musicbrainz", "album_tracks",
                                       variant[3:])
                if named:
                    prints.add(tracklist_fingerprint(named))
            else:
                prints = {mb_print, dz_print}
            if claim_print in prints:
                for slot_claim in group:
                    self._grade([slot_claim], "album_track",
                                slot_claim["value"], TIER_STRONG,
                                "tracklist fingerprint matches a "
                                "fetched release", summary)

    # -- artists ----------------------------------------------------------

    def _verify_artist(self, subject: str, claims: list[dict[str, Any]],
                       summary: dict[str, int]) -> None:
        parts = self._name_parts(claims)
        if parts is None or len(parts) != 1:
            return
        name = parts[0]
        artist_mbids = self._values(claims, "artist_mbid")
        deezer_ids = self._values(claims, "deezer_id")
        # R2-SF-2: try every claimed MBID until one resolves to
        # an artist whose name matches — the first RESOLVING id
        # used to end the check even when it was a namesake.
        mb = None
        for mbid in self._ordered_ids(claims, "artist_mbid"):
            resp = self._response("musicbrainz", "artist_by_id", mbid)
            if resp is not None \
                    and _norm(resp.get("name")) == _norm(name):
                mb = resp
                break
        # Deezer artist lookups for the claimed Deezer ids: the
        # second independent resolution path §7.1's absence rule
        # needs (grading still runs through the MusicBrainz link
        # form above — a Deezer response alone grades nothing
        # here beyond feeding absence's asserted set).
        dz_responses = []
        for deezer_id in self._ordered_ids(claims, "deezer_id"):
            resp = self._response("deezer", "artist_by_id", deezer_id)
            if resp is not None:
                dz_responses.append(resp)
        asserted: set[tuple[str, Any]] = set()
        for resp in dz_responses:
            if resp.get("id"):
                asserted.add(("deezer_id", resp["id"]))
        if mb is not None:
            asserted.add(("artist_mbid", mb["id"]))
            self._grade(claims, "artist_mbid", mb["id"], TIER_PLAUSIBLE,
                        "single musicbrainz response", summary)
            linked = (mb.get("links") or {}).get("deezer")
            if linked:
                asserted.add(("deezer_id", linked))
            if linked and linked in deezer_ids:
                # §7's link form: the MusicBrainz response itself asserts
                # the mapping — this is also the FETCHED link §8's
                # disambiguation ladder settles on with one album.
                self._grade(claims, "deezer_id", linked, TIER_STRONG,
                            "musicbrainz links to this deezer artist",
                            summary)
                self._grade(claims, "artist_mbid", mb["id"], TIER_STRONG,
                            "musicbrainz artist carrying the link",
                            summary)
            linked_spotify = (mb.get("links") or {}).get("spotify")
            if linked_spotify:
                asserted.add(("spotify_id", linked_spotify))
                self._grade(claims, "spotify_id", linked_spotify,
                            TIER_STRONG,
                            "musicbrainz links to this spotify artist",
                            summary)
        # §7.1 absence (SF-10): a claimed artist id resolves
        # through MusicBrainz by id and through Deezer by the
        # claimed Deezer ids — two providers, with the secondary
        # paths marked by the id they were built from (the
        # vouching rule in _apply_absence governs them).
        paths: dict[tuple[str, Any],
                    list[tuple[str, str, str, Any]]] = {}
        for mbid in artist_mbids:
            paths[("artist_mbid", mbid)] = [
                ("musicbrainz", "artist_by_id", mbid, None)] + [
                ("deezer", "artist_by_id", d, ("deezer_id", d))
                for d in sorted(deezer_ids)]
        for deezer_id in deezer_ids:
            paths[("deezer_id", deezer_id)] = [
                ("deezer", "artist_by_id", deezer_id, None)] + [
                ("musicbrainz", "artist_by_id", m,
                 ("artist_mbid", m)) for m in sorted(artist_mbids)]
        self._apply_absence(subject, claims, summary, asserted,
                            paths)

    # -- §8's bounded sanity rule ------------------------------------------

    def note_conflict(self, origin_node: str, subject: str,
                      field: str) -> bool:
        """The sanity-rule gate: True when a conflict asserted by
        this origin may trigger ONE re-verification now. An
        origin with no passed check on record can never trigger
        (§8), and the (origin, subject, field) trigger is bounded
        by a ROLLING 24 h window in the store (R2-SF-8) — not a
        calendar day, which allowed two triggers minutes apart
        across midnight."""
        reputation = self.store.reputation_for(origin_node)
        if reputation["checks_passed"] < 1:
            return False
        return self.store.consume_sanity_trigger(
            origin_node, subject, field, self.clock())

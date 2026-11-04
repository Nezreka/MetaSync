"""Library builder: SoulSync export -> claims -> the local store.

Build step 4 of DESIGN-METADATA (§5, §5.1 G3, §13): PR #12's
contributor, with its push transport deleted. The node walks its own
SoulSync's metadata export, turns each entity into claims with the
frozen library (meta/claims), and writes them into the local claims
store (meta/store) — nothing leaves the machine. The responder
answers queries from the store; this module is how the store learns
the library.

Claim construction is PR #12's, unchanged (the export facts did not
change when the transport did):

- Provenance (G1): a provider ID is self-describing — a Spotify ID
  came from Spotify — so ID claims carry their provider as source,
  and are only emitted when SoulSync's match status for that
  provider is a resolved one ("matched"/"identifier", or absent on
  older rows). Scalar claims (name, year, genres, label) carry the
  entity's canonical_source when it names a provider; anything else
  falls back to "file_tags", the weakest source — never an optimistic
  one. Track durations literally come from the files: "file_tags",
  and the export's duration is ALREADY milliseconds (SoulSync's
  media-server wrappers normalize every family to ms).
- Album MBID claims name the release-GROUP id (§7): asserted only
  when the export serves `musicbrainz_release_group_id` itself,
  under the same resolved-status gating as every provider id. The
  release id is never asserted as a group id; it is still used —
  as the album_track variant label, where grouping is all it does:
  `mb:<release_id>` when the MusicBrainz match is resolved, else
  `canonical:<source>:<id>`, else `local`. album_track claims
  require a real disc/track number and a non-local variant.
- provider_link claims (§6): an album links its own resolved
  provider ids to each other (`same_entity`, one claim per
  direction) and, per provider it shares with its artist, to the
  artist's id on that provider (`album_of`). The artist's ids come
  from the artist rows walked before it and persist across walks,
  like the variant map; an album whose artist row was never walked
  mints no `album_of` links.

Walk shape: entities in order (artist, album, track), keyset cursor
pages. Each page lands in the store in ONE commit (its seen-owners
and all its claim replacements together) BEFORE the walk position
advances past that page, so the saved cursor only ever points at
pages whose claims are in the store; a stopped run resumes at the
first unwritten page, and a crash mid-page leaves the page wholly
unwritten, never half. Writes are per export row
(store.replace_owners_page): re-processing a row replaces exactly
its claims, so re-writing a page is idempotent and an incremental
walk can never duplicate. After one complete walk, later walks are
incremental via the export's `since` (which tracks parent rows too:
an artist rename re-exports its albums and tracks), with an hour of
overlap against clock skew — re-exports replace, so overlap costs
nothing. A completed FULL walk sweeps claims whose row the walk
never saw (§6: the node simply stops asserting them) — but the
sweep is guarded: an empty-but-well-formed export (zero rows seen
while the store holds claims) and any sweep that would drop over
half the store's own claims are refused as stopped builds, because
both shapes mean the export lied, not that the library shrank.
Incremental walks never see deletions (a row that simply stops
changing is indistinguishable from one that left), so a full walk
is due every FULL_WALK_INTERVAL_DAYS even when incrementals keep
completing.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..soulsync import SoulSyncError
from . import claims as meta_claims
from . import soulid as meta_soulid
from .store import MetaStore

ENTITIES = ("artist", "album", "track")
# Match statuses that mean "this provider link is resolved".
_RESOLVED_STATUSES = {None, "", "matched", "identifier"}
_PROVIDER_SOURCES = {
    "musicbrainz", "spotify", "deezer", "tidal", "itunes", "discogs",
    "hydrabase",
}

# (export key, claim field, provider) per kind.
_ARTIST_IDS = [
    ("musicbrainz_id", "artist_mbid", "musicbrainz"),
    ("spotify_artist_id", "spotify_id", "spotify"),
    ("deezer_id", "deezer_id", "deezer"),
    ("tidal_id", "tidal_id", "tidal"),
    ("itunes_artist_id", "itunes_id", "itunes"),
    ("discogs_id", "discogs_id", "discogs"),
]
_ALBUM_IDS = [
    # The album MBID field is the release-GROUP id (DESIGN §7),
    # asserted only from the export's own release-group field —
    # never from the release id, which is a different identifier
    # (it serves as the album_track variant label instead).
    ("musicbrainz_release_group_id", "release_group_mbid", "musicbrainz"),
    ("spotify_album_id", "spotify_id", "spotify"),
    ("deezer_id", "deezer_id", "deezer"),
    ("tidal_id", "tidal_id", "tidal"),
    ("itunes_album_id", "itunes_id", "itunes"),
    ("discogs_id", "discogs_id", "discogs"),
]
_TRACK_IDS = [
    ("musicbrainz_recording_id", "recording_mbid", "musicbrainz"),
    ("spotify_track_id", "spotify_id", "spotify"),
    ("deezer_id", "deezer_id", "deezer"),
    ("tidal_id", "tidal_id", "tidal"),
    ("itunes_track_id", "itunes_id", "itunes"),
]


def scalar_source(item: dict[str, Any]) -> str:
    """The §5.1 G1 source for a scalar claim from this export row."""
    raw = str(item.get("canonical_source") or "").strip().lower()
    return raw if raw in _PROVIDER_SOURCES else "file_tags"


def _claim(subject: str, kind: str, field: str, value: Any,
           source: str) -> dict[str, Any]:
    return {
        "subject": subject, "kind": kind, "field": field,
        "value": value, "source": source,
        "soulid_version": meta_claims.SOULID_VERSION,
    }


def _usable(item: dict[str, Any]) -> bool:
    soul_id = item.get("soul_id")
    return isinstance(soul_id, str) and soul_id.startswith("soul_") \
        and not soul_id.startswith("soul_unnamed_")


def _resolved_pairs(item: dict[str, Any],
                    mapping: list[tuple[str, str, str]],
                    ) -> list[tuple[str, str, str]]:
    """(field, provider, value) for the row's resolved provider
    ids: present, non-blank, and the provider's match status a
    resolved one. Shared by the id claims and the provider_link
    minting, so the two can never disagree about what "resolved"
    means."""
    out = []
    for key, field, provider in mapping:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        status = item.get(f"{provider}_match_status")
        if status not in _RESOLVED_STATUSES:
            continue
        out.append((field, provider, value.strip()))
    return out


def _id_claims(item: dict[str, Any], kind: str,
               mapping: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        _claim(item["soul_id"], kind, field, value, provider)
        for field, provider, value in _resolved_pairs(item, mapping)
    ]


def _genre_claims(item: dict[str, Any], kind: str,
                  source: str) -> list[dict[str, Any]]:
    out = []
    for genre in item.get("genres") or []:
        if isinstance(genre, str) and genre.strip():
            out.append(_claim(item["soul_id"], kind, "genres",
                              genre.strip(), source))
    return out


def _year_claim(item: dict[str, Any], kind: str,
                source: str) -> list[dict[str, Any]]:
    year = item.get("year")
    if isinstance(year, int) and not isinstance(year, bool) and year > 0:
        return [_claim(item["soul_id"], kind, "year", year, source)]
    return []


def artist_claims(item: dict[str, Any]) -> list[dict[str, Any]]:
    if not _usable(item) or not str(item.get("name") or "").strip():
        return []
    source = scalar_source(item)
    out = [_claim(item["soul_id"], "artist", "name",
                  [str(item["name"])], source)]
    out += _id_claims(item, "artist", _ARTIST_IDS)
    out += _genre_claims(item, "artist", source)
    return out


def album_link_claims(item: dict[str, Any],
                      artist_ids: dict[str, dict[str, str]],
                      ) -> list[dict[str, Any]]:
    """provider_link claims for one album row (§6): its own resolved
    provider ids name each other (`same_entity`, one claim per
    direction — the shape's `provider` names the from side), and
    each provider it shares with its artist names the artist's id
    on that provider (`album_of`). `artist_ids` is the walk's
    {artist SoulID: {provider: id}} map of resolved artist ids;
    providers either side has not resolved mint nothing."""
    by_provider = {
        provider: value
        for _field, provider, value in _resolved_pairs(item, _ALBUM_IDS)
    }
    out = []
    for from_provider in sorted(by_provider):
        for to_provider in sorted(by_provider):
            if to_provider == from_provider:
                continue
            out.append(_claim(
                item["soul_id"], "album", "provider_link",
                {"provider": from_provider,
                 "from_id": by_provider[from_provider],
                 "to_kind": "album",
                 "to_id": by_provider[to_provider],
                 "relation": "same_entity"},
                from_provider))
    artist_name = str(item.get("artist_name") or "")
    if artist_name.strip():
        artist_subject = meta_soulid.generate_soul_id(
            "artist", artist_name)
        theirs = artist_ids.get(artist_subject) or {}
        for provider in sorted(by_provider):
            if provider in theirs:
                out.append(_claim(
                    item["soul_id"], "album", "provider_link",
                    {"provider": provider,
                     "from_id": by_provider[provider],
                     "to_kind": "artist",
                     "to_id": theirs[provider],
                     "relation": "album_of"},
                    provider))
    return out


def album_claims(item: dict[str, Any],
                 artist_ids: dict[str, dict[str, str]] | None = None,
                 ) -> list[dict[str, Any]]:
    title = str(item.get("title") or "")
    artist = str(item.get("artist_name") or "")
    if not _usable(item) or not title.strip() or not artist.strip():
        return []
    source = scalar_source(item)
    out = [_claim(item["soul_id"], "album", "name",
                  [artist, title], source)]
    out += _id_claims(item, "album", _ALBUM_IDS)
    out += _year_claim(item, "album", source)
    label = str(item.get("label") or "").strip()
    if label:
        out.append(_claim(item["soul_id"], "album", "label", label, source))
    out += _genre_claims(item, "album", source)
    out += album_link_claims(item, artist_ids or {})
    return out


def album_variant(item: dict[str, Any] | None) -> str:
    """The tracklist variant label for an album: which release this
    tracklist belongs to, so different releases never merge into one
    variant and the same release always lands in the same one. A
    MusicBrainz release id only counts when its match is resolved —
    a pending or failed match must not decide the grouping."""
    if item:
        release = str(item.get("musicbrainz_release_id") or "").strip()
        if release and item.get("musicbrainz_match_status") \
                in _RESOLVED_STATUSES:
            return f"mb:{release}"
        canonical = str(item.get("canonical_album_id") or "").strip()
        if canonical:
            return f"canonical:{scalar_source(item)}:{canonical}"
    return "local"


def track_claims(item: dict[str, Any],
                 album_variants: dict[str, str],
                 album_sources: dict[str, str]) -> list[dict[str, Any]]:
    title = str(item.get("title") or "")
    artist = str(item.get("artist_name") or "")
    if not _usable(item) or not title.strip() or not artist.strip():
        return []
    source = scalar_source(item)
    out = [_claim(item["soul_id"], "track", "name",
                  [artist, title], source)]
    out += _id_claims(item, "track", _TRACK_IDS)
    isrc = str(item.get("isrc") or "").strip()
    if isrc:
        out.append(_claim(item["soul_id"], "track", "isrc", isrc, source))
    duration = item.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
            and duration > 0:
        # The export's duration is ALREADY milliseconds: SoulSync's
        # media-server track wrappers normalize every family to ms
        # (core/media_server/items.py MediaTrack), and that value is
        # what lands in tracks.duration. (The claim itself is a bare
        # int per the frozen claims contract; step 5's aggregation
        # joins the album_track variant context onto it.)
        out.append(_claim(item["soul_id"], "track", "duration_ms",
                          round(duration), "file_tags"))
    out += _year_claim(item, "track", source)
    album_soul_id = item.get("album_soul_id")
    disc = item.get("disc_number")
    number = item.get("track_number")
    variant = album_variants.get(album_soul_id, "local") \
        if isinstance(album_soul_id, str) else "local"
    # album_track claims assert exact slot data, so they need exact
    # inputs: a real disc and track number (never an invented
    # position) and a release-level variant identity — under the
    # shared "local" label, slots from different editions of the
    # same album would mix into one tracklist at merge time.
    if isinstance(album_soul_id, str) and album_soul_id.startswith("soul_") \
            and not album_soul_id.startswith("soul_unnamed_") \
            and isinstance(disc, int) and not isinstance(disc, bool) \
            and disc >= 1 \
            and isinstance(number, int) and not isinstance(number, bool) \
            and number >= 1 and variant != "local":
        out.append(_claim(
            album_soul_id, "album", "album_track",
            {"track": item["soul_id"], "disc": disc,
             "position": number, "variant": variant},
            album_sources.get(album_soul_id, "file_tags"),
        ))
    return out


def verified_legacy_alias(item: dict[str, Any],
                          entity: str) -> tuple[str, str] | None:
    """The row's verified legacy -> v2 mapping (§6), or None.

    The export serves both ids for a row (`soul_id` +
    `soul_id_legacy`, DESIGN §5.1); the mapping is only worth
    learning when it is VERIFIED — the v2 id recomputed from the
    row's own name parts (the same parts its name claim carries)
    must equal the served `soul_id`. Anything else — no legacy id,
    a legacy id equal to the v2 id, name parts that do not
    recompute — teaches the node nothing."""
    legacy = item.get("soul_id_legacy")
    if not isinstance(legacy, str) or not legacy.strip():
        return None
    legacy = legacy.strip()
    if not _usable(item) or legacy == item["soul_id"]:
        return None
    if entity == "artist":
        name = str(item.get("name") or "")
        parts = [name] if name.strip() else None
    else:
        artist = str(item.get("artist_name") or "")
        title = str(item.get("title") or "")
        parts = [artist, title] if artist.strip() and title.strip() \
            else None
    if parts is None:
        return None
    if meta_soulid.generate_soul_id(entity, *parts) != item["soul_id"]:
        return None
    return legacy, item["soul_id"]


def claims_for(entity: str, item: dict[str, Any],
               album_variants: dict[str, str],
               album_sources: dict[str, str],
               artist_ids: dict[str, dict[str, str]] | None = None,
               ) -> list[dict[str, Any]]:
    if entity == "artist":
        return artist_claims(item)
    if entity == "album":
        return album_claims(item, artist_ids)
    return track_claims(item, album_variants, album_sources)


class BuildStopped(Exception):
    """The build stopped early; `reason` is a stable status string."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


_STATE_KEY = "meta_builder"

# A completed incremental walk says nothing about deletions: only a
# full walk sweeps. One is due this often, however fresh the last
# incremental completion is.
FULL_WALK_INTERVAL_DAYS = 7


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class LibraryBuilder:
    """One claims-store build over the export walk.

    State (walk position, per-album variant/source maps, last
    totals) lives in the store's kv table under `meta_builder`, so
    it survives restarts with the store it describes. The service
    owns WHEN builds run; this class owns one run.
    """

    def __init__(self, soulsync: Any, identity: Any, store: MetaStore,
                 enabled_on: Callable[[], bool] | None = None,
                 attestation_provider: Callable[[], dict | None]
                 | None = None):
        self.soulsync = soulsync
        self.identity = identity
        self.store = store
        # Live re-read of the answering switch, consulted at every
        # page boundary: turning answering off mid-build stops the
        # build at the next page, not at the next scheduled run.
        # None (manual runs, tests) means no live gate.
        self.enabled_on = enabled_on
        # Live re-read of the node's current binding attestation
        # (§9), in the same pattern: the pool hands it to the node
        # at the token exchange, and a build that starts after a
        # renewal must mint with the new one — and re-mint the
        # subjects the renewal stranded (see run()'s prologue).
        self.attestation_provider = attestation_provider
        self.totals = {"claims_built": 0, "items_skipped": 0,
                       "rows": 0, "items_seen": 0, "claims_swept": 0}

    # -- state -----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        state = self.store.kv_get(_STATE_KEY)
        return state if isinstance(state, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.store.kv_set(_STATE_KEY, state)

    def record_outcome(self, status: str, detail: str | None = None) -> None:
        """The service records each run's outcome here, beside the
        walk state, so status survives restarts."""
        state = self._state()
        state["last_status"] = status
        state["last_detail"] = detail
        self._save_state(state)

    def status(self) -> dict[str, Any]:
        state = self._state()
        walk = state.get("walk") or {}
        return {
            "last_status": state.get("last_status"),
            "last_detail": state.get("last_detail"),
            "last_run_at": state.get("last_run_at"),
            "last_completed_at": state.get("last_completed_iso"),
            "last_full_completed_at": state.get(
                "last_full_completed_iso"),
            "last_totals": state.get("last_totals") or {},
            "walk_entity": walk.get("entity") or "",
            "full": not (state.get("walk") or {}).get("since"),
        }

    def full_walk_due(self, now: datetime) -> bool:
        """A full walk is due when none has completed within
        FULL_WALK_INTERVAL_DAYS — incremental completions do not
        count, they cannot see deletions. While ANY walk is
        mid-flight this is False: the in-flight walk decides, and
        forcing a reset would throw its progress (and its sweep's
        seen-set) away."""
        state = self._state()
        walk = state.get("walk") or {}
        if walk.get("entity"):
            return False
        last = _parse_iso(state.get("last_full_completed_iso"))
        if last is None:
            return True
        return now - last >= timedelta(days=FULL_WALK_INTERVAL_DAYS)

    # -- one row -----------------------------------------------------------

    def _apply_row(self, state: dict[str, Any], entity: str,
                   item: dict[str, Any],
                   ) -> tuple[str | None, list[dict] | None]:
        """Build one export row's claims.

        Returns (owner, claims): claims is the row's new claim set
        (possibly empty — a usable row that no longer produces
        claims still replaces its old set with nothing), or None
        when the row was skipped. The OWNER is returned either way
        (and for skipped rows too): on a full walk the sweep decides
        what left the library by who was SEEN, and a row whose new
        claims fail validation was still seen — its prior claims
        stay, its new ones are skipped whole and counted — one bad
        row must never stop every build at the same page, nor cost
        the row its previously-good claims to the sweep."""
        if not _usable(item):
            return None, None
        owner = f"{entity}:{item['soul_id']}"
        album_variants: dict[str, str] = state.get("album_variants") or {}
        album_sources: dict[str, str] = state.get("album_sources") or {}
        artist_ids: dict[str, dict[str, str]] = \
            state.get("artist_ids") or {}
        claims = claims_for(entity, item, album_variants, album_sources,
                            artist_ids)
        try:
            for claim in claims:
                meta_claims.validate_claim(claim)
        except meta_claims.ClaimError:
            self.totals["items_skipped"] += 1
            return owner, None
        # §6: a row serving both ids teaches the alias table — but
        # only a VERIFIED mapping (the v2 id recomputes from the
        # row's name parts), and only for a row whose claims this
        # build actually stands behind.
        alias = verified_legacy_alias(item, entity)
        if alias is not None:
            self.store.learn_legacy_alias(alias[0], alias[1])
        self.totals["claims_built"] += len(claims)
        self.totals["rows"] += 1
        # The variant/source maps are recorded only for an album that
        # actually produced claims: a row skipped whole by validation
        # must not lend its identity to its tracks' album_track
        # claims — those would assert slots for an album this build
        # refused to assert at all.
        if entity == "album" and claims:
            album_variants[item["soul_id"]] = album_variant(item)
            album_sources[item["soul_id"]] = scalar_source(item)
            state["album_variants"] = {
                k: v for k, v in album_variants.items() if v != "local"}
            state["album_sources"] = {
                k: v for k, v in album_sources.items()
                if v != "file_tags"}
        # The artist map feeds the albums' album_of links (above,
        # same recorded-only-for-a-row-that-produced-claims rule):
        # an artist row re-walked with fewer resolved ids shrinks
        # its entry, so its albums' next rebuild drops the links
        # the artist no longer backs.
        if entity == "artist" and claims:
            resolved = {
                provider: value
                for _field, provider, value
                in _resolved_pairs(item, _ARTIST_IDS)}
            if resolved:
                artist_ids[item["soul_id"]] = resolved
            else:
                artist_ids.pop(item["soul_id"], None)
            state["artist_ids"] = artist_ids
        return owner, claims

    # -- the walk -----------------------------------------------------------

    def run(self, now_iso: str, force_full: bool = False) -> dict[str, Any]:
        # Prologue: an attestation that arrived since the last build
        # is noted in the store, and any subject whose stored batches
        # still embed an older one is re-minted (§6: re-minting is
        # what keeps a live library's answers standing-bearing).
        # The walk's own remints then embed the current value as
        # they go, because the store reads it at mint time.
        if self.attestation_provider is not None:
            # Failure-isolated (SF-2): a raising provider degrades to
            # "no attestation this run" — it must never abort the
            # walk itself, which is what an exception here would do
            # (this prologue runs before the walk's own error
            # handling converts failures into recorded status).
            try:
                attestation = self.attestation_provider()
            except Exception:  # noqa: BLE001 - degrade, never break
                attestation = None
            if attestation is not None and \
                    self.store.set_attestation(attestation):
                self.store.refresh_attestation_embeddings(
                    self.identity.public_key_b64url,
                    self.identity.signing_private_key)
        state = self._state()
        if force_full:
            # A forced restart abandons whatever walk was mid-flight:
            # clear its seen-owner rows rather than leaving them to
            # accumulate against a walk id nothing will resume. (A
            # completed walk has no started_iso left in state, and a
            # full walk's sweep already cleared its own rows.)
            abandoned = (state.get("walk") or {}).get("started_iso")
            if abandoned:
                self.store.walk_clear(str(abandoned))
            walk = {}
            state["walk"] = {}
        else:
            walk = dict(state.get("walk") or {})
        entity = walk.get("entity") or ENTITIES[0]
        cursor = walk.get("cursor") or ""
        since = walk.get("since") or ""
        full = not since
        if entity == ENTITIES[0] and not cursor \
                and not walk.get("started_iso"):
            walk["started_iso"] = now_iso
        walk_id = str(walk.get("started_iso") or now_iso) if full else None
        state["last_run_at"] = now_iso
        try:
            while entity in ENTITIES:
                if self.enabled_on is not None and not self.enabled_on():
                    raise BuildStopped(
                        "answering_off",
                        "answering was turned off during the build")
                page = self.soulsync.export_page(entity, cursor=cursor,
                                                 since=since)
                version = page.get("soulid_version")
                if version != meta_claims.SOULID_VERSION:
                    raise BuildStopped(
                        "soulid_not_v2",
                        f"{entity} export is on {version or 'no version'}; "
                        "SoulSync's SoulID v2 cutover has not run for it")
                items = page.get("items") or []
                self.totals["items_seen"] += len(items)
                seen_owners: list[str] = []
                entries: list[tuple[str, list[dict]]] = []
                for item in items:
                    owner, row_claims = self._apply_row(state, entity,
                                                        item)
                    if owner is None:
                        continue
                    seen_owners.append(owner)
                    if row_claims is not None:
                        entries.append((owner, row_claims))
                # The page lands whole, before the position moves
                # past it: its seen-owners and all its claim
                # replacements commit together, so the saved cursor
                # only ever points at written pages and a crash
                # mid-page leaves nothing of the page behind.
                if walk_id is not None and seen_owners:
                    self.store.walk_note_owners(walk_id, seen_owners)
                if entries:
                    self.store.replace_owners_page(
                        entries, self.identity.public_key_b64url,
                        self.identity.signing_private_key)
                if page.get("has_more"):
                    next_cursor = str(page.get("next_cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        raise BuildStopped(
                            "export_cursor_stuck",
                            "the export claims more pages but its "
                            "cursor does not advance")
                    cursor = next_cursor
                else:
                    idx = ENTITIES.index(entity)
                    entity = ENTITIES[idx + 1] if idx + 1 < len(ENTITIES) \
                        else ""
                    cursor = ""
                walk.update({"entity": entity, "cursor": cursor,
                             "since": since})
                state["walk"] = dict(walk)
                self._save_state(state)
        except SoulSyncError as err:
            state["last_totals"] = dict(self.totals)
            self._save_state(state)
            if err.code == "HTTP_404" or err.status == 404:
                raise BuildStopped(
                    "export_unavailable",
                    "this SoulSync build has no metadata export "
                    "(it predates the SoulID v2 work)") from err
            raise BuildStopped("soulsync_error", str(err)) from err
        except BuildStopped:
            state["last_totals"] = dict(self.totals)
            self._save_state(state)
            raise
        # Walk complete. A FULL walk saw every row: sweep the claims
        # whose rows it never saw — they left the library, and the
        # node simply stops asserting them (§6). Two shapes refuse
        # the sweep as a stopped build instead: the walk saw ZERO
        # rows while the store holds claims (an empty-but-well-
        # formed export is a broken export, not an empty library),
        # and a sweep that would drop over half the store's own
        # claims. A refused sweep deletes nothing, clears the
        # walk's seen-owners, and leaves the walk pending (the
        # incremental cursor does NOT advance past a walk whose
        # sweep was refused).
        if full and walk_id is not None:
            own = self.store.counts()["own_claims"]
            if own > 0 and self.store.walk_owner_count(walk_id) == 0:
                state["walk"] = {}
                state["last_totals"] = dict(self.totals)
                self._save_state(state)
                self.store.walk_clear(walk_id)
                raise BuildStopped(
                    "empty_walk",
                    "the export returned no rows but the store holds "
                    "claims; refusing to sweep the library away")
            unseen = self.store.walk_unseen_claims(walk_id)
            if own > 0 and unseen > own / 2:
                state["walk"] = {}
                state["last_totals"] = dict(self.totals)
                self._save_state(state)
                self.store.walk_clear(walk_id)
                raise BuildStopped(
                    "sweep_refused",
                    f"the full walk never saw rows holding {unseen} "
                    f"of {own} own claims; refusing to sweep over "
                    "half the library away")
            self.totals["claims_swept"] = self.store.walk_sweep(
                walk_id, self.identity.public_key_b64url,
                self.identity.signing_private_key)
            state["last_full_completed_iso"] = now_iso
        # The next walk is incremental from this one's start, minus
        # an hour of clock-skew overlap (re-exports replace per
        # owner, so overlap costs nothing).
        started = walk.get("started_iso") or now_iso
        try:
            since_dt = datetime.fromisoformat(started) \
                - timedelta(hours=1)
            next_since = since_dt.isoformat()
        except ValueError:
            next_since = started
        state["walk"] = {"entity": "", "cursor": "", "since": next_since}
        state["last_completed_iso"] = now_iso
        state["last_totals"] = dict(self.totals)
        self._save_state(state)
        return {**self.totals, "status": "completed"}

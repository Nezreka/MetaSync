"""Metadata contributor: SoulSync export -> signed claims -> ingest.

Build step 4 of DESIGN-METADATA (§5.1 G3, §6, §13): the node walks
its own SoulSync's metadata export, turns each entity into claims
with the frozen library (metasync_node.meta.claims), signs one batch
per 5,000 claims with the node key, and posts it to the metadata
service through the pool client's token door.

Provenance rules (G1): a provider ID is self-describing — a Spotify
ID came from Spotify — so ID claims carry their provider as source,
and are only emitted when SoulSync's match status for that provider
is a resolved one ("matched"/"identifier", or absent on older rows).
Scalar claims (name, year, genres, label, duration) carry the
entity's canonical_source when it names a provider; anything else
falls back to "file_tags", the weakest source — never an optimistic
one. Track durations literally come from the files: "file_tags".

Scale shape: the walk streams page by page. Each page's claims are
flushed (sent) BEFORE the walk position advances past that page, so
the saved cursor only ever points at pages whose claims the service
has accepted; a stopped run — not approved, allowance spent, crash —
resumes at the first unsent page and the backfill is never silently
skipped. Re-sending is safe (the ingest dedupes per claim). After
one complete walk, later walks are incremental via the export's
`since` (which tracks parent rows too: an artist rename re-exports
its albums and tracks), with an hour of overlap against clock skew.

Nothing here runs unless the user turned metadata sharing on
(settings share_metadata); there are no retractions in v1.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .meta import claims as meta_claims
from .soulsync import SoulSyncError

BATCH_SIZE = 5_000  # steady-state batch cap; the server enforces the rest
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
    # No release_mbid claim exists in v1: the MBID field for albums is
    # the release-GROUP id (DESIGN §7), the export serves only the
    # release id, and a release id asserted as a group id would be a
    # false claim. The release id is still used — as the album_track
    # variant label, where grouping is all it does.
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


def _id_claims(item: dict[str, Any], kind: str,
               mapping: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    out = []
    for key, field, provider in mapping:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        status = item.get(f"{provider}_match_status")
        if status not in _RESOLVED_STATUSES:
            continue
        out.append(_claim(item["soul_id"], kind, field, value.strip(),
                          provider))
    return out


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


def album_claims(item: dict[str, Any]) -> list[dict[str, Any]]:
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
        # int per the frozen claims contract; the compiler joins the
        # album_track variant context onto it at aggregation time —
        # the raw-vs-aggregated seam noted for the compiler step.)
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


class ContributionStopped(Exception):
    """The run stopped early; `reason` is a stable status string."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class Contributor:
    """One contribution run over the export walk.

    `state` is the persisted walk state (owned by the service's
    store); the contributor mutates it and calls `save()` at page
    boundaries so a crash loses at most one page of progress.
    """

    def __init__(self, soulsync: Any, pool_client: Any, identity: Any,
                 meta_base: str, state: dict[str, Any],
                 save: Callable[[], None],
                 sharing_on: Callable[[], bool] | None = None):
        self.soulsync = soulsync
        self.pool = pool_client
        self.identity = identity
        self.meta_base = meta_base
        self.state = state
        self.save = save
        # Live re-read of the share_metadata switch, consulted before
        # every send: turning sharing off mid-run stops the run at
        # the next batch, not at the next scheduled run. None (the
        # preview, tests) means no live gate.
        self.sharing_on = sharing_on
        self.buffer: list[dict[str, Any]] = []
        self.totals = {"accepted": 0, "duplicates": 0, "batches": 0,
                       "claims_built": 0}

    # -- sending -----------------------------------------------------

    def _flush(self, force: bool = False) -> None:
        while self.buffer and (force or len(self.buffer) >= BATCH_SIZE):
            if self.sharing_on is not None and not self.sharing_on():
                raise ContributionStopped(
                    "sharing_off",
                    "metadata sharing was turned off during the run")
            chunk, self.buffer = self.buffer[:BATCH_SIZE], self.buffer[BATCH_SIZE:]
            # One name claim per subject per batch (the contract's
            # batch rule): the same track on two albums, or two
            # spellings of one artist, legitimately produce two —
            # keep the first in the chunk, the rest of the item's
            # claims still ride.
            seen_names: set[str] = set()
            deduped = []
            for claim in chunk:
                if claim["field"] == "name":
                    if claim["subject"] in seen_names:
                        continue
                    seen_names.add(claim["subject"])
                deduped.append(claim)
            batch = meta_claims.make_batch(
                self.identity.public_key_b64url, deduped)
            envelope = meta_claims.sign_batch(
                self.identity.signing_private_key, batch)
            status, body = self.pool.post_meta_claims(envelope, self.meta_base)
            body = body if isinstance(body, dict) else {}
            if status in (200, 201):
                self.totals["accepted"] += int(body.get("accepted") or 0)
                self.totals["duplicates"] += int(body.get("duplicates") or 0)
                self.totals["batches"] += 1
                continue
            if status == 403 and body.get("error") == \
                    "contributor_not_approved":
                raise ContributionStopped(
                    "not_approved",
                    "this account is not contributor-approved yet")
            if status == 403:
                raise ContributionStopped(
                    "http_403", "the metadata service refused the batch")
            if status == 401:
                raise ContributionStopped(
                    "auth_failed", "the pool refused the contribution token")
            if status == 409:
                raise ContributionStopped(
                    "busy", "another contribution is in progress; retry later")
            if status == 429:
                raise ContributionStopped(
                    "allowance_spent",
                    "daily claim allowance spent; the walk resumes tomorrow")
            raise ContributionStopped(
                f"http_{status}", f"metadata service answered HTTP {status}")

    # -- the walk ------------------------------------------------------

    def _claims_for(self, entity: str, item: dict[str, Any],
                    album_variants: dict[str, str],
                    album_sources: dict[str, str]) -> list[dict[str, Any]]:
        """One export row's claims, validated. A row whose claims the
        frozen contract refuses (a stale SoulID after a rename, a NUL
        in a tag, an over-long name) is skipped whole and counted —
        one bad row must never stop every run at the same chunk."""
        if entity == "artist":
            claims = artist_claims(item)
        elif entity == "album":
            claims = album_claims(item)
        else:
            claims = track_claims(item, album_variants, album_sources)
        try:
            for claim in claims:
                meta_claims.validate_claim(claim)
        except meta_claims.ClaimError:
            self.totals["items_skipped"] = \
                self.totals.get("items_skipped", 0) + 1
            return []
        return claims

    def _save_walk(self, walk: dict[str, Any],
                   album_variants: dict[str, str],
                   album_sources: dict[str, str]) -> None:
        """Publish the walk position and the per-album maps by
        wholesale assignment, so a concurrent save_state in another
        thread serializes a complete snapshot, never a mutating one.
        Default-valued map entries are not stored (a missing entry
        already reads as variant "local" / source "file_tags")."""
        self.state["walk"] = dict(walk)
        self.state["album_variants"] = {
            k: v for k, v in album_variants.items() if v != "local"}
        self.state["album_sources"] = {
            k: v for k, v in album_sources.items() if v != "file_tags"}
        self.save()

    def run(self, now_iso: str) -> dict[str, Any]:
        walk = dict(self.state.get("walk") or {})
        entity = walk.get("entity") or ENTITIES[0]
        cursor = walk.get("cursor") or ""
        since = walk.get("since") or ""
        if entity == ENTITIES[0] and not cursor \
                and not walk.get("started_iso"):
            walk["started_iso"] = now_iso
        # Local working copies; published at save points. The maps
        # persist across walks: an incremental walk re-exports a
        # changed track but not its unchanged album, and the slot
        # claim still needs the album's real variant and source.
        album_variants: dict[str, str] = dict(
            self.state.get("album_variants") or {})
        album_sources: dict[str, str] = dict(
            self.state.get("album_sources") or {})
        try:
            while entity in ENTITIES:
                page = self.soulsync.export_page(entity, cursor=cursor,
                                                 since=since)
                version = page.get("soulid_version")
                if version != meta_claims.SOULID_VERSION:
                    raise ContributionStopped(
                        "soulid_not_v2",
                        f"{entity} export is on {version or 'no version'}; "
                        "SoulSync's SoulID v2 cutover has not run for it")
                for item in page.get("items") or []:
                    claims = self._claims_for(entity, item, album_variants,
                                              album_sources)
                    # The variant/source maps are recorded only for
                    # an album that actually produced claims: a row
                    # skipped whole by validation (a stale SoulID
                    # after a rename, an untitled album) must not
                    # lend its identity to its tracks' album_track
                    # claims — those would assert slots for an album
                    # this run refused to assert at all.
                    if entity == "album" and claims:
                        album_variants[item["soul_id"]] = album_variant(item)
                        album_sources[item["soul_id"]] = scalar_source(item)
                    self.buffer.extend(claims)
                    self.totals["claims_built"] += len(claims)
                # The page's claims go out BEFORE the position moves
                # past it: the saved cursor only ever points at pages
                # the service has accepted, so a stopped run (403,
                # 429, crash) resumes at the first unsent page and
                # an unapproved account cannot walk its backfill away.
                #
                # Ingest contract this relies on (PR-D must keep
                # it): a page can span several batches, so a stop
                # mid-page (429) means the next run re-sends the
                # page's earlier batches. Those re-sends MUST come
                # back as per-claim duplicates that do not count
                # against the daily allowance — the same rule the
                # `since` overlap already assumes ("overlap costs
                # nothing"). If duplicates were charged, a page
                # bigger than one batch could wedge forever:
                # re-spending the whole allowance on duplicates
                # before its last batch ever gets through.
                self._flush(force=True)
                if page.get("has_more"):
                    next_cursor = str(page.get("next_cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        raise ContributionStopped(
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
                self._save_walk(walk, album_variants, album_sources)
        except SoulSyncError as err:
            self.state["last_totals"] = dict(self.totals)
            if err.code == "HTTP_404" or err.status == 404:
                raise ContributionStopped(
                    "export_unavailable",
                    "this SoulSync build has no metadata export "
                    "(it predates the SoulID v2 work)") from err
            raise ContributionStopped("soulsync_error", str(err)) from err
        except ContributionStopped:
            self.state["last_totals"] = dict(self.totals)
            self.save()
            raise
        # Walk complete: the next walk is incremental from this one's
        # start, minus an hour of clock-skew overlap (re-exports are
        # deduped by the ingest, so overlap costs nothing).
        started = walk.get("started_iso") or now_iso
        try:
            since_dt = datetime.fromisoformat(started) \
                - timedelta(hours=1)
            next_since = since_dt.isoformat()
        except ValueError:
            next_since = started
        self._save_walk({"entity": "", "cursor": "", "since": next_since},
                        album_variants, album_sources)
        self.state["last_completed_iso"] = now_iso
        self.save()
        return {**self.totals, "status": "completed"}

    # -- preview -------------------------------------------------------

    def preview(self) -> dict[str, Any]:
        """Build (never send) the first page of each entity: counts by
        field plus a small claim sample, so the user sees exactly what
        a run would contribute before turning sharing on."""
        counts: dict[str, int] = {}
        sample: list[dict[str, Any]] = []
        notes: list[str] = []
        album_variants: dict[str, str] = {}
        album_sources: dict[str, str] = {}
        for entity in ENTITIES:
            try:
                page = self.soulsync.export_page(entity, limit=200)
            except SoulSyncError as err:
                notes.append(f"{entity}: export unavailable ({err})")
                continue
            if page.get("soulid_version") != meta_claims.SOULID_VERSION:
                notes.append(
                    f"{entity}: SoulID v2 cutover has not run "
                    f"({page.get('soulid_version') or 'no version'})")
                continue
            for item in page.get("items") or []:
                claims = self._claims_for(entity, item, album_variants,
                                          album_sources)
                if entity == "album" and claims:
                    album_variants[item["soul_id"]] = album_variant(item)
                    album_sources[item["soul_id"]] = scalar_source(item)
                for claim in claims:
                    counts[claim["field"]] = counts.get(claim["field"], 0) + 1
                    if len(sample) < 25:
                        sample.append(claim)
        return {"counts": counts, "sample": sample, "notes": notes,
                "page_size": 200}

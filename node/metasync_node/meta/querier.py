"""The querier — DESIGN-METADATA.md §5/§6/§9, step 6's core.

Asking is cache-first. A query resolves its subjects locally
(SoulID, provider ref through the provider index, legacy id ONLY
through a verified alias), reads own claims + the answer cache,
and only subjects with nothing local go to the relay. Answers
stream in and walk §9 admission (meta/admission.py) as they
arrive; admitted batches land in the cache, and the merged view
(meta/merge.py) is built from the store afterwards — so a second
identical query is answered entirely from cache with zero relay
queries, and a revocation that lands between queries purges the
cache and the next view simply no longer contains the revoked
origin's claims (views are computed, never materialized, so the
purge IS the invalidation).

Verification (§7) runs AFTER answers land: streamed results are
unverified first, verdicts upgrade the cache as checks complete,
and the next read shows the tiers. With verify_inline the checks
run synchronously before the view is built (tests, and any caller
that wants the settled view); in service the default is a single
background thread. Subjects whose strong verdicts are older than
90 days are re-verified before reliance (§6), and a network
challenge to a strong-verified value can trigger one bounded
re-check per origin per rolling 24 h (§8's sanity rule, gated by
meta/verify.py's note_conflict).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..relay import RelayError, RelayNotConnected
from . import admission as meta_admission
from . import claims as meta_claims
from . import merge as meta_merge
from .responder import Responder, _normalize_provider_ref
from .store import MetaStore, provider_refs, value_key
from .verify import VERIFIABLE_FIELDS

# §13 step 6: cache eviction is by reputation (meta/store.py's
# prune_answer_cache). The cap is generous — eviction is a size
# discipline, not a freshness one (aging handles freshness).
MAX_CACHED_BATCHES = 2000

# Early close (post-v2, DATA.md §19): the support bar one merged
# IDENTITY value must clear before an ask may call a subject
# SUFFICIENT and close its query early. Identity fields are the
# codebase's own set (claims.py: provider ids, MBIDs, ISRC) — the
# fields that say WHICH entity this is. Agreement on anything else
# never suffices: a SoulID subject's name is what its id is
# computed from, so a crowd can unanimously agree on the name
# while every provider id is still contested, and contested ids
# are exactly what the rest of the window is for. Support is
# merge's own count — distinct standing origins (attested
# ≥30 days, signature verified), own key excluded,
# `network`-sourced rows excluded — the crowd signal the merged
# view already computes, so the check runs on a merge that has
# actually happened, never on raw arrivals. The plan's example
# bar was "5 strong-verified answers": verification (§7) runs
# only AFTER an ask returns, so a freshly admitted batch carries
# no verdict mid-stream and "strong-verified" cannot be evaluated
# for the answers the close would cut off; support ≥ 5 distinct
# standing origins on an identity value is the honest
# pre-verification translation — five independent, attested
# libraries asserting the same identity. Two narrowings ride on
# that general bar (DATA.md §19): a verbatim provider ref needs
# the MATCHING identity value supported on its resolved subject,
# because the ref→subject link itself may have no standing; and
# the stream must contain at least two direct answerers, because
# forwarded replays alone may all have arrived through one node.
EARLY_CLOSE_SUPPORT = 5

_IDENTITY_FIELDS = frozenset(
    meta_claims.PROVIDER_ID_FIELDS + meta_claims.MBID_FIELDS + ("isrc",)
)

_STALE_S = meta_merge.VERIFICATION_STALE_S

# Artist-subject id fields -> the provider their ids belong to
# (the candidate keys of §8's disambiguation ladder).
_ARTIST_ID_PROVIDERS = {
    "artist_mbid": "musicbrainz",
    "deezer_id": "deezer",
    "spotify_id": "spotify",
    "tidal_id": "tidal",
    "itunes_id": "itunes",
    "discogs_id": "discogs",
    "hydrabase_id": "hydrabase",
}


class Querier:
    def __init__(self, store: MetaStore, identity: Any, *,
                 relay_provider: Callable[[], Any] | None = None,
                 verifier: Any = None,
                 responder: Responder | None = None,
                 clock: Callable[[], float] = time.time,
                 issuer_key_provider: Callable[[], Any] | None = None,
                 own_issuer_provider: Callable[[], str | None]
                 | None = None,
                 verify_inline: bool = False):
        self.store = store
        self.identity = identity
        self.relay_provider = relay_provider
        self.verifier = verifier
        self.clock = clock
        self.issuer_key_provider = issuer_key_provider
        self.own_issuer_provider = own_issuer_provider
        self.verify_inline = verify_inline
        # Resolution reuses the responder's exact matchers (one
        # implementation of the §6 matching rules, shared by the
        # answering and asking sides). The leech mode provider is
        # inert: resolution never consults the mode.
        self.responder = responder or Responder(
            store, identity, mode_provider=lambda: "leech")
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self.last_verify_error: str | None = None

    # -- admission (the streaming door) --------------------------------

    def admit(self, envelope: Any, *, now: float | None = None,
              ) -> meta_admission.Admission:
        """§9 admission for one received envelope, wired with this
        node's trusted issuer key + id when the providers are set:
        standing is granted only through an attestation signature
        that verifies against them (MF-1); unset providers mean
        nothing can verify, so every attestation admits at zero
        support."""
        issuer_key = None
        own_issuer = None
        if self.issuer_key_provider is not None:
            try:
                issuer_key = self.issuer_key_provider()
            except Exception:  # noqa: BLE001 - degrade to shape checks
                issuer_key = None
        if self.own_issuer_provider is not None:
            try:
                own_issuer = self.own_issuer_provider()
            except Exception:  # noqa: BLE001 - degrade to shape checks
                own_issuer = None
        return meta_admission.admit_envelope(
            self.store, envelope,
            now=self.clock() if now is None else now,
            issuer_key=issuer_key, own_issuer=own_issuer)

    # -- row assembly (store -> merge inputs) ----------------------------

    def _assemble(self, subjects: list[str]):
        """(rows_by_subject, slot_rows_by_track, needs_verify,
        needs_recheck, live_subjects, track_titles).

        Verdicts attach to their claim identities here. A subject
        NEEDS VERIFY when it holds UNEXPIRED cached claims §7 can
        actually grade (verify.VERIFIABLE_FIELDS) with no verdict
        yet — expired rows are dead (§6): scheduling them would
        let a fresh verdict resurrect a claim its origin may have
        retracted, and fields that can never receive verdicts
        (name, genres, display_name) must not hold the subject in
        the queue forever (gate MF-1/SF-6). It NEEDS RECHECK when
        a strong verdict is older than §6's reliance window — the
        re-check runs FORCED (MF-2: past the 90-day fetch cache),
        because a re-check that replays cached responses can
        never overturn anything.

        LIVE subjects (MF-3) hold at least one row that survives
        §6 expiry — merge's own _row_expired predicate, so the ask
        path and the merge can never disagree about what counts.
        A subject whose only cached claims are expired is NOT
        live: the ask paths treat it as missing and re-ask, even
        though its rows still assemble (they produce the expired
        shell record, and expire out of the merge)."""
        now = self.clock()
        verdicts = self.store.verdicts_for_subjects(subjects)
        rows_by_subject: dict[str, list[dict[str, Any]]] = {}
        needs_verify: set[str] = set()
        needs_recheck: set[str] = set()
        for subject in subjects:
            rows: list[dict[str, Any]] = []
            for claim in self.store.own_claims(subject):
                rows.append({**claim, "origin": None, "batch_id": None,
                             "asserted_at": None, "standing": False,
                             "verdict": None})
            rows_by_subject[subject] = rows
        for row in self.store.cached_claims(subjects):
            verdict = verdicts.get(
                (row["origin_node"], row["subject"], row["field"],
                 value_key(row["value"])))
            assembled = {
                "subject": row["subject"], "kind": row["kind"],
                "field": row["field"], "value": row["value"],
                "source": row["source"], "origin": row["origin_node"],
                "batch_id": row["batch_id"],
                "asserted_at": row["asserted_at"],
                "standing": row["standing"], "verdict": verdict,
            }
            if verdict is None:
                if row["field"] in VERIFIABLE_FIELDS \
                        and not meta_merge._row_expired(assembled, now):
                    needs_verify.add(row["subject"])
            elif verdict["tier"] == meta_merge.TIER_STRONG and \
                    now - verdict["verified_at"] > _STALE_S:
                needs_recheck.add(row["subject"])
            rows_by_subject[row["subject"]].append(assembled)
        slots_by_track: dict[str, list[dict[str, Any]]] = {}
        for claim in self.store.album_track_claims_for_tracks(subjects):
            value = claim["value"]
            if isinstance(value, dict) and value.get("track"):
                slots_by_track.setdefault(value["track"], []).append(
                    {**claim, "origin": None, "batch_id": None,
                     "asserted_at": None, "standing": False,
                     "verdict": None})
        for row in self.store.cached_slot_claims(subjects):
            value = row["value"]
            if not isinstance(value, dict) or not value.get("track"):
                continue
            verdict = verdicts.get(
                (row["origin_node"], row["subject"], row["field"],
                 value_key(value)))
            slots_by_track.setdefault(value["track"], []).append({
                "subject": row["subject"], "kind": row["kind"],
                "field": row["field"], "value": value,
                "source": row["source"], "origin": row["origin_node"],
                "batch_id": row["batch_id"],
                "asserted_at": row["asserted_at"],
                "standing": row["standing"], "verdict": verdict,
            })
        live: set[str] = set()
        for subject, rows in rows_by_subject.items():
            if any(not meta_merge._row_expired(row, now)
                   for row in rows):
                live.add(subject)
        for track, slot_rows in slots_by_track.items():
            slots_by_track[track] = [
                row for row in slot_rows
                if not meta_merge._row_expired(row, now)]
        # Track titles for the §7 variant fingerprint (R2-SF-11):
        # the tracks an album_track row references, titled by
        # their own name claims (own library first, then cache).
        referenced: set[str] = set()
        for rows in rows_by_subject.values():
            for row in rows:
                if row["field"] == "album_track" \
                        and isinstance(row["value"], dict) \
                        and row["value"].get("track"):
                    referenced.add(row["value"]["track"])
        track_titles: dict[str, str] = {}
        if referenced:
            name_rows: list[dict[str, Any]] = []
            for track in sorted(referenced):
                name_rows.extend(self.store.own_claims(track))
            name_rows.extend(
                self.store.cached_claims(sorted(referenced)))
            for row in name_rows:
                if row["field"] != "name" \
                        or row["subject"] in track_titles:
                    continue
                value = row["value"]
                if isinstance(value, list) and len(value) == 2:
                    track_titles[row["subject"]] = str(value[1])
        return rows_by_subject, slots_by_track, needs_verify, \
            needs_recheck, live, track_titles

    def _record(self, subject: str, kind: str | None,
                rows_by_subject, slots_by_track,
                track_titles=None, closures=None) -> dict[str, Any]:
        rows = rows_by_subject.get(subject, [])
        record_kind = kind
        if rows:
            record_kind = rows[0]["kind"]
        record = meta_merge.merge_subject(
            subject, record_kind or "track", rows, now=self.clock(),
            own_key=self.identity.public_key_b64url,
            slot_rows=slots_by_track.get(subject, []),
            track_titles=track_titles)
        # MF-3: found is computed AFTER §6 expiry filtering — a
        # subject whose rows all expired merged to no fields, and
        # reporting it found would pose a ghost as knowledge (and
        # suppress the re-ask that replaces it).
        record["found"] = bool(rows) and bool(record["fields"])
        # §9 presentation (R1-SF-5): tier precedence preserved,
        # reputation orders only the unverified tail.
        meta_merge.apply_display_order(record,
                                       self.store.reputation_for)
        if record_kind == "artist":
            block, closure = self._artist_disambiguation(record,
                                                        rows)
            if block is not None:
                record["disambiguation"] = block
            if closures is not None and closure:
                closures.setdefault(subject, set()).update(closure)
        return record

    # -- §8 disambiguation (R2-SF-7) --------------------------------------

    def _album_of_rows(self, album: str, provider: str,
                       artist_ext_id: str) -> list[dict[str, Any]]:
        """The album_of provider_link rows on one album subject
        pointing at one artist id on one provider — own rows
        (origin None) and cached rows with their §7 verdicts
        attached, in the shape merge.disambiguate_artist reads.
        Expired cached rows are dead (§6, gate MF-1): they are
        not settlement evidence. Every row is also marked against
        the USER'S OWN data for the album (gate r2 MF-2):
        `own_id_match` — the link's from_id is a provider id the
        user's own claims assert for this album; and
        `own_contradiction` — the user's own album_of data links
        this album, on this provider, to a DIFFERENT artist.
        §8's fetched-link rung settles only on a strong-verdict
        row carrying the first mark and not the second."""

        def is_link(value: Any) -> bool:
            return isinstance(value, dict) \
                and value.get("relation") == "album_of" \
                and value.get("provider") == provider \
                and str(value.get("to_id")) == artist_ext_id

        now = self.clock()
        own = self.store.own_claims(album)
        # The ids the user's own claims assert FOR THIS ALBUM, by
        # provider: own id claims, plus the from-end of every own
        # link claim (a link's from_id is the album's own id on
        # that provider by construction). A link's TO-end is not
        # counted: it names the other entity (or, for same_entity,
        # an id on a provider the value does not name).
        own_ids: dict[str, set[str]] = {}
        for oclaim in own:
            for ref, via, _ref_kind, _subj_kind in \
                    provider_refs(oclaim):
                if via == "link_to":
                    continue
                prov, _, ext = ref.partition(":")
                own_ids.setdefault(prov, set()).add(ext)
        own_targets = {
            str(oclaim["value"].get("to_id"))
            for oclaim in own
            if oclaim["field"] == "provider_link"
            and isinstance(oclaim["value"], dict)
            and oclaim["value"].get("relation") == "album_of"
            and oclaim["value"].get("provider") == provider}

        def marks(value: dict[str, Any]) -> dict[str, Any]:
            return {
                "own_id_match": str(value.get("from_id"))
                in own_ids.get(provider, set()),
                # Own album_of data pointing at any OTHER artist
                # on this provider contradicts this link — the
                # user's own data links the album elsewhere.
                "own_contradiction": bool(
                    own_targets - {str(value.get("to_id"))}),
            }

        verdicts = self.store.verdicts_for_subjects([album])
        out: list[dict[str, Any]] = []
        for claim in own:
            if claim["field"] == "provider_link" \
                    and is_link(claim["value"]):
                out.append({"subject": album, "origin": None,
                            "verdict": None,
                            **marks(claim["value"])})
        for row in self.store.cached_claims([album]):
            if row["field"] != "provider_link" \
                    or not is_link(row["value"]):
                continue
            verdict = verdicts.get(
                (row["origin_node"], album, "provider_link",
                 value_key(row["value"])))
            assembled = {"subject": album,
                         "origin": row["origin_node"],
                         "source": row["source"],
                         "asserted_at": row["asserted_at"],
                         "verdict": verdict,
                         **marks(row["value"])}
            if meta_merge._row_expired(assembled, now):
                continue
            out.append(assembled)
        return out

    def _artist_disambiguation(self, record, rows):
        """§8's settlement for one artist record (R2-SF-7): the
        candidates are this artist's claimed provider ids; the
        evidence is the album_of link rows on the albums those
        ids resolve to through the provider index. Returns
        (block | None, closure): the block — the ladder's outcome
        plus settled_at, the moment the settling evidence was
        verified — rides the record whenever any candidate has
        evidence; the closure is the set of album subjects
        consulted, which the exact-ask path counts as relied-on
        for §7 scheduling (R2-SF-4)."""
        now = self.clock()
        provider_ids: set[tuple[str, str]] = set()
        for row in rows:
            # MF-1: expired rows are dead — a retracted id is not
            # a settlement candidate.
            if meta_merge._row_expired(row, now):
                continue
            provider = _ARTIST_ID_PROVIDERS.get(row["field"])
            if provider and isinstance(row["value"], str):
                provider_ids.add((provider, row["value"]))
        candidates: dict[str, list[dict[str, Any]]] = {}
        closure: set[str] = set()
        for provider, ext_id in sorted(provider_ids):
            key = f"{provider}:{ext_id}"
            link_rows: list[dict[str, Any]] = []
            for album in self.store.resolve_provider_ref(key):
                if album == record["subject"]:
                    continue
                got = self._album_of_rows(album, provider, ext_id)
                if got:
                    closure.add(album)
                    link_rows.extend(got)
            if link_rows:
                candidates[key] = link_rows
        if not candidates:
            return None, closure
        mbid_settled = None
        settled_at = None
        mbid_values = record.get("fields", {}).get(
            "artist_mbid", {}).get("values", [])
        strong_mbids = [e for e in mbid_values
                        if e["tier"] == meta_merge.TIER_STRONG]
        # MF-2: "shared" means shared with the USER'S OWN
        # non-network data. A strong-verified MBID that only the
        # network asserts settles nothing — the crowd agreeing
        # with itself is not the user's library agreeing.
        local_mbids = {
            row["value"] for row in rows
            if row["field"] == "artist_mbid"
            and row.get("origin") is None
            and row.get("source") != "network"}
        shared = [e for e in strong_mbids
                  if e["value"] in local_mbids]
        if len(shared) == 1:
            key = f"musicbrainz:{shared[0]['value']}"
            if key in candidates:
                mbid_settled = key
                settled_at = shared[0].get("verified_at")
        result = meta_merge.disambiguate_artist(
            candidates,
            own_key=self.identity.public_key_b64url,
            mbid_settled=mbid_settled)
        block = dict(result)
        if result.get("settled") is not None:
            if result["via"] == "fetched_link":
                times = [
                    (r.get("verdict") or {}).get("verified_at")
                    for r in candidates[result["settled"]]
                    if meta_merge._settles_fetched(r)]
                times = [t for t in times if t is not None]
                settled_at = max(times) if times else None
            block["settled_at"] = settled_at
        return block, closure

    # -- the relay fan-out -------------------------------------------------

    @staticmethod
    def _record_sufficient(record: dict[str, Any]) -> bool:
        """One subject's share of the early-close bar (DATA.md
        §19): the merged record is FOUND and at least one merged
        IDENTITY value (provider id, MBID, ISRC) carries support
        ≥ EARLY_CLOSE_SUPPORT. A subject with answers but no
        identity five standing origins agree on is not sufficient
        — the disagreement is exactly what the rest of the window
        is for."""
        if not record.get("found"):
            return False
        for field, entry in record.get("fields", {}).items():
            if field not in _IDENTITY_FIELDS:
                continue
            for value in entry.get("values", []):
                if value.get("support", 0) >= EARLY_CLOSE_SUPPORT:
                    return True
        return False

    @staticmethod
    def _record_sufficient_for_ref(record: dict[str, Any],
                                   ref: str) -> bool:
        """The verbatim-ref tightening of the early-close bar: the
        identity value the asker actually named — this provider's
        field carrying this exact id — must itself carry support
        ≥ EARLY_CLOSE_SUPPORT on the subject the ref resolved to.

        A provider ref is not the subject's identity by itself. It
        becomes one only through a ref→subject link in the provider
        index, and admission writes that link even for a batch with
        no standing. Letting ANY identity value on the resolved
        subject clear the general bar would let one zero-standing
        key steer the ref onto a popular subject whose unrelated
        identity is well supported, then close before the ref's
        real owner can answer. The matching value is the fact the
        user asked by, so it is the fact that must be supported."""
        normalized = _normalize_provider_ref(ref)
        if normalized is None:
            return False
        provider, _, external_id = normalized.partition(":")
        if provider == "musicbrainz":
            field = {
                "artist": "artist_mbid",
                "album": "release_group_mbid",
                "track": "recording_mbid",
            }.get(record.get("kind"))
        else:
            field = f"{provider}_id"
        if field is None:
            return False
        entry = record.get("fields", {}).get(field, {})
        return any(
            value.get("value") == external_id
            and value.get("support", 0) >= EARLY_CLOSE_SUPPORT
            for value in entry.get("values", []))

    def _ask(self, kind: str, mode: str, *, subjects=None, terms=None,
             sufficient: Callable[[], bool] | None = None,
             ) -> dict[str, Any]:
        """Stream one relay query into admission. Relay trouble is
        a shorter answer, never an exception: what already landed
        (and everything local) still merges. "attempted" is False
        when no relay client exists at all — asked_network in the
        result means a query actually went out.

        `sufficient`, when given, is the early-close check: once
        EARLY_CLOSE_SUPPORT distinct standing origins have
        contributed, at least two distinct direct answerers with
        standing have arrived (their own standing batches, not
        forwarding envelopes, and never this node's own key), and
        again at each doubling of the standing count, so the merge
        the check runs stays rare relative to the stream — it is
        asked whether the merged view of what has arrived is
        enough. If the standing bar lands before the direct
        answerers, the check is deferred until the second direct
        answerer arrives rather than being spent on a stream one
        forwarder could have supplied alone. A yes ends the
        stream ONLY when the session negotiated the close and the
        wire message was queued for delivery; on an unnegotiated
        session the stream is not even ended locally — sufficiency
        is simply unused and the ask rides the window exactly as
        it did before the message existed. Everything already
        admitted still merges through the normal post-ask path.
        The check is best-effort in both directions: a checker
        that raises reads as "not sufficient", and a stream with
        no close() (a test double, an older client) simply runs
        to the window as before."""
        tally: dict[str, Any] = {"admitted": 0, "dropped": 0,
                                 "attempted": False}
        client = None
        if self.relay_provider is not None:
            try:
                client = self.relay_provider()
            except Exception:  # noqa: BLE001 - no client, local only
                client = None
        if client is None:
            return tally
        tally["attempted"] = True
        try:
            stream = client.query(kind, mode, subjects=subjects,
                                  terms=terms)
            own_key = self.identity.public_key_b64url
            standing_origins: set[str] = set()
            direct_answerers: set[str] = set()
            next_check = EARLY_CLOSE_SUPPORT
            check_deferred = False
            for envelope in stream:
                out = self.admit(envelope)
                tally["admitted" if out.admitted else "dropped"] += 1
                if sufficient is None or not out.admitted:
                    continue
                if out.standing and out.origin_node is not None:
                    standing_origins.add(out.origin_node)
                if out.standing and not out.forwarded \
                        and out.origin_node is not None \
                        and out.origin_node != own_key:
                    # A DIRECT ANSWERER for the 2-answerer rule
                    # (gate fold 2): its own batch, in this stream,
                    # carrying STANDING (SF-2 — two fresh
                    # zero-standing keys answering directly must
                    # not clear a rule meant to keep one cheap
                    # source from ending the window), and never the
                    # asker's own key (fix (c) — this node is not
                    # another node answering its own ask).
                    direct_answerers.add(out.origin_node)
                if len(standing_origins) < next_check:
                    continue
                if len(direct_answerers) < 2:
                    # The support bar is in, but everything that
                    # supplied it may have arrived through one
                    # forwarder replaying cached origin batches.
                    # Forwarded copies count toward support (they
                    # are the origins' signed claims), but they do
                    # not by themselves license ending the window:
                    # defer this milestone until a second node has
                    # answered with its own standing batch.
                    check_deferred = True
                    continue
                if not check_deferred \
                        and len(standing_origins) > next_check:
                    continue
                # Advance to the next doubling above the CURRENT
                # standing count. A deferred check can run above
                # its original milestone (more origins landed while
                # waiting for direct answerers), so "double the
                # threshold" is computed from where the stream is.
                milestone = EARLY_CLOSE_SUPPORT
                while milestone <= len(standing_origins):
                    milestone *= 2
                next_check = milestone
                check_deferred = False
                try:
                    enough = sufficient()
                except Exception:  # noqa: BLE001 - best-effort
                    enough = False
                if not enough:
                    continue
                early_close = getattr(stream, "close_early", None)
                if callable(early_close):
                    # The negotiated path — the only one the real
                    # client takes: the stream ends ONLY when the
                    # wire close was queued for delivery.
                    # Unnegotiated, sufficiency is simply unused
                    # and the loop keeps consuming to the window;
                    # ending the stream locally anyway would leave
                    # the relay forwarding (and charging answerers
                    # for) answers nobody reads (gate fold 2, SF-1).
                    try:
                        queued = bool(early_close())
                    except Exception:  # noqa: BLE001 - best-effort
                        queued = False
                    if queued:
                        break
                    continue
                closer = getattr(stream, "close", None)
                if not callable(closer):
                    # Nothing to signal with: the stream runs to
                    # the window exactly as it always did.
                    continue
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110 - best-effort
                    pass
                break
        except (RelayNotConnected, RelayError):
            pass
        if tally["admitted"]:
            self.store.prune_answer_cache(MAX_CACHED_BATCHES)
        return tally

    # -- verification scheduling -------------------------------------------

    def _schedule_verification(self, subjects, *,
                               force: bool = False) -> None:
        subjects = sorted(subjects)
        if self.verifier is None or not subjects:
            return
        if self.verify_inline:
            self._verify_quietly(subjects, force=force)
            return
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="meta-verify")
            executor = self._executor
        executor.submit(self._verify_quietly, subjects, force=force)

    def _schedule_closure(self, closures) -> None:
        """R2-SF-4's disambiguation closure: the album subjects an
        artist ask's settlement consulted are relied-on subjects
        too — their unverdicted claims schedule a first check and
        their stale strong verdicts a forced re-check, on the
        same terms as the asked subjects themselves."""
        subjects: set[str] = set()
        for albums in closures.values():
            subjects.update(albums)
        if not subjects:
            return
        _, _, c_verify, c_recheck, _, _ = self._assemble(
            sorted(subjects))
        self._schedule_verification(c_verify)
        self._schedule_verification(c_recheck, force=True)

    def _verify_quietly(self, subjects: list[str], *,
                        force: bool = False) -> None:
        try:
            self.verifier.verify_subjects(subjects, force=force)
        except Exception as err:  # noqa: BLE001 - opportunistic by design
            # Verification is an upgrade path, not a gate: a failed
            # run leaves claims unverified (the honest state) and
            # the backlog visible in status, never a broken query.
            self.last_verify_error = str(err)

    def _sanity_triggers(self, records, rows_by_subject=None
                         ) -> None:
        """§8's bounded sanity rule, scoped to §8 (R2-SF-8): a
        trigger needs a field holding a prior STRONG-VERIFIED
        value and a NETWORK claim challenging it. A dispute among
        unverified claims triggers nothing (there is no verified
        truth to sanity-check against yet), and a challenge to
        the LOCAL value alone triggers nothing — the local value
        is this node's own data, not the network's to dispute;
        what fires is per challenging origin, gated by the
        verifier's history + rolling-24h bound. album_track
        variant conflicts count too (see _album_track_conflicts).
        """
        if self.verifier is None:
            return
        fired: set[str] = set()
        for record in records:
            subject = record["subject"]
            for field, entry in record.get("fields", {}).items():
                if field == "album_track":
                    origins = self._album_track_conflicts(
                        entry, (rows_by_subject or {}).get(
                            subject, []))
                else:
                    if not entry.get("disputed"):
                        continue
                    values = entry.get("values", [])
                    if not any(e["tier"] == meta_merge.TIER_STRONG
                               for e in values):
                        continue
                    established = {
                        value_key(e["value"]) for e in values
                        if e["local"]
                        or e["tier"] == meta_merge.TIER_STRONG}
                    origins = set()
                    for e in values:
                        if value_key(e["value"]) in established:
                            continue
                        origins.update(e.get("origins", []))
                for origin in sorted(origins):
                    if self.verifier.note_conflict(origin, subject,
                                                   field):
                        fired.add(subject)
        if fired:
            # §8's re-check is a re-check: forced past the fetch
            # cache (MF-2), or it could never overturn anything.
            self._schedule_verification(fired, force=True)

    def _album_track_conflicts(self, entry, rows) -> set[str]:
        """The album_track case of §8's rule: the field is
        disputed (more than one variant) and at least one variant
        holds a STRONG-verified slot; the challengers are the
        origins asserting slots in the OTHER variants (judged on
        the raw rows' variant names and verdicts — the merged
        variants do not carry origins)."""
        variants = entry.get("variants", {})
        if len(variants) < 2:
            return set()
        # MF-1: expired rows are dead — an expired origin's stale
        # variant fires no sanity trigger.
        now = self.clock()
        rows = [row for row in rows
                if not meta_merge._row_expired(row, now)]
        if not any(t.get("tier") == meta_merge.TIER_STRONG
                   for v in variants.values()
                   for t in v.get("tracks", [])):
            return set()
        strong_variants = {
            row["value"].get("variant") for row in rows
            if row["field"] == "album_track"
            and (row.get("verdict") or {}).get("tier")
            == meta_merge.TIER_STRONG}
        origins: set[str] = set()
        for row in rows:
            if row["field"] != "album_track":
                continue
            if row["value"].get("variant") in strong_variants:
                continue
            if row.get("origin"):
                origins.add(row["origin"])
        return origins

    # -- the ask API ---------------------------------------------------------

    def _resolve_entry(self, entry: str,
                       kind: str | None = None) -> list[str]:
        """Local resolution for ONE exact-ask entry, in the
        responder's deterministic order."""
        query: dict[str, Any] = {"subjects": [entry]}
        if kind is not None:
            query["kind"] = kind
        return self.responder._resolve_exact(query)

    def _resolve_entries(self, entries: list[str],
                         kind: str | None = None):
        """Per-entry local resolution: (subjects, ask_verbatim).

        A SoulID or legacy entry always lands in subjects (the
        responder's resolution translates a verified legacy alias,
        and passes an unknown id through — it is askable as
        itself). A provider ref lands in subjects only when the
        provider index resolves it locally — resolved WITHIN the
        ask's kind when one is known (gate minor: providers number
        their kinds separately, so a deezer artist id must never
        resolve an album or track subject); an unresolved ref is
        ask_verbatim — it rides to the relay as an opaque subject,
        exactly as the brief's deezer:12345 shape requires, because
        a node that cannot resolve it locally may still be answered
        by one that can."""
        subjects: list[str] = []
        ask_verbatim: list[str] = []
        for entry in entries:
            got = self._resolve_entry(entry, kind)
            if got:
                for subject in got:
                    if subject not in subjects:
                        subjects.append(subject)
            elif _normalize_provider_ref(entry) is not None:
                ask_verbatim.append(entry)
        return subjects, ask_verbatim

    def query_exact(self, kind: str, subjects: list[str], *,
                    network: bool = True) -> dict[str, Any]:
        """Exact ask: SoulID subjects, provider refs riding as
        subjects, legacy ids through the verified alias table.
        Cache-first; the relay is asked only for what local data
        cannot answer."""
        resolved, ask_verbatim = self._resolve_entries(subjects,
                                                       kind)
        rows_by, slots_by, needs_verify, needs_recheck, live, \
            titles = self._assemble(resolved)
        # MF-3: missing means "nothing LIVE locally" — a subject
        # whose only cached claims are expired is re-asked.
        missing = [s for s in resolved if s not in live]
        asked = False
        tally = {"admitted": 0, "dropped": 0}
        if network and (missing or ask_verbatim):
            asked_subjects = list(missing)
            verbatim_entries = list(ask_verbatim)

            def _sufficient() -> bool:
                # The bar ranges over what the query CARRIED —
                # the missing subjects plus the verbatim refs,
                # which count only once they resolve (an
                # unresolved ref means some node may still hold
                # the answer the window would have brought).
                wanted = list(asked_subjects)
                verbatim_subjects: dict[str, list[str]] = {}
                for entry in verbatim_entries:
                    resolved_for_entry = self._resolve_entry(
                        entry, kind)
                    if not resolved_for_entry:
                        return False
                    verbatim_subjects[entry] = resolved_for_entry
                    wanted.extend(
                        s for s in resolved_for_entry
                        if s not in wanted)
                rows, slots, _nv, _nr, _live, titles = \
                    self._assemble(wanted)
                records = {
                    s: self._record(s, kind, rows, slots, titles)
                    for s in wanted}
                if not all(self._record_sufficient(records[s])
                           for s in wanted):
                    return False
                # A verbatim ref additionally needs the MATCHING
                # identity value supported on every subject it
                # resolved to. The general any-identity bar above
                # cannot vouch for the ref→subject link: that link
                # may rest on one zero-standing assertion while an
                # unrelated identity on the (wrong) subject is the
                # value carrying five origins.
                return all(
                    self._record_sufficient_for_ref(records[s], entry)
                    for entry, subjects_for_entry
                    in verbatim_subjects.items()
                    for s in subjects_for_entry)

            tally = self._ask(kind, "exact",
                              subjects=missing + ask_verbatim,
                              sufficient=_sufficient)
            asked = tally["attempted"]
            if tally["admitted"]:
                # Answers may resolve entries that were verbatim a
                # moment ago (the provider index learned the ref).
                resolved, ask_verbatim = self._resolve_entries(
                    subjects, kind)
                rows_by, slots_by, needs_verify, needs_recheck, \
                    live, titles = self._assemble(resolved)
        if self.verify_inline:
            self._schedule_verification(needs_verify)
            self._schedule_verification(needs_recheck, force=True)
            rows_by, slots_by, _, _, _, titles = \
                self._assemble(resolved)
        closures: dict[str, set[str]] = {}
        records = [self._record(s, kind, rows_by, slots_by, titles,
                                closures)
                   for s in resolved]
        if not self.verify_inline:
            self._schedule_verification(needs_verify)
            self._schedule_verification(needs_recheck, force=True)
        # R2-SF-4: the disambiguation closure — the album subjects
        # an artist ask's settlement relied on — schedules too.
        self._schedule_closure(closures)
        self._sanity_triggers(records, rows_by)
        return {"records": records, "asked_network": asked,
                "admitted": tally["admitted"],
                "dropped": tally["dropped"],
                "unresolved": ask_verbatim}

    def query_text(self, kind: str, terms: list[str], *,
                   network: bool = True) -> dict[str, Any]:
        """Text ask: the local name index first (own + cached
        names — the node's whole knowledge); a local resolution
        with LIVE claims satisfies the query with no relay
        traffic. An empty local result — or one whose claims are
        all expired (MF-3) — goes to the network, and its answers
        are re-resolved locally after admission."""
        resolved = self.responder._resolve_text(
            {"kind": kind, "terms": terms})
        rows_by, slots_by, _nv, _nr, live, titles = \
            self._assemble(resolved)
        asked = False
        tally = {"admitted": 0, "dropped": 0}
        # MF-3: a local resolution whose claims are ALL expired
        # does not satisfy the query — it goes to the network like
        # an empty one, and the answers re-resolve afterwards.
        if network and not any(s in live for s in resolved):
            # Early close is EXACT-only (fold, DATA.md §19): a
            # text ask never arms the sufficiency closer. Text
            # resolution is PROGRESSIVE — the terms re-resolve
            # over a set that grows as answers land — so a
            # mid-stream check can only judge the subjects
            # resolved SO FAR: the fast terms would read
            # sufficient and close the query over the obscure
            # term whose answers arrive late, silently
            # truncating its results. The exact path anchors on
            # its ask-start subject set; the text path has no
            # anchor. A text ask rides the full window, always —
            # completeness is the point of asking by name.
            tally = self._ask(kind, "text", terms=terms)
            asked = tally["attempted"]
            if tally["admitted"]:
                resolved = self.responder._resolve_text(
                    {"kind": kind, "terms": terms})
                rows_by, slots_by, _nv, _nr, live, titles = \
                    self._assemble(resolved)
        # R2-SF-4: text-resolved subjects are BYSTANDERS — a name
        # search happening to resolve a subject is not reliance on
        # its cached claims, so the text path schedules no
        # verification for them. They are verified when an exact
        # ask returns them as relied-on subjects.
        records = [self._record(s, kind, rows_by, slots_by, titles)
                   for s in resolved]
        self._sanity_triggers(records, rows_by)
        return {"records": records, "asked_network": asked,
                "admitted": tally["admitted"],
                "dropped": tally["dropped"],
                "unresolved": [] if resolved else list(terms)}

    def record(self, entry: str) -> dict[str, Any] | None:
        """One subject's merged view from local data + cache only —
        no relay traffic, ever. The entry may be a SoulID, a
        provider ref, or a legacy id with a verified alias. None
        when this node knows nothing about it; a subject whose
        claims all expired returns the expired shell (found=False).

        A PURE READ (R2-SF-4): record() schedules NO verification.
        First checks and §6 re-checks are reliance-gated to the
        ask paths — a local read is not reliance on the network's
        claims, and letting every peek trigger provider fetches
        would make the read path a fetch amplifier."""
        resolved = self.responder._resolve_exact({"subjects": [entry]})
        if not resolved:
            return None
        subject = resolved[0]
        rows_by, slots_by, _nv, _nr, _live, titles = \
            self._assemble([subject])
        if not rows_by.get(subject):
            return None
        return self._record(subject, None, rows_by, slots_by,
                            titles)

    def status(self) -> dict[str, Any]:
        """The querier's status addition: cache size (from the
        store counts), revocation lists held, verification
        backlog."""
        counts = self.store.counts()
        out: dict[str, Any] = {
            "cached_batches": counts["cached_batches"],
            "cached_claims": counts["cached_claims"],
            "legacy_aliases": counts["legacy_aliases"],
            "revocations": None,
            "verification": self.store.verification_backlog(
                self.clock(), _STALE_S),
        }
        revocations = self.store.revocations
        if revocations is not None:
            out["revocations"] = revocations.summary()
        if self.last_verify_error:
            out["last_verify_error"] = self.last_verify_error
        return out

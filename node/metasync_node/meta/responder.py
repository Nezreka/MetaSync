"""The node responder — DESIGN-METADATA.md §5/§6/§9/§10, step 4.

Answers relay queries from the local claims store (meta/store),
under the node's mode (§9):

- `leech` — never answer. The relay never routes queries to leech
  connections in the first place; the handler refuses anyway, so
  the rule holds even if a query arrives some other way.
- `own_library` — answer from the node's own library claims only.
- `full` — own library, plus re-serving from the answer cache,
  subject to the §7.1 terms caution: a cached batch is re-served
  only when every claim in it passes the re-serve rule
  (store.claim_reservable — bare ID/link claims always pass, as
  §7.1 names them what travels; held-back VALUES stay home until
  the terms check clears). Re-served batches keep the origin
  node's signature; this node wraps them in its own signed
  forwarding envelope (§6) that attests "I am passing this along"
  and adds no support.

Query shapes (§10, plus the detail-page jump-start extension):

- `exact` with `subjects`: SoulIDs, answered with the claims
  asserting exactly those subjects (plus album_track claims that
  place a requested track — their subject is the album, their
  content is the track's slot).
- `exact` with provider refs — subjects of the form
  "provider:external_id" (or an explicit `provider_ids` list):
  resolved through the provider index to subjects, then answered
  as above. Refs are matched forgivingly at the edges — surrounding
  whitespace stripped, the provider name case-insensitive (the ID
  itself stays exact: provider IDs are case-sensitive). Self-
  checking by construction: the returned claims include the very
  provider-id / provider_link claim that matched, so the asker can
  verify the mapping locally with no provider call. The relay
  treats subjects as opaque strings, so provider refs ride the
  frozen protocol unchanged.
- `text` with `terms`: token/fuzzy match over the name index
  (exact token, token prefix, or one edit away — all in the
  soulid normalizer's family), best-matching subjects first,
  bounded to MAX_TEXT_SUBJECTS.

Own answers are minted fresh per query: up to TWO signed batches
per subject — the subject's claims split by the same §7.1 rule
the store materializes (re-servable partition first, own-only
partition second), so the part of a subject that may travel is
never held back by the part that may not. Caps keep one broad
query from flooding the asker (§6/§10): at most
MAX_BATCHES_PER_ANSWER batches, in a deterministic order — exact
subject matches first (query order), then provider-resolved
subjects (query order), then text matches by score. Query INPUTS
are capped too, at resolution time: at most MAX_QUERY_SUBJECTS
subjects + provider refs are resolved (design order, the rest
dropped — never an unbounded index scan), and text queries look
at most MAX_QUERY_TERMS terms' tokens.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..relay import NODE_MODES, normalize_mode
from . import claims as meta_claims
from . import soulid as _soulid
from .store import MetaStore, claim_reservable

MAX_BATCHES_PER_ANSWER = 50
MAX_TEXT_SUBJECTS = 25
# Input caps, applied at resolution time in design order (plain
# subjects first, then provider refs; text terms as given).
MAX_QUERY_SUBJECTS = 200
MAX_QUERY_TERMS = 32

_PROVIDERS = {"spotify", "deezer", "tidal", "itunes", "discogs",
              "hydrabase", "musicbrainz"}


def _normalize_provider_ref(entry: str) -> str | None:
    """The canonical "provider:external_id" form of a query entry,
    or None when the entry is not a provider ref. Whitespace around
    the ref and around the ID is stripped and the provider name is
    lowercased; the ID itself is untouched (provider IDs are
    case-sensitive)."""
    provider, sep, external_id = entry.partition(":")
    if not sep:
        return None
    provider = provider.strip().lower()
    external_id = external_id.strip()
    if provider not in _PROVIDERS or not external_id:
        return None
    return f"{provider}:{external_id}"


def _envelope_origin(envelope: Any) -> str | None:
    """The origin node a cached envelope names, or None when its
    shape is not even an envelope — a corrupted cache row is
    skipped in the serving path, never a KeyError."""
    if not isinstance(envelope, dict):
        return None
    batch = envelope.get("batch")
    if not isinstance(batch, dict):
        return None
    header = batch.get("header")
    if not isinstance(header, dict):
        return None
    node = header.get("node")
    return node if isinstance(node, str) and node else None


def _within_one_edit(a: str, b: str) -> bool:
    """True when a and b are at most one insertion, deletion, or
    substitution apart — the typo tolerance for text queries."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # la + 1 == lb: one insertion into a.
    i = j = 0
    skipped = False
    while i < la:
        if a[i] == b[j]:
            i += 1
            j += 1
        elif not skipped:
            skipped = True
            j += 1
        else:
            return False
    return True


def forwarding_envelope(identity: Any, envelope: dict[str, Any]) -> dict:
    """Wrap a cached batch for re-serving (§6): the origin envelope,
    untouched, plus this node's signed forwarding attestation."""
    batch = envelope["batch"]
    fwd = {
        "origin_batch_id": meta_claims.batch_digest(batch),
        "origin_node": batch["header"]["node"],
        "forwarded_at": datetime.now(UTC).isoformat(),
    }
    sig = identity.sign(meta_claims.canonical_bytes(fwd))
    return {"forward": {**fwd, "sig": sig}, "envelope": envelope}


class Responder:
    """One node's query handler, over its claims store.

    `mode_provider` is re-read per query, so a mode change in
    settings takes effect on the next query without any rewiring.
    Compatible with relay.set_query_handler: takes the forwarded
    query dict, returns a list of batch dicts (signed envelopes for
    own claims, forwarding envelopes for re-served cache).
    """

    def __init__(self, store: MetaStore, identity: Any,
                 mode_provider: Callable[[], str]):
        self.store = store
        self.identity = identity
        self.mode_provider = mode_provider

    # -- query resolution ---------------------------------------------

    def _resolve_exact(self, query: dict[str, Any]) -> list[str]:
        subjects: list[str] = []
        refs: list[str] = []
        raw_subjects = query.get("subjects") or []
        if isinstance(raw_subjects, list):
            for entry in raw_subjects:
                if not isinstance(entry, str):
                    continue
                entry = entry.strip()
                if not entry:
                    continue
                ref = _normalize_provider_ref(entry)
                if ref is not None:
                    refs.append(ref)
                else:
                    subjects.append(entry)
        raw_ids = query.get("provider_ids") or []
        if isinstance(raw_ids, list):
            for entry in raw_ids:
                if not isinstance(entry, str):
                    continue
                ref = _normalize_provider_ref(entry)
                if ref is not None:
                    refs.append(ref)
        # Input cap, in design order: plain subjects take the budget
        # first, provider refs get what remains.
        ordered: list[str] = []
        for subject in subjects[:MAX_QUERY_SUBJECTS]:
            if subject not in ordered:
                ordered.append(subject)
        remaining = MAX_QUERY_SUBJECTS - len(ordered)
        for ref in refs[:remaining]:
            for subject in self.store.resolve_provider_ref(ref):
                if subject not in ordered:
                    ordered.append(subject)
        return ordered

    def _resolve_text(self, query: dict[str, Any]) -> list[str]:
        kind = query.get("kind")
        kind = kind if kind in meta_claims.KINDS else None
        terms = query.get("terms") or []
        query_tokens: set[str] = set()
        if isinstance(terms, list):
            for term in terms[:MAX_QUERY_TERMS]:
                if isinstance(term, str):
                    for word in re.findall(r"[^\W_]+", term,
                                            flags=re.UNICODE):
                        token = _soulid.normalize_for_soul_id(word)
                        if token:
                            query_tokens.add(token)
        scores: dict[str, int] = {}
        for token in sorted(query_tokens)[:MAX_QUERY_TERMS]:
            matched = self.store.subjects_with_token(token, kind)
            if len(token) >= 3:
                matched |= self.store.subjects_with_token_prefix(
                    token, kind)
            if len(token) >= 4:
                for candidate in self.store.tokens_with_prefix(
                        token[:2], kind):
                    if abs(len(candidate) - len(token)) <= 1 \
                            and _within_one_edit(token, candidate):
                        matched |= self.store.subjects_with_token(
                            candidate, kind)
            for subject in matched:
                scores[subject] = scores.get(subject, 0) + 1
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [subject for subject, _score in
                ranked[:MAX_TEXT_SUBJECTS]]

    # -- answer construction --------------------------------------------

    def _own_batches(self, subject: str,
                     extra_claims: list[dict[str, Any]],
                     ) -> list[dict[str, Any]]:
        """The subject's own answer: its claims (plus any extra —
        a track's album_track slot) split into the re-servable and
        own-only partitions, one signed batch per non-empty
        partition, re-servable first."""
        subject_claims = self.store.own_claims(subject)
        claims = subject_claims + [
            c for c in extra_claims if c not in subject_claims]
        if not claims:
            return []
        partitions = (
            [c for c in claims if claim_reservable(c)],
            [c for c in claims if not claim_reservable(c)],
        )
        out = []
        for part in partitions:
            if not part:
                continue
            batch = meta_claims.make_batch(
                self.identity.public_key_b64url, part)
            out.append(meta_claims.sign_batch(
                self.identity.signing_private_key, batch))
        return out

    def handle(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        mode = normalize_mode(self.mode_provider())
        if mode == "leech" or mode not in NODE_MODES:
            return []
        if not isinstance(query, dict):
            return []
        qmode = query.get("mode")
        if qmode == "exact":
            subjects = self._resolve_exact(query)
        elif qmode == "text":
            subjects = self._resolve_text(query)
        else:
            return []
        out: list[dict[str, Any]] = []
        seen_batches: set[str] = set()
        # album_track claims whose subject is an album the asker did
        # not name, but which place a requested TRACK: they ride the
        # track's batch, so an exact track answer carries its slot.
        # (When the album itself was requested, the claim rides the
        # album's own batch instead — never both.)
        slot_claims: dict[str, list[dict[str, Any]]] = {}
        for claim in self.store.album_track_claims_for_tracks(subjects):
            if claim["subject"] in subjects:
                continue
            track_id = claim["value"].get("track")
            if track_id in subjects:
                slot_claims.setdefault(track_id, []).append(claim)
        for subject in subjects:
            if len(out) >= MAX_BATCHES_PER_ANSWER:
                break
            for batch in self._own_batches(
                    subject, slot_claims.get(subject, [])):
                if len(out) >= MAX_BATCHES_PER_ANSWER:
                    break
                out.append(batch)
                seen_batches.add(
                    meta_claims.batch_digest(batch["batch"]))
        if mode == "full" and subjects:
            for envelope in self.store.cached_batches_for_subjects(
                    subjects, reservable_only=True):
                if len(out) >= MAX_BATCHES_PER_ANSWER:
                    break
                origin = _envelope_origin(envelope)
                if origin is None:
                    continue  # corrupted cache row: skipped
                if origin == self.identity.public_key_b64url:
                    continue  # our own batch, echoed back: we hold it
                try:
                    digest = meta_claims.batch_digest(envelope["batch"])
                except meta_claims.ClaimError:
                    continue  # unparseable claims: skipped
                if digest in seen_batches:
                    continue
                seen_batches.add(digest)
                out.append(forwarding_envelope(self.identity, envelope))
        return out

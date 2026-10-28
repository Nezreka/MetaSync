"""Admission: the §9 door every received batch walks through.

Four checks, on EVERY batch the querier receives, direct or
re-served (a forwarding envelope is judged by its INNER origin
batch — the forwarder's envelope adds no support and earns none):

(a) the batch signature verifies and every claim validates
    (claims.verify_batch — the origin really asserted these
    claims, under this key, in this shape);
(b) the embedded attestation, when one is present, names the
    batch's signer (attestation.node_key == header.node, compared
    on raw key bytes);
(c) header.asserted_at falls inside the attestation's validity
    window (issued_at..expires);
(d) the origin key is not named on ANY revocation list this node
    holds — the union over held lists, because the issuer a batch
    claims is unverified at receipt and a revoked key naming a
    made-up issuer must not escape (gate re-pass N1).

Failures of (a), (b), or (d) DROP the batch. Check (c) failing —
or no attestation at all — admits the batch at ZERO SUPPORT: its
claims display, they just never count toward the §9 buckets.

Standing is the §9 number, and it is CRYPTOGRAPHIC (MF-1): the
attestation passes (b) and (c), its signature VERIFIES against
the issuer key this node holds for the issuer the attestation
names, AND min(asserted_at, receipt) − bound_at ≥ 30 days. The
receipt cap keeps a future-dated asserted_at from manufacturing
age the window alone would otherwise allow up to 14 days of.

In v1 the node holds ONE issuer key — the configured
meta_issuer_public_key, speaking for the trusted issuer id the
caller passes (the node's own pool, learned from its own
attestation). Every case that cannot verify — an attestation
from an issuer whose key this node does not hold, no key
configured, this node holding no attestation of its own to name
the trusted issuer, a garbage signature — admits at ZERO
SUPPORT, exactly like a missing attestation. Shape, naming, and
window alone vouch for nothing: fabricating all three costs a
fresh key exactly nothing, so an earlier shape-only version of
this door let any new key mint itself a year of standing.

Admitted batches land in the answer cache through
MetaStore.put_cached_batch with the outcome recorded in the same
commit (batch_admission), so merge-time support counting reads a
decision made once, here, at the door. A re-admission may UPGRADE
that record (R1-SF-1: standing is not frozen at first sight — a
batch admitted before this node could verify it earns standing
when it arrives again and verifies); it never downgrades.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..attestation import (
    attestation_for_batch,
    validate_attestation_shape,
    verify_batch_attestation,
)
from . import claims as meta_claims
from .store import MetaStore, node_id_for_key

STANDING_MIN_AGE_S = 30 * 24 * 3600


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str
    batch_id: str | None = None
    origin_node: str | None = None
    standing: bool = False
    forwarded: bool = False


def unwrap_envelope(envelope: Any) -> dict[str, Any] | None:
    """The signed {batch, sig} envelope admission judges: the
    envelope itself for a direct answer, the inner envelope of a
    forwarding envelope for a re-served one (§6 — re-served copies
    count once, under the ORIGIN's key and age).

    Unwrap and classification share ONE predicate (gate fold 2):
    an envelope is unwrapped only when `is_forwarding_envelope`
    says it has the full signed forwarding shape. Any other
    envelope carrying a ``forward`` key returns None here, so
    admission drops it as malformed — the two must never disagree
    again, because the disagreement was load-bearing: a bare
    ``{"forward": {}, "envelope": ...}`` unwrapped to the inner
    batch while classifying as NOT forwarded, so one forwarder's
    replays counted as many nodes answering directly."""
    if not isinstance(envelope, dict):
        return None
    if "forward" in envelope:
        if not is_forwarding_envelope(envelope):
            return None
        inner = envelope.get("envelope")
        return inner if isinstance(inner, dict) else None
    return envelope


def is_forwarding_envelope(envelope: Any) -> bool:
    """Whether an envelope has the real forwarding shape: a signed
    forwarding attestation naming the origin batch and node,
    wrapping the origin envelope. This predicate is the ONLY
    definition of a forwarding envelope — unwrap, classification,
    and the client's stamp check all key off the same shape. A
    bare ``forward`` key is not a forwarding envelope; an envelope
    carrying one without the full shape is malformed, never a
    direct batch that happens to carry an unrelated value."""
    if not isinstance(envelope, dict):
        return False
    forward = envelope.get("forward")
    if not isinstance(forward, dict):
        return False
    if not isinstance(envelope.get("envelope"), dict):
        return False
    return all(
        isinstance(forward.get(field), str)
        for field in ("origin_batch_id", "origin_node",
                      "forwarded_at", "sig"))


def _drop(reason: str, origin_node: str | None = None) -> Admission:
    return Admission(admitted=False, reason=reason,
                     origin_node=origin_node)


def admit_envelope(
    store: MetaStore,
    envelope: Any,
    *,
    now: float | None = None,
    issuer_key: Any = None,
    own_issuer: str | None = None,
) -> Admission:
    """Run the §9 checks on one received envelope and, when it is
    admitted, land it in the answer cache. `issuer_key` is the
    issuer public key this node holds (the configured
    meta_issuer_public_key) and `own_issuer` the trusted issuer
    id it speaks for (this node's own pool, from its own
    attestation). Standing is granted only through a signature
    that verifies under that pair; either being None means
    nothing can verify, so every attestation admits at zero
    support — never a bypass of the four checks."""
    received = time.time() if now is None else now
    forwarded = is_forwarding_envelope(envelope)
    inner = unwrap_envelope(envelope)
    if inner is None:
        return _drop("malformed_envelope")
    batch_obj = inner.get("batch")
    if not isinstance(batch_obj, dict):
        return _drop("malformed_envelope")
    header = batch_obj.get("header")
    if not isinstance(header, dict):
        return _drop("malformed_envelope")
    origin = header.get("node")
    if not isinstance(origin, str) or not origin:
        return _drop("malformed_envelope")

    # (a) signature + digest + every claim valid.
    try:
        batch = meta_claims.verify_batch(origin, inner)
    except meta_claims.ClaimError:
        return _drop("batch_invalid", origin)

    # (d) the union revocation check. (put_cached_batch enforces it
    # again at the write; admission is the first gate so a revoked
    # batch is never even a merge candidate in flight.)
    revocations = store.revocations
    if revocations is not None:
        node_id = node_id_for_key(origin)
        if node_id is not None and revocations.is_revoked(node_id):
            return _drop("origin_revoked", origin)

    # (b) + (c): the embedded attestation, when one is present.
    standing = False
    issuer: str | None = None
    bound_at: int | None = None
    attestation = header.get("attestation")
    if attestation is not None:
        if validate_attestation_shape(attestation) is None:
            # Present but unparseable: honest minting refuses
            # unchecked parts, so a malformed attestation is a
            # failed (b), not an absent one.
            return _drop("attestation_malformed", origin)
        parsed = attestation_for_batch(attestation, origin)
        if parsed is None:
            return _drop("attestation_names_other_key", origin)
        issuer = parsed["issuer"]
        bound_at = parsed["bound_at"]
        # MF-1: standing requires the pool's signature to VERIFY
        # against the key held for the issuer the attestation
        # names. Any other attestation — foreign issuer, no key
        # held, garbage sig — is zero support, not a drop: check
        # (a) already proved the claims are the origin's own
        # assertions; it is only the vouching that is absent.
        verified = None
        if issuer_key is not None and own_issuer is not None \
                and issuer == own_issuer:
            verified = verify_batch_attestation(
                attestation, issuer_key, origin)
        asserted_at = batch["header"]["asserted_at"]
        in_window = parsed["issued_at"] <= asserted_at < parsed["expires"]
        if verified is not None and in_window:
            age = min(asserted_at, received) - parsed["bound_at"]
            standing = age >= STANDING_MIN_AGE_S

    try:
        batch_id = store.put_cached_batch(
            inner, received_at=received, standing=standing,
            attestation_issuer=issuer, attestation_bound_at=bound_at)
    except meta_claims.ClaimError:
        return _drop("store_refused", origin)
    return Admission(
        admitted=True,
        reason="admitted" if standing else "admitted_zero_support",
        batch_id=batch_id, origin_node=origin, standing=standing,
        forwarded=forwarded)

"""A synthetic population, for the questions real users cannot answer.

Fake nodes cannot manufacture novelty — every one built from a single library is
downstream of one enrichment run, so it can only hand back ids that library
already had. What they CAN do is attack. You cannot ask real users to poison the
network to see whether it holds, and by the time a real attacker tries it, the
protocol is already deployed.

Three populations:

* **honest** — nodes holding overlapping subsets of the truth, each covering
  some fraction of the providers, which is what a real spread of libraries
  looks like.
* **liars** — nodes asserting wrong ids, independently. Each invents its own
  falsehood, so they never agree with each other.
* **sybils** — one attacker running K identities that all assert the SAME wrong
  id. This is the population that matters, because MetaSync counts distinct
  node keys and keys are free to generate.

The harness reports whether the truth still ranks first, and at what size the
attack wins. That threshold is a design input for transport: if identities are
free, corroboration alone is not a defence.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .claim import Claim
from .identity import NodeIdentity
from .store import ORIGIN_LOCAL, ORIGIN_PEER, ClaimStore

PROVIDERS = ("spotify", "deezer", "itunes", "tidal", "qobuz", "musicbrainz")


@dataclass
class Population:
    honest: int = 10
    liars: int = 0
    sybils: int = 0           # identities controlled by ONE attacker
    keys: int = 500
    coverage: float = 0.6     # share of providers an honest node resolved
    presence: float = 0.5     # share of entities an honest node holds
    local: bool = False       # one honest node is THIS machine (origin=local)
    seed: int = 1


@dataclass
class SimResult:
    truth_ranked_first: int = 0
    lie_ranked_first: int = 0
    tied: int = 0
    keys_checked: int = 0
    claims_written: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def truth_rate(self) -> float:
        return self.truth_ranked_first / self.keys_checked if self.keys_checked else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "keys_checked": self.keys_checked,
            "claims_written": self.claims_written,
            "truth_ranked_first": self.truth_ranked_first,
            "lie_ranked_first": self.lie_ranked_first,
            "tied": self.tied,
            "truth_rate": round(self.truth_rate, 4),
            **self.detail,
        }


def _norm(index: int) -> Dict[str, str]:
    return {"artist": f"artist{index}", "album": f"album{index}", "title": f"title{index}"}


def _claim(index: int, ids: Dict[str, Tuple[str, ...]], day: str = "2026-08-19") -> Claim:
    return Claim(entity="track", norm=_norm(index), ids=ids, observed_on=day)


def run(population: Population, store: Optional[ClaimStore] = None,
        store_path: Optional[str] = None) -> SimResult:
    """Build the population, merge it, and see whether the truth survives."""
    rng = random.Random(population.seed)
    owned = store is None
    store = store or ClaimStore(store_path or ":memory:")
    result = SimResult()

    truth = {
        i: {p: f"true-{p}-{i}" for p in PROVIDERS}
        for i in range(population.keys)
    }

    # Honest nodes: partial libraries, partial enrichment. None of them holds
    # the whole truth, which is the point of pooling in the first place.
    for n in range(population.honest):
        identity = NodeIdentity.generate()
        # The first node may stand in for this machine, whose own observations
        # outrank any number of peers.
        origin = ORIGIN_LOCAL if (population.local and n == 0) else ORIGIN_PEER
        for index, ids in truth.items():
            if rng.random() > population.presence:
                continue
            held = {p: (v,) for p, v in ids.items() if rng.random() < population.coverage}
            if not held:
                continue
            if store.put(_claim(index, held).sign(identity), origin):
                result.claims_written += 1

    # Liars: each invents its own falsehood, so they never corroborate each other.
    for n in range(population.liars):
        identity = NodeIdentity.generate()
        for index in truth:
            held = {p: (f"lie{n}-{p}-{index}",) for p in PROVIDERS if rng.random() < 0.5}
            if held and store.put(_claim(index, held).sign(identity), ORIGIN_PEER):
                result.claims_written += 1

    # Sybils: one attacker, many identities, one agreed lie. Free identities
    # make this the cheapest attack on a system that counts distinct keys.
    for _ in range(population.sybils):
        identity = NodeIdentity.generate()
        for index in truth:
            held = {p: (f"sybil-{p}-{index}",) for p in PROVIDERS}
            if store.put(_claim(index, held).sign(identity), ORIGIN_PEER):
                result.claims_written += 1

    for index in truth:
        key = _claim(index, {}).key
        resolved = store.resolve("track", key)
        entry = resolved.get("spotify")
        if not entry:
            continue
        result.keys_checked += 1
        top = entry[0]
        expected = truth[index]["spotify"]
        if top["value"] == expected:
            result.truth_ranked_first += 1
        elif len(entry) > 1 and entry[1]["observers"] == top["observers"]:
            result.tied += 1
        else:
            result.lie_ranked_first += 1
        if result.keys_checked == 1:
            result.detail["example_ranking"] = [
                {"value": e["value"][:24], "observers": e["observers"]} for e in entry[:4]
            ]

    if owned:
        store.close()
    return result


def sybil_threshold(honest: int = 10, keys: int = 100, coverage: float = 0.6,
                    presence: float = 0.5, seed: int = 1,
                    max_sybils: int = 40) -> Dict[str, Any]:
    """Smallest number of fake identities that outranks the honest majority.

    The answer is the protocol's real security parameter: it is the number of
    keypairs an attacker must generate, and keypairs cost nothing.
    """
    for sybils in range(0, max_sybils + 1):
        result = run(Population(honest=honest, sybils=sybils, keys=keys,
                                coverage=coverage, presence=presence, seed=seed))
        if result.truth_rate < 0.5:
            return {"honest_nodes": honest, "sybils_needed": sybils,
                    "truth_rate_at_threshold": round(result.truth_rate, 3)}
    return {"honest_nodes": honest, "sybils_needed": None,
            "note": f"truth held against {max_sybils} sybils"}

# What happens when nodes lie

Measured with `metasync/simulate.py`. Synthetic nodes cannot manufacture novel
data — every one built from a single library is downstream of one enrichment run
— but they can attack, and you cannot ask real users to poison the network to
find out whether it holds.

## The result

Ten honest nodes, each holding ~50% of the entities and ~60% of the providers
(what a real spread of partial libraries looks like), against one attacker
running K identities that all assert the same wrong id:

| fake identities | truth ranked first |
|---|---|
| 0 | 100% |
| 1 | 83% |
| 2 | 62% |
| 3 | 39% |
| 6 | 1% |
| 10 | 0% |

**Three fake identities beat ten honest nodes. Fifteen beat fifty.**

Independent liars, by contrast, are harmless: each invents its own falsehood, so
they never corroborate each other and never out-count an honest majority.

## Why so few

Corroboration counts distinct node keys, and keys cost nothing to generate. That
is the ordinary sybil problem, but the ratio here is worse than 1:1 because of an
asymmetry:

* A real library is **sparse and lumpy**. It holds some entities, and for those
  it resolved some providers. Any given id is asserted by perhaps 30% of honest
  nodes.
* An attacker is **exhaustive**. It asserts every provider for every key,
  because fabricating costs nothing.

So each fake identity carries roughly three times the weight of an honest one.

## What is done about it

**Local observation outranks any peer count.** `resolve()` ranks a value this
machine observed in its own library above everything else, as a separate tier
rather than one more vote. Against 10 sybils and against 500, the outcome is
identical — the truth rate floors at exactly the share of entities the local node
holds and stays there.

```
                      0      3     10     50    500  sybils
peer data only     100%    39%     0%     0%      —
own library        100%   51.5%  30.5%  30.5%  30.5%
```

This is a real defence for the case that matters most — your own library cannot
be corrupted by strangers — and no defence at all for entities you have never
seen, which is exactly where a peer network is supposed to help.

## What is not done about it

Nothing here makes identities expensive, and until something does, peer
corroboration on entities you do not hold is a **weak signal, not evidence**.
Options, none implemented:

* **Cost per identity** — proof of work on key creation, or vouching/invites.
  Standard, effective, and unpleasant.
* **Prefer independently checkable ids.** An ISRC→MBID mapping can be verified
  against a MusicBrainz dump; a sybil cannot forge MusicBrainz. Claims that
  check out against a corpus need no corroboration at all.
* **Exhaustiveness is itself anomalous.** Real libraries are lumpy: nobody owns
  every release by every artist with every provider resolved. A node asserting
  everything about everything does not look like a library and could be
  downweighted on that shape alone.

The third is the most promising, because it exploits the same asymmetry that
makes the attack cheap.

## Reproducing

```python
from metasync.simulate import Population, run, sybil_threshold
run(Population(honest=10, sybils=3, keys=200, seed=5))
sybil_threshold(honest=50, keys=60, max_sybils=60)
```

`tests/test_adversarial.py` asserts these numbers. They are recorded as a known
weakness, not a guarantee — if they move, the protocol's security changed and
someone should notice.

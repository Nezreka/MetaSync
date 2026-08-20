# Storage, and where it stops working

Measured on a real store: 357,221 claims from one library, 2,029,559 ids.

## The numbers

| | |
|---|---|
| store file | 861 MB |
| column data (all tables) | 334 MB |
| everything else | indexes |
| node ids inside `claim_ids` alone | 87 MB |
| keys inside `claim_ids` alone | 43 MB |

`claim_ids` is 2M rows carrying a 43-character base64 node id and a 21-character
key each, and every index covering those columns stores them again.

## A prediction that was wrong

Claims used to store a full payload JSON each, and the obvious read was that
`key`, `norm` and `song_key` were being repeated per claim. Decomposing them
into a shared `entities` table was expected to cut the store by most of its
size. It cut it by **2%**.

The reason: at one observer per entity there is nothing to deduplicate. This
store has 328,193 entities and 357,221 claims — 1.09 claims per entity. The
decomposition only pays when many nodes claim the *same* entity, which is
exactly the case that does not exist yet. It is still the right shape for a
network of peers, and it was worth doing for that reason, but it fixed a problem
this store did not have.

Dropping one redundant index — `(entity, key)`, a prefix of the primary key that
SQLite already maintains as a unique index — saved another 74 MB, which is more
than the schema change did.

## Where it actually stops working

Storage is per (node, entity, key). A hundred peers each holding 300k entities
with six ids apiece is ~180M rows in `claim_ids`, or roughly **80 GB**.

Interning would help: node ids into an integer table (43 bytes → 2), keys into
`entities.rowid` (21 → 3). Across the table and its indexes that is maybe a 3×
saving — 80 GB becomes 27 GB. Better, still not viable.

**The real answer is not to store every peer's claim forever.** You do not need
a hundred signatures to know a hundred people agree. Keeping a count per
(key, provider, value) plus a bounded sample of signatures — enough to spot-check
and to prove the count was not invented — turns per-peer growth into a constant.
The signatures that must be kept whole are this node's own, since those are what
it publishes.

That is a protocol decision, not a schema tweak, and it should be made before
transport rather than after: what a node is obliged to retain determines what it
can be asked to serve.

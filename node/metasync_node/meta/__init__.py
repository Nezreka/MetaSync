"""MetaSync metadata network — shared library.

This package is the ONE implementation of the pieces DESIGN-METADATA.md
requires to exist exactly once: the SoulID v2 (kinded_v2) algorithm, the
claim/batch formats, and the merge rules. The node imports it; the
server-side verification/compiler worker imports it; nothing else in the
system re-implements any of it.

Pure stdlib + `cryptography` (already a node dependency). No Flask, no
app imports: this package must stay importable from any Python process.
"""

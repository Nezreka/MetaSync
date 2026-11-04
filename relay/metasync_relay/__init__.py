"""MetaSync metadata relay: the dumb switchboard of DESIGN-METADATA.md section 10.

The relay holds persistent connections from nodes, forwards queries to
connected nodes, and routes their answers back to the asker by query ID.
It stores nothing and touches no database: presence lives in memory
only, and access tokens are verified offline against the issuer key —
the guest-list check lives at the token exchange (section 10).
"""

PROTOCOL_VERSION = 1  # the lowest protocol version this relay speaks
PROTOCOL_VERSION_MAX = 2

__all__ = ["PROTOCOL_VERSION", "PROTOCOL_VERSION_MAX"]

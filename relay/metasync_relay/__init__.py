"""MetaSync metadata relay: the dumb switchboard of DESIGN-METADATA.md section 10.

The relay holds persistent connections from nodes, forwards queries to
connected nodes, and routes their answers back to the asker by query ID.
It stores nothing: presence lives in memory only, and its single database
touch is a read-only connect-time peek at the portal_keys guest list.
"""

PROTOCOL_VERSION = 1

__all__ = ["PROTOCOL_VERSION"]

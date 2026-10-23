"""MetaSync node: the program that runs alongside a user's SoulSync.

For SIN it pulls listening signals from the local SoulSync (API v1 only),
pushes a weekly report to each joined pool, pulls canonical playlist data
back from the pool, and materializes/updates those playlists inside the
local SoulSync. See DESIGN-NODE.md at the repo root.
"""

__version__ = "0.1.0"

# Song-level signals (idea, not built)

SIN reports albums only (release-group MBIDs). That is the right base:
it matches how SoulSync acquires music, it reaches k = 15 far faster than
songs would (interest in one album spreads over many songs), and it keeps
reports coarse. Two things it leaves out:

- **The songs in a chart are not the network's.** An album charts on SIN's
  data, but which 5 songs represent it comes from ListenBrainz's public
  popularity, so a chart plays the mainstream's favourites from the albums
  SoulSync users wanted.
- **Song-level intent is invisible.** Library v2 wishlists single songs and
  mirrored Spotify/Deezer playlists are a big share of SoulSync use; a song
  wish becomes an album signal, and `listen_album` needs 3+ tracks from one
  album, so singles-driven listening barely registers.

## The idea, in order

1. **`track_wish`**: a song wished for this week, keyed by MusicBrainz
   recording ID where SoulSync has one. Binary per (song, install, week)
   like every signal, capped like the others, and the same bulk rule.
2. **Use it to pick an album's songs.** For an album that is already
   public (charted, k reached at album level), order its candidate songs by
   how many installs wished for each, falling back to ListenBrainz where
   the network has nothing. The open question is the privacy one: an
   ordering made from counts below k reveals relative counts. Options:
   only reorder within the song set ListenBrainz already makes public, or
   require a song's own count to reach a smaller floor before it moves.
   Decide before building.
3. **A "Most Wanted songs" chart** only once songs clear k = 15 on their
   own, which needs a much bigger network (likely a hundred or more
   reporting installs).

Not before the album network has the installs to chart on its own: at a
dozen installs, song-level counts would publish nothing.

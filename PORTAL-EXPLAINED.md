# The Portal and API Keys, in Plain English

This is the simple version. The full technical design is in
[PORTAL-DESIGN.md](PORTAL-DESIGN.md). If the two ever disagree, the
technical one wins, but this one should never confuse you.

## The four pieces

1. **SoulSync** — the app people already run. For the main MetaSync
   flow it gains one MetaSync settings section; that is where the Pool
   key is entered and handed to MetaSync.
2. **MetaSync (the program)** — a small companion that runs next to a
   person's SoulSync, on their own machine, with its own little website
   on its own port (8790). This is what does the actual work: reading
   their SoulSync, sending reports, pulling playlists down.
3. **The Portal (a website)** — a sign-in website at
   **metasync.ssync.net**. Its only jobs: let people create an account,
   and give each account its own API key. That's it.
4. **The Pool (SIN's center)** — the server in the cloud (on Neon) that
   receives everyone's weekly reports, adds them up, and publishes the
   global playlists. Already built and live in an early form.

**ssync.net itself never gets a sign-in.** It is a poster, not an app —
plain web pages with nothing running behind them. The sign-in lives on
the Portal. The ssync.net/metasync page just explains things and links
over to the Portal.

## Signing in

Same as roll-call: a **"Sign in with Google"** button. You click it,
Google confirms who you are, and the Portal creates your account from
your name and email. No new password to invent or store — Google is the
ID check. (Roll-call also enables GitHub sign-in in the same setup.)

## What an API key actually is

An API key is just **a long password, but for a program instead of a
person**.

People log in with Google. Programs can't click a Google button, so
MetaSync-the-program needs its own password to prove to the Pool: "I'm
running for a real account, let me in." That password is the API key.
It looks like `sin_live_` followed by a long random scramble.

## Yes, keys are stored in a database — as fingerprints

Your instinct is right: for a key to be revocable, something has to be
written down somewhere a revoke button can reach. That somewhere is
**the MetaSync database** — the same Neon database the Pool already
uses. It gets two new lists (tables):

- **Accounts**: who signed in (name, email, when).
- **Keys**: one row per key — whose account it belongs to, the key's
  **fingerprint**, when it was created, when it was last used, and
  whether it is on or off.

The important part: **the database never stores the actual key.** It
stores a fingerprint — the key scrambled through a one-way function
(SHA-256), the same trick websites use for passwords. You cannot
unscramble a fingerprint back into the key.

So the life of a key goes like this:

1. **Create** — you click "Create key" in the Portal. The Portal makes a
   random key, shows it to you **once** with a copy button, and saves
   only its fingerprint in the database. If you lose the key, nobody —
   not you, not us — can look it up. You just make a new one.
2. **Enter it in SoulSync** — you paste the key into SoulSync's
   MetaSync settings. SoulSync hands it to the MetaSync program
   running on that machine, proving itself with MetaSync's own local
   key (explained below). You do not normally paste the Pool key
   straight into a random program or web page.
3. **Trade it for a temporary pass** — MetaSync presents the key to
   the Pool together with its own signature (explained next). The Pool
   checks the key's fingerprint, checks that this MetaSync is the one
   tied to the key, and gives MetaSync a temporary pass. Normal work
   then uses that pass, not the long-lived key.
4. **Check-in** — when the Pool accepts the pass, it notes the time on
   the key's row: "last used Tuesday." That's how the Portal can show
   you "connected."
5. **Revoke** — you click revoke in the Portal. The row's switch flips
   to off. The key cannot trade for a new pass, and any pass already
   out there stops working on its next use. Anyone who copied your key
   is locked out, and everyone else's keys are unaffected.
6. **Regenerate** — revoke the old one and create a new one in one
   click. Same as changing a password.

And if the database ever leaked: the thief gets fingerprints, which are
useless for logging in. The keys themselves were never in there.

## Can only MetaSync talk to SIN?

That is the rule: **SIN should only be accessible by SoulSync via
MetaSync.** The honest short answer is that we can get very close —
close enough to stop the realistic problems — but no program can make
an absolute promise about another person's computer.

Here is how it works in plain words:

- **Two keys, facing each other.** Locally, the two programs
  authenticate with plain API keys — the kind both already have.
  MetaSync reads your SoulSync with a SoulSync API key (`sk_...`),
  created in SoulSync's settings, exactly like the Companion
  extension and Museck do; that's what those keys are for. And
  SoulSync talks to MetaSync with **MetaSync's own local key**:
  MetaSync shows it once in its interface, you paste it into
  SoulSync's MetaSync settings, and SoulSync presents it whenever it
  calls MetaSync. If MetaSync regenerates that key, SoulSync is cut
  off until the new one is pasted. No pairing codes, no ceremony —
  each program simply holds a key the other one issued.
- **Your key stays home.** You put the long-lived Pool key into
  SoulSync. SoulSync hands it only to MetaSync, and only on a call
  carrying MetaSync's local key — so another program that happens to
  be listening on your computer cannot simply pretend to be MetaSync
  and receive it.
- **MetaSync carries a second secret.** Every MetaSync has its own
  hidden signature key, made when it first runs and never sent
  anywhere. It can use that secret to *sign* a request — proving it
  holds the secret without showing the secret — the same basic idea as
  signing a form in a way only you can.
- **Your key is tied to your MetaSync.** The first time MetaSync tries
  to connect, the Portal shows you the MetaSync waiting to be tied to
  your key. You approve it only if it is yours. After that, the key
  works only with that MetaSync's signature.
- **MetaSync trades the key for a temporary pass.** Once the Pool has
  checked both the key's fingerprint and MetaSync's signature, it
  issues a pass that lasts about a day. Reports and playlist downloads
  use the pass plus a fresh signature. They do not carry the long-lived
  key around. When the pass runs out, MetaSync quietly trades again.
- **Downloads are locked too.** Nobody can read SIN's lists or
  playlists from the Pool just by knowing an address. The public
  website and Portal can show the finished lists, but the Pool's own
  doors require the pass and signature.
- **Revoke means revoke.** If you revoke the key or untie the
  MetaSync, the pass stops working straight away. It does not get to
  live out the rest of its day.

So a copied key on its own is not enough. A script with just the key
cannot sign as the approved MetaSync. A fake program cannot borrow the
pass without also making the real MetaSync's signature.

The limit is the owner of the machine. If somebody controls the
computer, they can eventually dig secrets out of a program they run,
or alter the program itself. That is true for every app, not just this
one. The goal here is to stop scripts, shared keys, borrowed passes
and fake clients — and this design does that.

One stronger step is saved for later: SoulSync itself can add its own
signature saying, in effect, “I handed this key to this MetaSync.”
That would give the Pool direct evidence that a real SoulSync took
part. It needs a small addition inside SoulSync, so it is marked as a
later decision in the technical design rather than quietly promised
now.

## Two boxes that never touch

This is the privacy design, and it survives the keys being stored:

- **Box 1 (accounts + keys)** knows *who you are* and *that your key
  checked in*. It knows nothing about your music.
- **Box 2 (reports)** knows *what the whole network reported* — counts
  of MusicBrainz IDs — under a nickname (a "token") that changes every
  week and is made from a secret that lives only on your own machine.
  It knows nothing about accounts.

Your key is checked at the door, then set aside. The report goes into
Box 2 under the rotating nickname, with no account and no key attached.
There is no list anywhere that connects "this account" to "these
signals." Even the person running the Pool (Boulder) can only see:
this account exists, its key is active, it last checked in on Tuesday,
and the network's totals. Never whose report was whose.

## Who can do what

| Who | Can do | Cannot do |
| --- | --- | --- |
| A user | Sign in, create/revoke their own keys, see their own connection status | See or touch anyone else's keys or data |
| Boulder (admin) | See accounts exist, switch off an abusive key, see pool totals and health | See which signals came from which account |
| MetaSync (user's copy) | Send its user's report, pull playlists down | Change the shared playlists |
| MetaSync (official copy) | Publish the shared global playlists | Anything with user accounts — that's the Portal's job |

## What still has to happen before any of this is real

1. The Portal gets built (accounts, Google sign-in, key pages, and
   the screen where you approve the MetaSync tied to a key).
2. The Pool adds the key exchange: personal key + MetaSync signature
   in, temporary pass out, and locked doors for both sending reports
   and pulling playlists.
3. SoulSync gets its MetaSync settings section — where MetaSync's
   local key lives — and the handoff that sends the Pool key to
   MetaSync under that key.
4. A few clicks that only Boulder can do: point metasync.ssync.net at
   the Portal in Cloudflare, create the Portal's hosting project, and
   register the Google sign-in (same steps as roll-call).

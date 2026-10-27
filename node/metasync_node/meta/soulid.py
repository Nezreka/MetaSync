"""SoulID v2 (`kinded_v2`) — the shared, frozen implementation.

Ported byte-for-byte from SoulSync's `core/soulid_worker.py` (SoulSync
PR #1883). SoulSync is the minter of record; this copy exists so the
MetaSync compiler can recompute SoulIDs from name claims and verify
them (DESIGN-METADATA.md §6/§7), and so the node can compute SoulIDs
for local lookups without asking SoulSync.

FROZEN NETWORK CONTRACT: any change to the normalizer or the hash
input is a new SOULID_VERSION and a new cutover in SoulSync, never a
quiet edit here. The golden vectors in
`node/tests/fixtures/soulid_golden_vectors.json` are generated from
SoulSync's implementation; `tests/test_meta_soulid.py` fails if this
port and those vectors ever disagree.
"""

import hashlib
import re
import unicodedata

SOULID_VERSION = "kinded_v2"

_SEP = "\x1f"

# Fixed positional parts per entity kind. The arity is part of the scheme:
# a kind's input always has exactly this many parts.
_KIND_ARITY = {"artist": 1, "album": 2, "track": 2, "album_track": 3}

KINDS = tuple(_KIND_ARITY)


# Scripts whose combining marks are optional decoration: they come off,
# so Beyoncé and Beyonce — and שלום with and without niqqud, محمد with
# and without harakat — are the same artist. In most other scripts the
# "marks" ARE part of the letter — a kana dakuten (が vs か), a
# Devanagari vowel sign (हिन्दी mangled to हनद without it), a Thai tone
# mark — and stripping them collides different words onto one id.
_MARK_STRIPPED_SCRIPTS = (
    (0x0041, 0x024F),  # Latin, incl. Extended-A/B
    (0x1E00, 0x1EFF),  # Latin Extended Additional (Vietnamese)
    (0x0370, 0x03FF),  # Greek
    (0x1F00, 0x1FFF),  # Greek Extended
    (0x0400, 0x052F),  # Cyrillic + Supplement
    (0x0590, 0x05FF),  # Hebrew (niqqud are optional vowel points)
    (0x0600, 0x06FF),  # Arabic (harakat are optional vowel marks)
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
)


def _marks_stripped_after(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _MARK_STRIPPED_SCRIPTS)


def _filter_for_hash(s: str) -> str:
    out = []
    base = ''
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == 'Cf':
            # Invisible format chars (RLM/LRM, soft hyphen, ZWJ/ZWNJ,
            # BOM) come out unconditionally: sources disagree on them,
            # so keeping them splits one name into two ids.
            continue
        if cat in ('Mn', 'Mc', 'Me'):
            # A combining mark lives or dies with its base letter.
            if base and not _marks_stripped_after(base):
                out.append(ch)
            continue
        if cat[0] in ('L', 'N'):
            out.append(ch)
            base = ch
        else:
            base = ''
    return unicodedata.normalize('NFC', ''.join(out))


def normalize_for_soul_id(text: str) -> str:
    """Normalize a string for deterministic hashing, in any script.

    - Compatibility-decompose and casefold (fullwidth forms fold to ASCII,
      ß -> ss, ﬁ -> fi)
    - Remove parentheticals: (feat. X), (Deluxe), (Remastered), [Live] —
      unless that empties the string, in which case they stay ("(Intro)"
      is a title, not a suffix)
    - Keep letters and digits in any script. Combining marks come off
      after Latin/Greek/Cyrillic bases (accents) and Hebrew/Arabic bases
      (niqqud/harakat are optional vowel marks); after any other base
      they are kept and the result is recomposed, because there the mark
      is part of the letter (kana dakuten, Indic vowel signs, Thai
      tones). A deliberate side effect of the Arabic strip: hamza folds
      onto its carrier (أحمد == احمد), the standard Arabic search
      normalization — sources disagree on hamza spelling more than on
      anything else. Invisible format characters always come out.
      Punctuation, symbols and whitespace are dropped.
    - If nothing letter-like survives (the band "!!!", the album "( )"),
      fall back to the raw text — compatibility-folded, casefolded,
      whitespace removed — so symbol-only names still mint ONE
      deterministic id on every install instead of a placeholder that
      the export then hides forever.

    The pre-v2 normalizer deleted every character outside [a-z0-9], which
    hashed non-Latin names to '' or fragments (and made every Cyrillic
    album by one artist share a single id).

    This function is a frozen network contract: kinded_v2 ids on other
    installs are computed with exactly this code, so any change here is
    a new SOULID_VERSION and a new cutover, never a quiet edit.
    """
    if not text:
        return ''
    raw = str(text)
    s = unicodedata.normalize('NFKD', raw).casefold()
    # Remove parenthetical/bracket suffixes (NFKD folded fullwidth brackets
    # to ASCII, so one pattern covers both)
    no_parens = re.sub(r'\s*[\(\[][^)\]]*[\)\]]', '', s)
    candidate = no_parens if no_parens.strip() else s
    # Format chars come out of the candidate itself, so the symbol
    # fallback below can't resurrect them ("!!!" + RLM is still "!!!").
    candidate = ''.join(
        c for c in candidate if unicodedata.category(c) != 'Cf')
    result = _filter_for_hash(candidate)
    if result:
        return result
    # Fall back to the same candidate the filter saw (parens already
    # stripped when there was anything outside them), whitespace removed.
    return ''.join(candidate.split())


def generate_soul_id(kind: str, *parts: str) -> str:
    """Generate a kinded_v2 soul ID for one entity.

    `kind` is one of _KIND_ARITY and `parts` must match its fixed arity.
    Returns '' when any part normalizes to '' — the caller decides between
    the soul_unnamed_<pk> placeholder and leaving the column NULL, so an ID
    is never minted from a partial input.
    """
    if kind not in _KIND_ARITY:
        raise ValueError(f"unknown soul_id kind: {kind!r}")
    if len(parts) != _KIND_ARITY[kind]:
        raise ValueError(
            f"soul_id kind {kind!r} takes {_KIND_ARITY[kind]} part(s), "
            f"got {len(parts)}")
    normalized = [normalize_for_soul_id(p) for p in parts]
    if any(not p for p in normalized):
        return ''
    combined = _SEP.join([SOULID_VERSION, kind, *normalized])
    digest = hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]
    return f'soul_{digest}'

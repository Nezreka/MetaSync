"""Generate the SoulID golden vectors from SoulSync's implementation.

Usage:
    python3 gen_soulid_golden_vectors.py /path/to/soulsync/core/soulid_worker.py

The vectors pin SoulSync's `kinded_v2` behaviour (SoulSync PR #1883) so
the MetaSync port in `metasync_node/meta/soulid.py` can never drift from
the minter of record without a test failing. Regenerate ONLY against a
new SoulSync SOULID_VERSION — a vector change against the same version
means one of the two implementations changed, which is a bug by
definition (frozen network contract).

SoulSync's module imports its own logging/worker helpers; they are
stubbed here — only the pure functions at the top of the file are used.
"""

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

CASES = [
    # kind, parts — chosen to cover every normalizer rule in the docstring
    ("artist", ["Beyoncé"]),
    ("artist", ["Beyonce"]),
    ("artist", ["שָׁלוֹם"]),          # Hebrew with niqqud
    ("artist", ["שלום"]),            # Hebrew without
    ("artist", ["مُحَمَّد"]),          # Arabic with harakat
    ("artist", ["محمد"]),
    ("artist", ["أحمد"]),             # hamza on carrier
    ("artist", ["احمد"]),             # hamza folded form — same id
    ("artist", ["Кино"]),             # Cyrillic
    ("artist", ["周杰倫"]),            # CJK
    ("artist", ["がくせい"]),          # kana — dakuten kept where present
    ("artist", ["学生"]),
    ("artist", ["हिन्दी"]),           # Devanagari vowel signs kept
    ("artist", ["เก้า"]),             # Thai tone mark kept
    ("artist", ["!!!"]),              # symbol-only fallback
    ("artist", ["( )"]),              # parens-only fallback
    ("artist", ["Sigur Rós"]),
    ("artist", ["Motörhead"]),
    ("artist", ["Ａｒｃｔｉｃ Ｍｏｎｋｅｙｓ"]),  # fullwidth folds to ASCII
    ("artist", ["Straße"]),           # ß -> ss
    ("artist", ["Nirvana"]),
    ("artist", ["The Beatles"]),
    ("artist", ["Beatles"]),
    ("album", ["Radiohead", "OK Computer"]),
    ("album", ["Radiohead", "OK Computer (Deluxe Edition)"]),
    ("album", ["Beyoncé", "Renaissance"]),
    ("album", ["Кино", "Группа крови"]),
    ("album", ["!!!", "!!!"]),
    ("album", ["Various Artists", "(Intro)"]),
    ("album", ["Radiohead", ""]),    # empty part -> ''
    ("track", ["Radiohead", "Karma Police"]),
    ("track", ["Nirvana", "Lithium"]),
    ("track", ["Taylor Swift", "Love Story"]),
    ("track", ["Taylor Swift", "Love Story (Taylor's Version)"]),
    ("album_track", ["Radiohead", "OK Computer", "Airbag"]),
    ("album_track", ["Radiohead", "OK Computer", "Paranoid Android"]),
    # Added at the Claude-gate review (Oct 26, 2026): edge cases the
    # first 36 did not cover. Generated from the same SoulSync
    # implementation; every pre-existing vector must stay identical.
    ("artist", ["!!!\u200f"]),              # symbol-only + RLM (Cf in fallback)
    ("artist", ["A\u200dB"]),              # ZWJ inside a name (Cf stripped)
    ("artist", ["So\u00adft"]),             # soft hyphen (Cf stripped)
    ("artist", ["\ufeffName"]),            # leading BOM (Cf stripped)
    ("artist", ["(foo]"]),               # mismatched brackets still strip
    ("artist", ["한국"]),                 # Hangul: NFKD jamo split, NFC recompose
    ("artist", ["1\u0301"]),             # mark after a digit base (kept)
    ("artist", ["!\u0301x"]),            # mark after a symbol base (dropped)
    ("artist", ["A\x1fB"]),              # separator char inside a part (dropped)
    ("track", ["Some Artist", "Song (Live) [Remaster] (2020)"]),
    ("album", ["Radiohead", "OK Computer (Deluxe) [2CD]"]),
]


def load_soulsync(path: str):
    for name in ("utils", "utils.logging_config", "core", "core.worker_utils"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["utils.logging_config"].get_logger = (
        lambda name: logging.getLogger(name))
    sys.modules["core.worker_utils"].interruptible_sleep = lambda *a, **k: None
    spec = importlib.util.spec_from_file_location("soulsync_soulid_worker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    mod = load_soulsync(sys.argv[1])
    vectors = []
    for kind, parts in CASES:
        vectors.append({
            "kind": kind,
            "parts": parts,
            "normalized": [mod.normalize_for_soul_id(p) for p in parts],
            "soul_id": mod.generate_soul_id(kind, *parts),
        })
    out = {
        "soulid_version": mod.SOULID_VERSION,
        "source": "SoulSync core/soulid_worker.py, PR #1883 (commit 9b21ba21)",
        "vectors": vectors,
    }
    dest = Path(__file__).parent / "fixtures" / "soulid_golden_vectors.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    print(f"wrote {len(vectors)} vectors to {dest}")


if __name__ == "__main__":
    main()

"""MetaSync command line.

    python -m metasync sync [--full] [--entity track] [--since 2026-08-01]
    python -m metasync stats
    python -m metasync show track soul_0dd312db592d67a3
    python -m metasync lookup spotify 6VbjrBn8FgFHaFI6NxCSK5
    python -m metasync pack --out mylibrary.pack
    python -m metasync import theirlibrary.pack
    python -m metasync verify
    python -m metasync id
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .identity import NodeIdentity
from .soulsync import ENTITIES, SoulSyncError
from .store import ORIGIN_LOCAL, ClaimStore
from .sync import sync_all


def _progress(entity: str, stats) -> None:
    print(f"  {entity:7} rows={stats.rows:<7} claims={stats.claims:<7} "
          f"skipped={stats.rows - stats.claims}", end="\r", file=sys.stderr, flush=True)


def cmd_sync(args, config: Config) -> int:
    entities = [args.entity] if args.entity else list(ENTITIES)
    print(f"Syncing {', '.join(entities)} from {config.soulsync_url}", file=sys.stderr)
    try:
        result = sync_all(config, entities=entities, since=args.since,
                          resume=not args.full, on_page=None if args.quiet else _progress)
    except SoulSyncError as exc:
        print(f"\nSoulSync API error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print(" " * 70, end="\r", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0


def cmd_stats(args, config: Config) -> int:
    with ClaimStore(config.store_path) as store:
        print(json.dumps(store.stats(), indent=2))
    return 0


def cmd_show(args, config: Config) -> int:
    with ClaimStore(config.store_path) as store:
        resolved = store.resolve(args.entity, args.key, min_observers=args.min_observers)
        if not resolved:
            print(f"nothing held for {args.entity} {args.key}", file=sys.stderr)
            return 1
        print(json.dumps({"entity": args.entity, "key": args.key, "ids": resolved}, indent=2))
    return 0


def cmd_lookup(args, config: Config) -> int:
    with ClaimStore(config.store_path) as store:
        hits = store.find_by_id(args.provider, args.value)
        print(json.dumps([{"entity": e, "key": k} for e, k in hits], indent=2))
    return 0 if hits else 1


def cmd_seed(args, config: Config) -> int:
    from .seed import build
    key_path = config.data_dir / "seed-musicbrainz.key"
    identity = NodeIdentity.load_or_create(key_path)

    def progress(phase, stats):
        scanned = stats.isrc_rows_scanned if phase == "isrc" else stats.recording_rows_scanned
        print(f"  {phase}: {scanned:,} rows scanned, {stats.isrc_matched:,} isrcs matched",
              end="\r", file=sys.stderr, flush=True)

    with ClaimStore(config.store_path) as store:
        stats = build(store, args.isrc, args.recording, identity,
                      only_missing=not args.all, progress=None if args.quiet else progress)
        result = {"seed_node": identity.node_id, **stats.as_dict(), "store": store.stats()}
    print(" " * 70, end="\r", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0


def cmd_gaps(args, config: Config) -> int:
    from .gaps import measure
    from .soulsync import SoulSyncClient
    client = SoulSyncClient(config.soulsync_url, config.require_api_key())
    with ClaimStore(config.store_path) as store:
        report = measure(client, store, args.entity, limit=config.page_size,
                         min_observers=args.min_observers, max_rows=args.max_rows)
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def cmd_pack(args, config: Config) -> int:
    from .pack import write_pack
    origin = None if args.relay else ORIGIN_LOCAL
    with ClaimStore(config.store_path) as store:
        stats = write_pack(store, args.out, origin=origin, entity=args.entity)
    print(json.dumps({"pack": str(args.out), **stats.as_dict()}, indent=2))
    return 0 if stats.claims else 1


def cmd_import(args, config: Config) -> int:
    from .pack import BadPack, import_pack
    own = NodeIdentity.load_or_create(config.node_key_path).node_id
    try:
        with ClaimStore(config.store_path) as store:
            stats = import_pack(store, args.pack, own_node=own)
    except BadPack as exc:
        print(f"not a usable pack: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stats.as_dict(), indent=2))
    return 0


def cmd_verify(args, config: Config) -> int:
    with ClaimStore(config.store_path) as store:
        checked, failures = store.verify_all()
    print(f"verified {checked} claims, {len(failures)} failures")
    for failure in failures[:20]:
        print(f"  {failure}")
    return 1 if failures else 0


def cmd_id(args, config: Config) -> int:
    print(NodeIdentity.load_or_create(config.node_key_path).node_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metasync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env", help="path to the env file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="import claims from SoulSync")
    p_sync.add_argument("--full", action="store_true", help="restart the walk from the beginning")
    p_sync.add_argument("--entity", choices=ENTITIES, help="sync only this entity type")
    p_sync.add_argument("--since", default="", help="only rows updated at or after this ISO timestamp")
    p_sync.add_argument("--quiet", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("stats", help="what this node holds").set_defaults(func=cmd_stats)

    p_show = sub.add_parser("show", help="resolved id map for one key")
    p_show.add_argument("entity", choices=ENTITIES)
    p_show.add_argument("key")
    p_show.add_argument("--min-observers", type=int, default=1)
    p_show.set_defaults(func=cmd_show)

    p_lookup = sub.add_parser("lookup", help="reverse lookup by provider id")
    p_lookup.add_argument("provider")
    p_lookup.add_argument("value")
    p_lookup.set_defaults(func=cmd_lookup)

    p_seed = sub.add_parser("seed", help="build dataset claims from a MusicBrainz dump")
    p_seed.add_argument("--isrc", required=True, help="path to mbdump/isrc")
    p_seed.add_argument("--recording", required=True, help="path to mbdump/recording")
    p_seed.add_argument("--all", action="store_true",
                        help="seed every track, not only those missing a MusicBrainz id")
    p_seed.add_argument("--quiet", action="store_true")
    p_seed.set_defaults(func=cmd_seed)

    p_gaps = sub.add_parser("gaps", help="how many library rows MetaSync could fill in")
    p_gaps.add_argument("entity", choices=ENTITIES)
    p_gaps.add_argument("--min-observers", type=int, default=1)
    p_gaps.add_argument("--max-rows", type=int, help="stop after this many rows")
    p_gaps.set_defaults(func=cmd_gaps)

    p_pack = sub.add_parser("pack", help="write this node's claims to a shareable file")
    p_pack.add_argument("--out", default="metasync.pack", help="output path (default: metasync.pack)")
    p_pack.add_argument("--entity", choices=ENTITIES, help="pack only this entity type")
    p_pack.add_argument("--relay", action="store_true",
                        help="also forward claims received from peers")
    p_pack.set_defaults(func=cmd_pack)

    p_import = sub.add_parser("import", help="verify and merge a pack from another node")
    p_import.add_argument("pack")
    p_import.set_defaults(func=cmd_import)

    sub.add_parser("verify", help="re-verify every stored signature").set_defaults(func=cmd_verify)
    sub.add_parser("id", help="print this node's public key").set_defaults(func=cmd_id)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, Config.load(args.env))


if __name__ == "__main__":
    raise SystemExit(main())

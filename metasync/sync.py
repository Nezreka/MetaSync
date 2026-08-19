"""Walk SoulSync's export and turn it into signed local claims.

Resumable: the cursor is checkpointed after every page, so an interrupted sync
picks up where it stopped instead of re-walking a 50k-track library.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .identity import NodeIdentity
from .importer import ImportStats, claims_from_rows
from .soulsync import ENTITIES, SoulSyncClient
from .store import ORIGIN_LOCAL, ClaimStore


def sync_entity(client: SoulSyncClient, store: ClaimStore, identity: NodeIdentity,
                entity: str, page_size: int = 500, since: str = "",
                resume: bool = True,
                on_page: Optional[Callable[[str, ImportStats], None]] = None) -> ImportStats:
    """Import one entity type. Returns the running stats for the walk."""
    stats = ImportStats()
    cursor = store.get_cursor(entity) if resume else ""

    for page in client.export(entity, cursor=cursor, since=since, limit=page_size):
        claims, stats = claims_from_rows(page["rows"], entity, stats)
        # Signed here, not in the importer: signing is an assertion of
        # authorship, and only claims that survived validation get one.
        store.put_many((claim.sign(identity) for claim in claims), origin=ORIGIN_LOCAL)
        if page["cursor"]:
            store.set_cursor(entity, page["cursor"])
        if on_page:
            on_page(entity, stats)

    return stats


def sync_all(config: Config, entities: Optional[List[str]] = None, since: str = "",
             resume: bool = True,
             on_page: Optional[Callable[[str, ImportStats], None]] = None) -> Dict[str, Any]:
    client = SoulSyncClient(config.soulsync_url, config.require_api_key())
    identity = NodeIdentity.load_or_create(config.node_key_path)

    results: Dict[str, Any] = {"node": identity.node_id, "entities": {}}
    with ClaimStore(config.store_path) as store:
        if not resume:
            store.reset_cursor()
        for entity in (entities or list(ENTITIES)):
            stats = sync_entity(client, store, identity, entity,
                                page_size=config.page_size, since=since,
                                resume=resume, on_page=on_page)
            results["entities"][entity] = stats.as_dict()
        results["store"] = store.stats()
    return results

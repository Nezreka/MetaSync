"""Pytest path setup for the node suite.

The node tests historically insert the node dir themselves, per file
(the suite also runs under unittest discovery, which never reads this
file). The relay client tests additionally import the real relay
package from the sibling relay/ tree, so both paths live here for
pytest runs; test_relay_client.py repeats the inserts itself for
unittest discovery.
"""
import sys
from pathlib import Path

_NODE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _NODE_DIR.parent
for _path in (str(_NODE_DIR), str(_REPO_ROOT / "relay")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

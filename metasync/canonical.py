"""MetaSync Canonical JSON (MCJ/1) — the byte-stable encoding claims are signed over.

A signature is only meaningful if every implementation in every language derives
the same bytes from the same claim. MCJ/1 is deliberately narrower than JSON:

  * object keys sorted by UTF-16 code unit (Python's default ``sort_keys``)
  * no insignificant whitespace — separators are ``,`` and ``:``
  * non-ASCII escaped as ``\\uXXXX``, so output is pure ASCII and no UTF-8
    normalization question can arise
  * **floats are rejected outright**. ``1.0`` vs ``1`` vs ``1.0000000000000002``
    is the classic cross-language canonicalization failure; MetaSync carries no
    fractional quantities, so the encoder refuses them rather than specifying a
    float format nobody implements identically.
  * ``null`` is rejected — omit the key instead, so "absent" has one encoding
"""

from __future__ import annotations

import json
from typing import Any


class NotCanonical(ValueError):
    """Raised when a value cannot be encoded unambiguously."""


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        raise NotCanonical(
            f"{path}: floats are not encodable in MCJ/1 (got {value!r}); "
            "use an integer, or a string if precision matters"
        )
    if value is None:
        raise NotCanonical(f"{path}: null is not encodable in MCJ/1 — omit the key instead")
    if isinstance(value, str):
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotCanonical(f"{path}: object keys must be strings, got {type(key).__name__}")
            _check(item, f"{path}.{key}")
        return
    raise NotCanonical(f"{path}: {type(value).__name__} is not encodable in MCJ/1")


def encode(value: Any) -> bytes:
    """Canonical bytes for ``value``. Raises NotCanonical on ambiguous input."""
    _check(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        check_circular=True,
    ).encode("ascii")

"""Merge the layers of a card document.

A document is assembled from layers, in this order:

1. the **archetype** (a fragment that also declares defaults and policy);
2. the **fragments** it uses (``use:``), in order;
3. the card's own keys.

The merge rules are the plan's:

* maps merge deeply;
* scalars: the last layer wins;
* lists replace, unless the layer asks to append (``$append: true``);
* ``null`` removes an inherited key.

A cycle (an archetype that uses itself, a fragment that uses another one in a
circle) raises :class:`DocumentError` with the ``card_cycle`` code: a broken
package never hangs a send.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

#: The key that asks a list to be appended instead of replaced.
APPEND_KEY = "$append"

#: The key that carries the items of an appended list.
ITEMS_KEY = "items"

#: The keys that steer the resolution and are never merged as content.
DIRECTIVE_KEYS = ("archetype", "use")

#: Slots that hold the same thing: the last one wins, the others are dropped.
#:
#: The body of a card is either one paragraph (``description``) or several
#: (``paragraphs``): a theme that writes one of them replaces the other, so
#: a delta never leaves two bodies fighting over the card.
EXCLUSIVE_SLOTS = (("description", "paragraphs"),)


class DocumentError(Exception):
    """A document cannot be resolved (a stable code, for the lint)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` over ``base`` and return a new document.

    The inputs are never modified: the result is a fresh document.
    """
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if value is None:
            # ``null`` removes the inherited key.
            out.pop(key, None)
            continue
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = merge(current, value)
            continue
        appended = _appended_items(value)
        if appended is not None:
            base_items = list(current) if isinstance(current, list) else []
            out[key] = base_items + appended
            continue
        out[key] = copy.deepcopy(value)
    _drop_exclusive(out, overlay)
    return out


def _drop_exclusive(out: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    """Keep one slot per exclusive group: the one the overlay declares."""
    for group in EXCLUSIVE_SLOTS:
        declared = [key for key in group if key in overlay]
        if not declared:
            continue
        for key in group:
            if key not in declared:
                out.pop(key, None)


def _appended_items(value: Any) -> list[Any] | None:
    """The items of an appending list (``{"$append": true, "items": [...]}``)."""
    if not isinstance(value, Mapping) or not value.get(APPEND_KEY):
        return None
    items = value.get(ITEMS_KEY)
    if not isinstance(items, list):
        return []
    return copy.deepcopy(items)


def resolve_layers(
    doc: Mapping[str, Any],
    archetypes: Mapping[str, Mapping[str, Any]],
    fragments: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The document with its archetype and fragments merged in.

    The archetype comes first, then the fragments it uses, then the card's
    own keys: the last layer wins. A fragment may use an archetype and other
    fragments too: the resolution is recursive.
    """
    return _resolve(doc, archetypes, fragments, ())


def _resolve(
    doc: Mapping[str, Any],
    archetypes: Mapping[str, Mapping[str, Any]],
    fragments: Mapping[str, Mapping[str, Any]],
    stack: tuple[str, ...],
) -> dict[str, Any]:
    """One layer resolved: its archetype, its fragments, then its own keys."""
    layers: list[Mapping[str, Any]] = []
    archetype_name = doc.get("archetype")
    if isinstance(archetype_name, str) and archetype_name.strip():
        name = archetype_name.strip()
        archetype = archetypes.get(name)
        if archetype is None:
            raise DocumentError("card_unknown_archetype", name)
        layers.append(_resolve(archetype, archetypes, fragments, (*stack, name)))
    uses = doc.get("use")
    if uses is not None:
        if not isinstance(uses, list):
            raise DocumentError("card_bad_use")
        for entry in uses:
            if not isinstance(entry, str) or not entry.strip():
                raise DocumentError("card_bad_use")
            name = entry.strip()
            fragment = fragments.get(name)
            if fragment is None:
                raise DocumentError("card_unknown_fragment", name)
            layers.append(_resolve(fragment, archetypes, fragments, (*stack, name)))
    own = {key: value for key, value in doc.items() if key not in DIRECTIVE_KEYS}
    layers.append(own)
    out: dict[str, Any] = {}
    for layer in layers:
        out = merge(out, layer)
    return out


__all__ = [
    "APPEND_KEY",
    "EXCLUSIVE_SLOTS",
    "ITEMS_KEY",
    "DocumentError",
    "merge",
    "resolve_layers",
]

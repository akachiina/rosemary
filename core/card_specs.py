"""The registry of every customizable card.

The default package is the source of truth: each document declares the shape,
the category, the mention policy and the variable contract of its card (see
:mod:`rosemary.core.theme_package`). This module only projects that metadata
into the registry (:mod:`rosemary.core.cards`), so the editor, the lint and
the send sites read one inventory.

A card is customized per guild through a theme's ``cards:`` section (see
:mod:`rosemary.core.themes`); the keys map to i18n labels under
``card.<key>.title``.
"""

from __future__ import annotations

from rosemary.core.cards import CardSpec, register_cards
from rosemary.core.theme_package import default_package


def _spec(key: str, meta: dict) -> CardSpec:
    """The spec of one card, read from the metadata of its document.

    The document is the source of truth: it declares the shape, the category,
    the mention policy and the variable contract of the card.
    """
    return CardSpec(
        key=key,
        category=meta["category"],
        rich=meta["rich"],
        mention_default=meta["mention"],
        variables=meta["variables"],
        form=meta["form"],
        style=meta["style"],
    )


def _build() -> list[CardSpec]:
    """One spec per document of the default package."""
    package = default_package()
    return [_spec(key, package.metadata(key)) for key in package.card_keys()]


register_cards(*_build())

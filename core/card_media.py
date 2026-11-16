"""Resolve a theme's media files inside a card (Components V2 / embed).

A card may reference a file the theme ships under ``assets/media/`` with the
``{media/<name>}`` placeholder, in any URL slot (the image, the thumbnail,
the file, the author icon, a gallery url, a button url). This pass runs
**before** the card is compiled, so the ``attachment://<filename>`` URL
survives the compiler's URL check, and it returns the ``discord.File`` list
the send site must attach to the message.

Discord shows an attachment referenced by a component/embed only when the
file rides the same message, so the placeholder and the file are two halves
of the same promise: resolve one, attach the other.
"""

from __future__ import annotations

import io
import re
from dataclasses import replace
from pathlib import Path

import discord

from rosemary.core.design.ir import (
    Action,
    Author,
    Card,
    Container,
    Gallery,
    Row,
    Section,
    Thumbnail,
)
from rosemary.core.theme_assets import MEDIA_REF_RE, ThemeAssets


class _Media:
    """Collects the files a card references while it resolves the URLs."""

    def __init__(self, assets: ThemeAssets) -> None:
        self._assets = assets
        self.files: list[discord.File] = []
        self._names: set[str] = set()
        #: resolved path -> filename: the same file referenced twice (a logo
        #: in the thumbnail and the author icon) rides the message once.
        self._by_path: dict[Path, str] = {}

    def url(self, value: str) -> str:
        """Replace every ``{media/<name>}`` in a URL slot, collecting files."""
        if not value or "{media/" not in value:
            return value

        def replace(match: re.Match[str]) -> str:
            return self._file_url(match.group(1))

        return MEDIA_REF_RE.sub(replace, value)

    def _file_url(self, key: str) -> str:
        path = self._assets.media_path(key)
        if path is None:
            return ""
        resolved = path.resolve()
        known = self._by_path.get(resolved)
        if known is not None:
            return f"attachment://{known}"
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        filename = self._unique(path)
        self._by_path[resolved] = filename
        self.files.append(discord.File(io.BytesIO(data), filename=filename))
        return f"attachment://{filename}"

    def _unique(self, path: Path) -> str:
        """A filename no other media file of this message took."""
        base = path.name or "media"
        if base not in self._names:
            self._names.add(base)
            return base
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, dot, ext = base, "", ""
        index = 2
        while f"{stem}_{index}{dot}{ext}" in self._names:
            index += 1
        name = f"{stem}_{index}{dot}{ext}"
        self._names.add(name)
        return name


def resolve_card_media(card: Card, assets: ThemeAssets) -> tuple[Card, list[discord.File]]:
    """Resolve ``{media/...}`` in ``card``'s URL slots; return the card + files."""
    media = _Media(assets)
    return _card(card, media), media.files


def _card(card: Card, media: _Media) -> Card:
    return replace(
        card,
        url=media.url(card.url),
        image=media.url(card.image),
        thumbnail=media.url(card.thumbnail),
        file=media.url(card.file),
        footer_icon=media.url(card.footer_icon),
        author=_author(card.author, media),
        actions=tuple(_action(action, media) for action in card.actions),
        blocks=tuple(_block(block, media) for block in card.blocks),
        embeds=tuple(_card(embed, media) for embed in card.embeds),
    )


def _author(author: Author | None, media: _Media) -> Author | None:
    if author is None:
        return None
    return replace(author, icon_url=media.url(author.icon_url))


def _action(action: Action, media: _Media) -> Action:
    return replace(action, url=media.url(action.url))


def _block(block, media: _Media):
    if isinstance(block, Gallery):
        return replace(block, urls=tuple(media.url(url) for url in block.urls))
    if isinstance(block, Row):
        return replace(
            block, buttons=tuple(_action(button, media) for button in block.buttons)
        )
    if isinstance(block, Section):
        children = tuple(_block(child, media) for child in block.children)
        accessory = block.accessory
        if isinstance(accessory, Thumbnail):
            accessory = replace(accessory, url=media.url(accessory.url))
        elif isinstance(accessory, Action):
            accessory = _action(accessory, media)
        return replace(block, children=children, accessory=accessory)
    if isinstance(block, Container):
        return replace(
            block, children=tuple(_block(child, media) for child in block.children)
        )
    return block


__all__ = ["resolve_card_media"]

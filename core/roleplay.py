"""Roleplay persistence: per-guild gif overrides.

``data/<guild_id>/roleplay.json`` holds ``gifs``: per-action lists that
REPLACE the theme's built-in list once seeded. Each entry is either
``{"url": ...}`` (a fixed gif) or ``{"api": <category>}`` (fetch a random
gif from nekos.best at send time), so a guild can mix static links with
the live source. The manager UI seeds them from the theme on first edit
(the color-manager pattern).

Gif resolution per action, in order:

1. the guild's override list (random pick inside it);
2. the theme's ``roleplay.gifs.<action>`` list (same entry shapes);
3. the built-in default category for the action via nekos.best.

A resolution returns ``None`` only when every layer fails (API offline
and no static url anywhere): callers answer with the no-gif notice.
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path
from typing import Any

import aiohttp

from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

_ROLEPLAY_FILE = "roleplay.json"

#: nekos.best exposes one gif category per interaction (``attack`` is the
#: only launch action without one; ``kick`` is the closest feel).
NEKOS_API = "https://nekos.best/api/v2/{category}"

#: The launch actions (order = command list order) and the nekos.best
#: category each falls back to when nothing is configured.
ACTIONS: tuple[str, ...] = (
    "hug",
    "kiss",
    "pat",
    "slap",
    "bite",
    "punch",
    "attack",
    "poke",
    "dance",
    "highfive",
    "stare",
    "cuddle",
)

#: action -> nekos.best category (same name except ``attack``).
DEFAULT_CATEGORIES: dict[str, str] = {action: action for action in ACTIONS}
DEFAULT_CATEGORIES["attack"] = "kick"

#: Regive contexts kept per guild (oldest dropped): the dispatcher table is
#: a convenience, not an archive.
REGIVE_HISTORY = 500


class RoleplayStore:
    """Per-guild gif overrides and regive contexts in ``roleplay.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(
            Path(data_dir), filename=_ROLEPLAY_FILE, use_defaults=False
        )

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        return await self.storage.get(guild_id)

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    async def get_gifs(self, guild_id: int, action: str) -> list[dict[str, Any]]:
        """The guild's gif list for an action (empty when not seeded)."""
        doc = await self._doc(guild_id)
        gifs = doc.get("gifs")
        entries = gifs.get(action) if isinstance(gifs, dict) else None
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    async def set_gifs(
        self, guild_id: int, action: str, gifs: list[dict[str, Any]]
    ) -> None:
        doc = await self._doc(guild_id)
        doc.setdefault("gifs", {})[action] = gifs
        await self._save(guild_id, doc)

    async def all_overrides(self, guild_id: int) -> dict[str, list[dict[str, Any]]]:
        doc = await self._doc(guild_id)
        gifs = doc.get("gifs")
        return dict(gifs) if isinstance(gifs, dict) else {}

    #: regive contexts (one dispatcher view, keyed by message id) -------------

    async def set_regive(
        self, guild_id: int, message_id: int, action: str, actor_id: int, target_id: int
    ) -> None:
        """Remember who may re-give the card posted as ``message_id``.

        The dispatcher button's custom_id is fixed, so the pair travels in
        this table (surviving restarts) instead of the custom_id; the oldest
        entries fall off beyond :data:`REGIVE_HISTORY` to bound the file.
        """
        doc = await self._doc(guild_id)
        table = doc.setdefault("regives", {})
        table[str(message_id)] = {
            "action": action,
            "actor": actor_id,
            "target": target_id,
        }
        while len(table) > REGIVE_HISTORY:
            table.pop(next(iter(table)))
        await self._save(guild_id, doc)

    async def get_regive(
        self, guild_id: int, message_id: int
    ) -> dict[str, Any] | None:
        doc = await self._doc(guild_id)
        table = doc.get("regives")
        entry = table.get(str(message_id)) if isinstance(table, dict) else None
        return dict(entry) if isinstance(entry, dict) and entry.get("action") else None


def pick_gif(gifs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One usable entry out of a configured list (random, url wins over api)."""
    usable = [g for g in gifs if isinstance(g, dict) and (g.get("url") or g.get("api"))]
    return random.choice(usable) if usable else None


async def fetch_nekos_gif(
    category: str, timeout: float = 8.0
) -> str | None:
    """A random gif url from nekos.best, or ``None`` on any failure.

    Failures are expected (the API is a best-effort default, not a
    dependency): logged at info, never raised, so a send degrades to the
    no-gif notice instead of crashing the command.
    """
    url = NEKOS_API.format(category=category)
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp,
        ):
                if resp.status != 200:
                    log.info("nekos.best returned %s for %s", resp.status, category)
                    return None
                data = await resp.json(content_type=None)
    except Exception as exc:
        log.info("nekos.best fetch failed for %s: %s", category, exc)
        return None
    results = data.get("results") if isinstance(data, dict) else None
    for entry in results or []:
        found = (entry or {}).get("url") if isinstance(entry, dict) else None
        if found:
            return str(found)
    return None


async def resolve_gif(
    gifs: list[dict[str, Any]], fallback_category: str | None
) -> tuple[str | None, bool]:
    """Effective ``(gif_url, from_api)`` for one action.

    ``gifs`` is the guild override list (or the theme's): a random entry
    wins; a static ``url`` is used as-is, an ``api`` entry fetches a fresh
    gif. Empty list or unusable entries fall through to the built-in
    category. ``(None, True)`` is only returned when every layer fails
    (nothing configured or the API unreachable); ``from_api`` tells callers
    whether the url is ephemeral (no source link on the card).
    """
    entry = pick_gif(gifs)
    if entry is not None:
        url = str(entry.get("url") or "").strip()
        if url:
            return url, False
        category = str(entry.get("api") or "").strip()
        if category:
            fetched = await fetch_nekos_gif(category)
            if fetched:
                return fetched, True
    if fallback_category:
        fetched = await fetch_nekos_gif(fallback_category)
        if fetched:
            return fetched, True
    return None, True


def category_for(action: str) -> str | None:
    """The built-in nekos.best category of an action (``None``: unknown)."""
    return DEFAULT_CATEGORIES.get(action)


_CATEGORY_RE = re.compile(r"^[a-z0-9]+$")


def valid_api_category(value: str) -> bool:
    """Whether the manager may store ``value`` as an api category.

    Shape validation only (lowercase slug): the catalog of categories is
    the API's business, and a wrong name fails gracefully at fetch time
    (logged, falls through to the static list / default category).
    """
    return bool(_CATEGORY_RE.match(str(value or "").strip().lower()))

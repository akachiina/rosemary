"""Interactions (roleplay) persistence: genders and per-guild gif overrides.

Two documents live in ``data/<guild_id>/roleplay.json``:

* ``genders``: ``{user_id: gender_key}``. Every user is implicitly
  ``any`` (tanto faz) until they pick one with ``/genero``.
* ``gifs``: per-action gif lists that REPLACE the theme's built-in list
  once seeded. Each gif is ``{"url": ..., "actor": ..., "target": ...}``
  where ``actor``/``target`` are gender keys or ``any``. The manager UI
  seeds them from the theme on first edit, so the guild owns its own
  list afterwards (the color-manager pattern).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_ROLEPLAY_FILE = "roleplay.json"

#: Genders offered by ``/genero`` (order = display order).
GENDERS: tuple[str, ...] = ("male", "female", "nonbinary", "any", "unspecified")

#: Default while a user never picked a gender.
DEFAULT_GENDER = "any"

#: The twelve launch actions (order = command list order).
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


def normalize_gender(value: Any) -> str:
    """Coerce a stored/choice value into a known gender key."""
    text = str(value or "").strip().lower()
    return text if text in GENDERS else DEFAULT_GENDER


class RoleplayStore:
    """Per-guild genders and gif overrides in ``roleplay.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(
            Path(data_dir), filename=_ROLEPLAY_FILE, use_defaults=False
        )

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        return await self.storage.get(guild_id)

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    #: genders ---------------------------------------------------------------

    async def get_gender(self, guild_id: int, user_id: int) -> str:
        doc = await self._doc(guild_id)
        genders = doc.get("genders")
        return (
            normalize_gender(genders.get(str(user_id)))
            if isinstance(genders, dict)
            else DEFAULT_GENDER
        )

    async def has_gender(self, guild_id: int, user_id: int) -> bool:
        doc = await self._doc(guild_id)
        genders = doc.get("genders")
        return isinstance(genders, dict) and str(user_id) in genders

    async def set_gender(self, guild_id: int, user_id: int, gender: str) -> str:
        gender = normalize_gender(gender)
        doc = await self._doc(guild_id)
        genders = doc.setdefault("genders", {})
        genders[str(user_id)] = gender
        await self._save(guild_id, doc)
        return gender

    #: gifs ------------------------------------------------------------------

    async def get_gifs(self, guild_id: int, action: str) -> list[dict[str, Any]]:
        """The guild's gif list for an action (empty when not seeded)."""
        doc = await self._doc(guild_id)
        gifs = doc.get("gifs")
        entries = gifs.get(action) if isinstance(gifs, dict) else None
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict) and entry.get("url")]

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


def pick_gif(
    gifs: list[dict[str, Any]], actor_gender: str, target_gender: str
) -> dict[str, Any] | None:
    """Choose a gif matching the (actor, target) gender pair, in layers.

    Layer 1 keeps gifs whose filters match BOTH genders exactly (so a pair
    of users who both picked ``female`` can hit an ``ff`` gif); layer 2
    accepts partial matches (one side ``any``); layer 3 falls back to gifs
    with no gender filters at all. Deterministic order: the first match
    wins, so seeded lists behave predictably.
    """
    both = [
        g
        for g in gifs
        if g.get("actor", DEFAULT_GENDER) == actor_gender
        and g.get("target", DEFAULT_GENDER) == target_gender
    ]
    if both:
        return both[0]
    partial = [
        g
        for g in gifs
        if (
            g.get("actor", DEFAULT_GENDER) in (actor_gender, DEFAULT_GENDER)
            and g.get("target", DEFAULT_GENDER) in (target_gender, DEFAULT_GENDER)
        )
        and (
            g.get("actor", DEFAULT_GENDER) != DEFAULT_GENDER
            or g.get("target", DEFAULT_GENDER) != DEFAULT_GENDER
        )
    ]
    if partial:
        return partial[0]
    unfiltered = [
        g
        for g in gifs
        if g.get("actor", DEFAULT_GENDER) == DEFAULT_GENDER
        and g.get("target", DEFAULT_GENDER) == DEFAULT_GENDER
    ]
    return unfiltered[0] if unfiltered else None

"""Per-card mention policies for Rosemary.

Every customizable card (:class:`rosemary.core.cards.CardSpec`) declares a
default mention policy; guilds override it per card in /customize. All sends
resolve their ``AllowedMentions`` here so mention behavior stays in one place
instead of scattered ``channel.send`` calls.

Modes:
* ``none`` — never ping (default; logs, lists, starboard).
* ``single`` — ping at most one user (thank-you, birthday, welcome).
* ``winner_auto`` — ping the winner only on automatic posts; manual
  ``/bump_leaderboard`` commands never ping (``source="command"``).
* ``role`` — ping a role (bump reminder ``bump.ping_role``).
* ``all`` — legacy opt-in: parse everything (explicit spam choice).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import discord

from rosemary.core.storage import GuildStorage

_MENTIONS_FILE = "mentions.json"

#: Valid policies, in UI display order.
MODES: tuple[str, ...] = ("none", "single", "winner_auto", "role", "all")

#: Fallback when a card key has no registered spec (defensive: never ping).
DEFAULT_MODE = "none"


class MentionStore:
    """Per-guild storage of per-card mention policy overrides.

    Uses its own ``mentions.json`` file (via :class:`GuildStorage`), so ping
    preferences never mix with card documents or settings. A missing key means
    "not customized" — resolution falls back to the card spec default.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._storage = GuildStorage(data_dir, filename=_MENTIONS_FILE, use_defaults=False)

    async def get_policy(self, guild_id: int, key: str) -> str | None:
        """Return the guild's saved policy for ``key`` or ``None``."""
        data = await self._storage.get(guild_id)
        value = data.get(key)
        return value if isinstance(value, str) and value in MODES else None

    async def set_policy(self, guild_id: int, key: str, policy: str) -> None:
        """Persist one card's mention policy."""
        if policy not in MODES:
            raise ValueError(f"Unknown mention policy {policy!r}")
        await self._storage.set(guild_id, key, policy)

    async def reset(self, guild_id: int, key: str) -> None:
        """Remove an override, restoring the card spec default."""
        await self._storage.delete_keys(guild_id, key)


def mention_store(bot) -> MentionStore:
    """Default store bound to the bot's data directory."""
    return MentionStore(bot.storage.data_dir)


def spec_default(key: str) -> str:
    """Default policy declared by the card spec, ``"none"`` when unknown."""
    try:
        from rosemary.core.cards import get_card
    except ImportError:  # pragma: no cover - import cycle guard
        return DEFAULT_MODE
    spec = get_card(key)
    if spec is None:
        return DEFAULT_MODE
    default = getattr(spec, "mention_default", DEFAULT_MODE)
    return default if default in MODES else DEFAULT_MODE


async def effective_policy(bot, guild_id: int, key: str) -> str:
    """Guild override for ``key``, falling back to the spec default.

    A corrupted stored value (unknown mode) is repaired on read so one bad
    write can never wedge the picker or a send.
    """
    import logging

    store = mention_store(bot)
    data = await store._storage.get(guild_id)
    value = data.get(key)
    if value is None:
        return spec_default(key)
    if isinstance(value, str) and value in MODES:
        return value
    logging.getLogger(__name__).warning(
        "Repairing unknown mention policy %r for card %s in guild %s",
        value,
        key,
        guild_id,
    )
    await store.reset(guild_id, key)
    return spec_default(key)


def describe_policy(policy: str) -> str:
    """I18n key suffix for a policy (labels live under ``cards.mentions.*``)."""
    return policy if policy in MODES else DEFAULT_MODE


def valid_policy(value: Any) -> bool:
    """Whether ``value`` is a known mention policy name."""
    return isinstance(value, str) and value in MODES


# -- pings toggle ------------------------------------------------------------
#: The editor exposes one per-card question: may this card ping at all?
#: Stored values reuse the legacy vocabulary (``none`` = off, anything else
#: = on) so old ``mentions.json`` files keep working with no migration;
#: ``mention_default != "none"`` is the per-card default.


def pings_default(key: str) -> bool:
    """Whether the card spec opts into pings by default."""
    return spec_default(key) != "none"


async def effective_pings(bot, guild_id: int, key: str) -> bool:
    """Guild's pings toggle for ``key`` (override, else the spec default)."""
    store = mention_store(bot)
    value = await store.get_policy(guild_id, key)
    if value is None:
        return pings_default(key)
    return value != "none"


async def set_pings_for(bot, guild_id: int, key: str, enabled: bool) -> None:
    """Persist one card's pings toggle bound to the bot's data directory."""
    await mention_store(bot).set_policy(guild_id, key, "all" if enabled else "none")


def _parse_mention_tokens(text: str) -> tuple[list[int], list[int]]:
    """``(user_ids, role_ids)`` found in already-resolved text."""
    import re as _re

    token = _re.compile(r"<@!?([0-9]{1,20})>|<@&([0-9]{1,20})>")
    users: list[int] = []
    roles: list[int] = []
    for found in token.finditer(text):
        role_id, user_id = found.group(2), found.group(1)
        (roles if role_id else users).append(int(role_id or user_id))
    return list(dict.fromkeys(users)), list(dict.fromkeys(roles))


def _allowed_for(users: list[int], roles: list[int]) -> discord.AllowedMentions:
    if not users and not roles:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=uid) for uid in users] if users else False,
        roles=[discord.Object(id=rid) for rid in roles] if roles else False,
        replied_user=False,
    )


async def allowed_for_ids(
    bot,
    guild_id: int,
    key: str,
    *,
    user_ids: tuple[int, ...] | list[int] = (),
    role_ids: tuple[int, ...] | list[int] = (),
    silent: bool = False,
) -> discord.AllowedMentions:
    """``AllowedMentions`` for a send whose candidates are known by id.

    Pings off (toggle or ``silent``) -> ``none()``; on -> exactly the passed
    candidate ids may ping. This replaces the legacy ``mentions_for`` policy
    matrix at send sites.
    """
    if silent or not await effective_pings(bot, guild_id, key):
        return discord.AllowedMentions.none()
    users = [int(uid) for uid in user_ids if int(uid) > 0]
    roles = [int(rid) for rid in role_ids if int(rid) > 0]
    return _allowed_for(users, roles)


async def allowed_for_text(
    bot,
    guild_id: int,
    key: str,
    text: str,
    *,
    silent: bool = False,
    user_ids: tuple[int, ...] | list[int] = (),
    role_ids: tuple[int, ...] | list[int] = (),
) -> discord.AllowedMentions:
    """``AllowedMentions`` for already-resolved text (staff logs, DM bodies).

    Only the ``<@id>``/``<@&id>`` tokens actually present in ``text`` may ping,
    plus any explicitly passed candidate ids, and only when the guild's pings
    toggle for ``key`` is on. Log cards default to off, so staff logs stay
    silent unless a guild explicitly opts in.
    """
    if silent or not await effective_pings(bot, guild_id, key):
        return discord.AllowedMentions.none()
    users, roles = _parse_mention_tokens(text)
    users += [int(uid) for uid in user_ids if int(uid) > 0]
    roles += [int(rid) for rid in role_ids if int(rid) > 0]
    return _allowed_for(list(dict.fromkeys(users)), list(dict.fromkeys(roles)))


async def allowed_for_document(
    bot,
    guild_id: int,
    key: str,
    doc: dict[str, Any],
    mapping: dict[str, Any],
    *,
    silent: bool = False,
) -> discord.AllowedMentions:
    """``AllowedMentions`` for a card document rendered with ``mapping``.

    Parses the mention tokens the document resolves to (bodies and button
    labels), so customized text decides who can ping — the position of
    ``{@user}``/``{user}`` in the content is the admin's choice, not code's.
    """
    if silent or not await effective_pings(bot, guild_id, key):
        return discord.AllowedMentions.none()
    from rosemary.core.cards import document_mention_ids

    users, roles = document_mention_ids(doc, mapping)
    return _allowed_for(users, roles)

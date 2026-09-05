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


def resolve_allowed_mentions(
    policy: str,
    user_ids: tuple[int, ...] | list[int] = (),
    role_ids: tuple[int, ...] | list[int] = (),
    source: str = "auto",
) -> discord.AllowedMentions:
    """Build an ``AllowedMentions`` for ``policy``.

    ``user_ids``/``role_ids`` are the *candidates* found in the card text
    (winner, bumper, celebrant...). Only the ids permitted by the policy are
    included; everything else parses as plain text (still clickable, no ping).
    """
    if policy == "all":
        return discord.AllowedMentions.all()
    if policy == "single":
        ids = [int(uid) for uid in list(user_ids)[:1] if int(uid) > 0]
        if not ids:
            return discord.AllowedMentions.none()
        return discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(id=uid) for uid in ids],
            roles=False,
            replied_user=False,
        )
    if policy == "winner_auto":
        if source != "auto":
            return discord.AllowedMentions.none()
        ids = [int(uid) for uid in list(user_ids)[:1] if int(uid) > 0]
        if not ids:
            return discord.AllowedMentions.none()
        return discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(id=uid) for uid in ids],
            roles=False,
            replied_user=False,
        )
    if policy == "role":
        ids = [int(rid) for rid in list(role_ids) if int(rid) > 0]
        if not ids:
            return discord.AllowedMentions.none()
        return discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=rid) for rid in ids],
            replied_user=False,
        )
    return discord.AllowedMentions.none()


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
    """Guild override for ``key``, falling back to the spec default."""
    override = await mention_store(bot).get_policy(guild_id, key)
    if override is not None:
        return override
    return spec_default(key)


async def mentions_for(
    bot,
    guild_id: int,
    key: str,
    *,
    source: str = "auto",
    user_ids: tuple[int, ...] | list[int] = (),
    role_ids: tuple[int, ...] | list[int] = (),
) -> discord.AllowedMentions:
    """Resolve the effective ``AllowedMentions`` for one card send."""
    policy = await effective_policy(bot, guild_id, key)
    return resolve_allowed_mentions(policy, user_ids, role_ids, source)


async def send_log_mentions(
    bot,
    guild_id: int,
    key: str,
    *,
    user_ids: tuple[int, ...] | list[int] = (),
    role_ids: tuple[int, ...] | list[int] = (),
) -> discord.AllowedMentions:
    """Resolve mentions for a staff-log entry (never auto-pings by default).

    Log cards default to ``none``; a guild may opt a specific log card into
    ``single``/``all`` in /customize, in which case only the explicitly passed
    candidate ids may ping.
    """
    return await mentions_for(
        bot, guild_id, key, source="auto", user_ids=user_ids, role_ids=role_ids,
    )


def describe_policy(policy: str) -> str:
    """I18n key suffix for a policy (labels live under ``cards.mentions.*``)."""
    return policy if policy in MODES else DEFAULT_MODE


def valid_policy(value: Any) -> bool:
    """Whether ``value`` is a known mention policy name."""
    return isinstance(value, str) and value in MODES

"""Per-card mention policies for Rosemary.

Every customizable card (:class:`rosemary.core.cards.CardSpec`) declares a
default mention policy. Guilds opt in/out per card through the active theme
file's ``pings:`` section (``true``/``false`` per card key); without an entry
the spec default applies. All sends resolve their ``AllowedMentions`` here so
mention behavior stays in one place instead of scattered ``channel.send`` calls.

Modes:

* ``none`` -- never ping (default; logs, lists, starboard).
* ``single`` -- ping at most one user (thank-you, birthday, welcome).
* ``winner_auto`` -- ping the winner only on automatic posts; manual
  ``/bump_leaderboard`` commands never ping (``source="command"``).
* ``role`` -- ping a role (bump reminder ``bump.ping_role``).
* ``all`` -- legacy opt-in: parse everything (explicit spam choice).

The pings toggle is content-driven: only the ``<@id>``/``<@&id>`` tokens the
resolved text actually contains may ping.
"""

from __future__ import annotations

from typing import Any

import discord

#: Valid policies, in UI display order.
MODES: tuple[str, ...] = ("none", "single", "winner_auto", "role", "all")

#: Fallback when a card key has no registered spec (defensive: never ping).
DEFAULT_MODE = "none"


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


def pings_default(key: str) -> bool:
    """Whether the card spec opts into pings by default."""
    return spec_default(key) != "none"


def theme_pings(bot, guild_id: int | None, key: str) -> bool:
    """Guild's pings toggle for ``key``: theme file override, else spec default.

    Reads the guild's active theme snapshot synchronously (see
    :func:`rosemary.core.themes.theme_for`); bots without theme support (test
    fakes) fall back to the spec default. A missing key means "not overridden".
    """
    if guild_id is None:
        return pings_default(key)
    from rosemary.core.themes import theme_for

    theme = theme_for(bot, guild_id)
    overrides = getattr(theme, "pings", None)
    if isinstance(overrides, dict) and key in overrides:
        return bool(overrides[key])
    return pings_default(key)


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
    candidate ids may ping.
    """
    if silent or not theme_pings(bot, guild_id, key):
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
    silent unless a guild explicitly opts in via its theme.
    """
    if silent or not theme_pings(bot, guild_id, key):
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
    labels), so themed text decides who can ping -- the position of
    ``{@user}``/``{user}`` in the content is the admin's choice, not code's.
    """
    if silent or not theme_pings(bot, guild_id, key):
        return discord.AllowedMentions.none()
    from rosemary.core.cards import document_mention_ids

    users, roles = document_mention_ids(doc, mapping)
    return _allowed_for(users, roles)

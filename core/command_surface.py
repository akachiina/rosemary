"""The policy of a command surface: ACK, errors, ephemeral, no traceback.

Every command answers the same way: it ACKs before the slow work, it never
shows a traceback to the member, it says "no permission" when the invoker
lacks it and "the bot has no permission" when the bot does. A surface declares
that policy once, and :func:`run` applies it::

    await run(ctx, Surface(card_key="moderation.error_failed"), handler)

The notice is always the translated catalog copy, rendered by
:mod:`rosemary.core.feedback` (the theme owns the prefix) and it never pings:
the copy names the member the bot is about to ban.

``card_key`` keeps the name of the plan (a "card" was any message the bot
sent): it is the key of the copy the surface answers with when the command
fails. The family owns the wording (the moderation family answers
``moderation.error_failed``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

from rosemary.core.feedback import respond as respond_notice

log = logging.getLogger(__name__)

#: The copy of a missing permission (the invoker lacks it).
NO_PERMISSION = "general.no_permission"
#: The copy of a bot without the permission it needs.
BOT_NO_PERMISSION = "general.bot_no_permission"
#: The copy of an unexpected failure.
UNKNOWN_ERROR = "general.unknown_error"

#: The handler a surface runs: the command's own body.
Handler = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class Surface:
    """The policy of one command surface.

    ``card_key``
        The key of the copy the surface answers with when the command fails
        (the family owns the wording).
    ``ephemeral``
        Whether the answer is ephemeral. ``None`` means "the context decides":
        a slash command answers ephemerally, a prefix command replies publicly
        (a plain message cannot be ephemeral).
    ``defer``
        Whether the surface ACKs the interaction before the slow work. A
        command that answers with a panel keeps it ``False``: the panel is
        the answer.
    ``permission``
        The Discord permission the invoker needs. The surface checks it and
        answers ``general.no_permission`` when it is missing. A family with
        richer checks (the moderation one knows the role hierarchy) leaves it
        empty and answers its own copies.
    """

    card_key: str
    ephemeral: bool | None = None
    defer: bool = True
    permission: str | None = None


def guild_id_of(ctx: Any) -> int | None:
    """The guild of a context (slash, prefix or facade)."""
    guild_id = getattr(ctx, "guild_id", None)
    if guild_id is None:
        guild = getattr(ctx, "guild", None)
        guild_id = getattr(guild, "id", None)
    return guild_id


def is_application(ctx: Any) -> bool:
    """Whether the context answers an interaction (the only one with a window).

    A prefix context is not one: the facade over it says so (``is_prefix``),
    and a raw prefix context has neither ``defer`` nor ``respond``.
    """
    if getattr(ctx, "is_prefix", False):
        return False
    if isinstance(ctx, (discord.ApplicationContext, discord.Interaction)):
        return True
    return hasattr(ctx, "defer") and hasattr(ctx, "respond")


def ephemeral_for(ctx: Any, surface: Surface) -> bool:
    """Whether the answer of this surface is ephemeral."""
    if surface.ephemeral is not None:
        return surface.ephemeral
    return is_application(ctx)


def has_permission(ctx: Any, permission: str) -> bool:
    """Whether the invoker holds the permission the surface requires.

    A context without guild permissions (a DM, a fake) is not blocked here:
    the command's own checks decide.
    """
    permissions = getattr(getattr(ctx, "author", None), "guild_permissions", None)
    if permissions is None:
        return True
    return bool(getattr(permissions, permission, False))


async def notice(
    ctx: Any,
    bot: Any,
    guild_id: int | None,
    key: str,
    *,
    style: str = "info",
    icon: str | None = None,
    ephemeral: bool = True,
    **variables: Any,
) -> None:
    """One translated notice line (the theme's prefix, never a ping).

    The copy comes from the catalog (``key``); the tone picks the theme's
    prefix and ``icon`` names the verb of the action. The notice never
    notifies: the copy may name the member the bot is about to ban.
    """
    text = await bot.translator.t(guild_id, key, **variables)
    await respond_notice(
        ctx, bot, guild_id, text, style=style, icon=icon, ephemeral=ephemeral
    )


async def _safe_notice(
    ctx: Any,
    bot: Any,
    guild_id: int | None,
    key: str,
    style: str,
    ephemeral: bool,
    *,
    icon: str | None = None,
) -> None:
    """Answer the failure, but never fail while answering it."""
    try:
        await notice(
            ctx, bot, guild_id, key, style=style, icon=icon, ephemeral=ephemeral
        )
    except Exception:  # noqa: BLE001
        log.warning("Could not answer the failed command", exc_info=True)


async def run(ctx: Any, surface: Surface, handler: Handler) -> None:
    """Run a command under the surface's policy.

    The ACK happens before the handler, a missing permission stops the
    command, and every failure becomes a translated notice: the member never
    sees a traceback and the bot always answers.
    """
    bot = getattr(ctx, "bot", None)
    guild_id = guild_id_of(ctx)
    ephemeral = ephemeral_for(ctx, surface)
    try:
        if surface.permission and not has_permission(ctx, surface.permission):
            await _safe_notice(
                ctx, bot, guild_id, NO_PERMISSION, "warning", ephemeral, icon="lock"
            )
            return
        if surface.defer and is_application(ctx):
            await ctx.defer(ephemeral=ephemeral)
        await handler()
    except commands.CheckFailure:
        await _safe_notice(
            ctx, bot, guild_id, NO_PERMISSION, "warning", ephemeral, icon="lock"
        )
    except discord.Forbidden:
        log.warning(
            "The bot lacks the permission for %s (guild %s)", surface.card_key, guild_id
        )
        await _safe_notice(
            ctx, bot, guild_id, BOT_NO_PERMISSION, "danger", ephemeral, icon="lock"
        )
    except Exception:
        log.exception("Command %s failed (guild %s)", surface.card_key, guild_id)
        await _safe_notice(
            ctx, bot, guild_id, surface.card_key, "danger", ephemeral, icon="error"
        )


__all__ = [
    "BOT_NO_PERMISSION",
    "NO_PERMISSION",
    "UNKNOWN_ERROR",
    "Handler",
    "Surface",
    "ephemeral_for",
    "guild_id_of",
    "has_permission",
    "is_application",
    "notice",
    "run",
]

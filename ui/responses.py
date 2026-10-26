"""Standard ephemeral responses as a colored container.

Every user-facing ephemeral notice (errors, warnings, plain confirmations)
renders through one builder so a theme can restyle the surface and every
cog speaks the same visual language: one container in the semantic color,
the message text inside. Plain ``ctx.respond(text, ephemeral=True)`` at call
sites predating this helper is the deviation this module exists to retire.

Prefix aliases reuse the slash handlers verbatim through
:class:`PrefixContext`, the slash-shaped facade declared below.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary.ui.containers import TextDisplay, designer_container


async def notice_view(
    bot,
    guild_id: int | None,
    text: str,
    *,
    color: str = "danger",
) -> discord.ui.DesignerView:
    """One themed container carrying ``text`` (already translated).

    ``color`` is a theme color token: ``danger`` for errors, ``warning`` for
    warnings, ``success`` for confirmations, ``info`` for notices. The text
    keeps its own ``{success}``/``{error}`` emoji tokens (the catalog injected
    them already); the container color is the only thing added here.
    """
    from rosemary.core.themes import theme_for

    theme = theme_for(bot, guild_id)
    view = discord.ui.DesignerView(store=False)
    try:
        accent = theme.color(color)
    except KeyError:
        accent = theme.color("brand")
    view.add_item(designer_container(accent, TextDisplay(text)))
    return view


async def respond_notice(
    interaction_or_ctx,
    bot,
    guild_id: int | None,
    text: str,
    *,
    color: str = "danger",
) -> None:
    """Respond ephemerally with :func:`notice_view` (ACKs when needed)."""
    view = await notice_view(bot, guild_id, text, color=color)
    response = interaction_or_ctx.response
    if response is not None and not response.is_done():
        await response.send_message(view=view, ephemeral=True)
        return
    await interaction_or_ctx.respond(view=view, ephemeral=True)


class PrefixContext:
    """Slash-shaped facade over a prefix ``commands.Context``.

    Prefix aliases call the slash handler verbatim with this adapter as the
    context: handlers read ``guild_id``, ``respond``, ``defer`` or
    ``followup`` without knowing which surface they got. Everything else is
    delegated to the underlying context (``author``, ``guild``, ``reply``...).

    Prefix commands are plain messages, so nothing can be ephemeral:
    ``respond`` replies publicly and silently drops ``ephemeral=``. The
    interaction-only 3s window does not exist, so ``defer`` is a no-op and
    ``followup.send`` is a regular channel send.
    """

    def __init__(self, ctx: commands.Context) -> None:
        self._ctx = ctx

    @property
    def guild_id(self) -> int | None:
        guild = self._ctx.guild
        return guild.id if guild is not None else None

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    async def respond(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        return await self._ctx.reply(*args, **kwargs, mention_author=False)

    async def send(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        return await self._ctx.send(*args, **kwargs)

    async def defer(self, *args, **kwargs) -> None:
        """No-op: prefix commands have no interaction window to acknowledge."""
        return None

    @property
    def followup(self):
        """``followup.send`` maps to a plain channel send."""
        return self

"""Standard ephemeral responses as one plain line.

Every user-facing ephemeral notice (errors, warnings, plain confirmations)
renders through one builder so a theme can restyle the surface and every
cog speaks the same visual language: the theme's prefix (``{emoji} ``) plus
the message, one thing per line. The notice layer lives in
:mod:`rosemary.core.feedback`: the prefix is a theme style, so a guild
restyles every notice at once.

Prefix aliases reuse the slash handlers verbatim through
:class:`PrefixContext`, the slash-shaped facade declared below.
"""

from __future__ import annotations

from discord.ext import commands

from rosemary.core.feedback import respond as _respond_notice_line


async def respond_notice(
    interaction_or_ctx,
    bot,
    guild_id: int | None,
    text: str,
    *,
    color: str = "danger",
) -> None:
    """Respond ephemerally with one notice line (ACKs when needed).

    ``color`` is a theme color token: ``danger`` for errors, ``warning`` for
    warnings, ``success`` for confirmations, ``info`` for notices. It picks
    the prefix emoji; the message keeps its own catalog copy.
    """
    await _respond_notice_line(
        interaction_or_ctx, bot, guild_id, text, style=color, ephemeral=True
    )


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

    #: The command surface tells a prefix context from a slash one: the ACK
    #: window and the ephemeral flag only exist on the interaction side.
    is_prefix = True

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

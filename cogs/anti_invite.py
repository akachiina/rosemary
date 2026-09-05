"""Anti-invite filter: delete external invite links on sight.

Deletes messages containing ``discord.gg/`` / ``discord.com/invite/`` codes
that do not belong to this guild, warns the author in-channel, and logs the
block. Members with ``manage_messages`` and the exempt channel are skipped.
Own codes are resolved with a single API lookup per offending message.
"""

import logging
import re

import discord
from discord.ext import commands

from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

_INVITE_RE = re.compile(
    r"(?:discord\.gg|discord\.com/invite|discordapp\.com/invite)/([A-Za-z0-9-]+)",
    re.IGNORECASE,
)


class AntiInviteCog(commands.Cog):
    """External invite link filter."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        guild_id = message.guild.id
        try:
            if not await get_setting(self.bot.storage, guild_id, "anti_invite.enabled"):
                return
            match = _INVITE_RE.search(message.content or "")
            if not match:
                return
            if message.author.guild_permissions.manage_messages:
                return
            exempt = await get_setting(
                self.bot.storage, guild_id, "anti_invite.exempt_channel"
            )
            if exempt and message.channel.id == exempt:
                return
            if await self._is_own_code(message.guild, match.group(1)):
                return
            with _suppress():
                await message.delete()
            if await get_setting(self.bot.storage, guild_id, "anti_invite.warn_on_delete"):
                warning = await text_or(
                    self.bot,
                    guild_id,
                    "anti_invite.warning",
                    await self.bot.translator.t(
                        guild_id, "anti_invite.warning", user=message.author.mention
                    ),
                    user=message.author.mention,
                )
                await message.channel.send(
                    warning, allowed_mentions=discord.AllowedMentions.none()
                )
            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "anti_invite.logs.blocked.title"),
                await log_description(
                    self.bot,
                    guild_id,
                    "anti_invite.logs.blocked.description",
                    user=message.author.mention,
                    channel=message.channel.mention,
                    code=match.group(1),
                ),
                color="warning",
                card_key="anti_invite.logs.blocked.description",
                mention_user_ids=[message.author.id],
            )
        except Exception as exc:
            log.error("Anti-invite check failed: %s", exc)

    async def _is_own_code(self, guild: discord.Guild, code: str) -> bool:
        """Whether an invite code belongs to this guild (one API lookup)."""
        try:
            invite = await self.bot.fetch_invite(code)
        except (discord.NotFound, discord.HTTPException):
            return False
        return invite.guild is not None and invite.guild.id == guild.id


def _suppress():
    import contextlib

    return contextlib.suppress(discord.Forbidden, discord.HTTPException, discord.NotFound)


def setup(bot) -> None:
    bot.add_cog(AntiInviteCog(bot))

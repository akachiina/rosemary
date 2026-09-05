"""Bump Reminder cog for Rosemary.

Detects Disboard bumps, tracks the cooldown, manages anti-camping channel locks,
and sends a ping when the next bump is available.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import commands

from rosemary.core.bump import BumpStore
from rosemary.core.cards import log_description, maybe_view, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.time_parser import TimeParser

log = logging.getLogger(__name__)

DISBOARD_BOT_ID = 302050872383242240

#: Language-neutral brand text present in every Disboard embed/card (its
#: markdown footer linking to disboard.org), regardless of the locale.
DISBOARD_MARKER = "disboard"


class BumpReminderCog(commands.Cog):
    """Detects bumps and sends reminders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = BumpStore(bot.storage.data_dir)
        self._reminder_tasks: dict[int, asyncio.Task] = {}
        self._unlock_tasks: dict[int, asyncio.Task] = {}
        self._started = False

    async def start(self) -> None:
        """Start per-guild recovery once per process.

        Called explicitly from ``RosemaryBot._setup`` because py-cord 2.8.1's
        sync ``add_cog`` never invokes ``cog_load``.
        """
        if self._started:
            return
        self._started = True
        for guild in self.bot.guilds:
            asyncio.create_task(self._check_pending_reminders(guild.id))
            asyncio.create_task(self._check_and_recover_channel(guild.id))

    def cog_unload(self) -> None:
        for task in self._reminder_tasks.values():
            task.cancel()
        for task in self._unlock_tasks.values():
            task.cancel()

    # -- Core Logic ----------------------------------------------------------

    async def _check_pending_reminders(self, guild_id: int) -> None:
        await self.bot.wait_until_ready()
        if not await get_setting(self.bot.storage, guild_id, "bump.enabled"):
            return

        state = await self.store.get_reminder(guild_id)
        if state.get("reminder_sent", True):
            return

        last_bump = await self.store.get_last_bump_time(guild_id)
        if not last_bump:
            return

        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")
        target_time = last_bump + timedelta(seconds=cooldown)
        remaining = (target_time - datetime.now(UTC)).total_seconds()

        if remaining <= 0:
            log.info("Pending bump reminder in %s is overdue, sending now", guild_id)
            await self.send_reminder(guild_id)
        else:
            log.info("Pending bump reminder in %s scheduled in %.0fs", guild_id, remaining)
            self._schedule_reminder(guild_id, remaining)

    async def _check_and_recover_channel(self, guild_id: int) -> None:
        await self.bot.wait_until_ready()
        if await self.store.is_channel_locked(guild_id):
            log.warning("Channel was locked in %s on startup, recovering", guild_id)
            await self.unlock_channel(guild_id)

    def _schedule_reminder(self, guild_id: int, delay_seconds: float) -> None:
        if guild_id in self._reminder_tasks:
            self._reminder_tasks[guild_id].cancel()
        self._reminder_tasks[guild_id] = asyncio.create_task(
            self._wait_and_send_reminder(guild_id, delay_seconds)
        )

    def _schedule_unlock(self, guild_id: int, delay_seconds: float) -> None:
        if guild_id in self._unlock_tasks:
            self._unlock_tasks[guild_id].cancel()
        self._unlock_tasks[guild_id] = asyncio.create_task(
            self._wait_and_unlock(guild_id, delay_seconds)
        )

    async def _wait_and_send_reminder(self, guild_id: int, delay_seconds: float) -> None:
        try:
            anti_camping = await get_setting(
                self.bot.storage, guild_id, "bump.anti_camping.enabled",
            )
            if anti_camping:
                lock_delay = await get_setting(
                    self.bot.storage, guild_id, "bump.anti_camping.lock_delay",
                )
                lock_advance = delay_seconds - lock_delay
                if lock_advance > 0:
                    await asyncio.sleep(lock_advance)
                await self.lock_channel(guild_id)
                remaining_time = lock_delay if lock_advance > 0 else delay_seconds
                await asyncio.sleep(remaining_time)
            else:
                await asyncio.sleep(delay_seconds)

            state = await self.store.get_reminder(guild_id)
            if not state.get("reminder_sent", True):
                await self.send_reminder(guild_id)

            if anti_camping:
                unlock_delay = await get_setting(
                    self.bot.storage, guild_id, "bump.anti_camping.unlock_delay",
                )
                self._schedule_unlock(guild_id, unlock_delay)

        except asyncio.CancelledError:
            if (
                await self.store.is_channel_locked(guild_id)
                and (guild_id not in self._unlock_tasks or self._unlock_tasks[guild_id].done())
            ):
                await self.unlock_channel(guild_id)
            raise
        except Exception as exc:
            log.error("Error in reminder task for %s: %s", guild_id, exc)
            if await self.store.is_channel_locked(guild_id):
                await self.unlock_channel(guild_id)

    async def _wait_and_unlock(self, guild_id: int, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self.unlock_channel(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Error in unlock task for %s: %s", guild_id, exc)
            await self.unlock_channel(guild_id)

    # -- Actions -------------------------------------------------------------

    async def lock_channel(self, guild_id: int) -> None:
        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            overwrites = channel.overwrites
            default_role = guild.default_role
            if default_role not in overwrites:
                overwrites[default_role] = discord.PermissionOverwrite()
            overwrites[default_role].send_messages = False
            await channel.edit(overwrites=overwrites)
            await self.store.mark_channel_locked(guild_id)

            message = await text_or(
                self.bot,
                guild_id,
                "bump.anti_camping",
                await get_setting(self.bot.storage, guild_id, "bump.anti_camping.message"),
            )
            if message:
                lock_message = await channel.send(message)
                await self.store.set_lock_message_id(guild_id, lock_message.id)

            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "bump.logs.channel_locked.title"),
                await log_description(
                    self.bot,
                    guild_id,
                    "bump.logs.channel_locked.description",
                    channel=channel.mention,
                ),
                color="warning",
                card_key="bump.logs.channel_locked.description",
            )
        except Exception as exc:
            log.error("Failed to lock bump channel in %s: %s", guild_id, exc)

    async def unlock_channel(self, guild_id: int) -> None:
        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            overwrites = channel.overwrites
            default_role = guild.default_role
            if default_role in overwrites:
                overwrites[default_role].send_messages = None
                await channel.edit(overwrites=overwrites)
            await self.store.mark_channel_unlocked(guild_id)

            lock_message_id = await self.store.get_lock_message_id(guild_id)
            if lock_message_id:
                try:
                    lock_message = channel.get_partial_message(lock_message_id)
                    await lock_message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as exc:
                    log.warning(
                        "Failed to delete lock message in %s: %s", guild_id, exc,
                    )
                await self.store.set_lock_message_id(guild_id, None)

            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "bump.logs.channel_unlocked.title"),
                await log_description(
                    self.bot,
                    guild_id,
                    "bump.logs.channel_unlocked.description",
                    channel=channel.mention,
                ),
                color="success",
                card_key="bump.logs.channel_unlocked.description",
            )
        except Exception as exc:
            log.error("Failed to unlock bump channel in %s: %s", guild_id, exc)

    async def send_reminder(self, guild_id: int) -> None:
        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        ping_role_id = await get_setting(self.bot.storage, guild_id, "bump.ping_role")
        content = f"<@&{ping_role_id}>" if ping_role_id else ""
        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")
        cooldown_display = TimeParser.format_duration(timedelta(seconds=cooldown))

        view = await maybe_view(self.bot, guild_id, "bump.reminder") or (
            await self._build_reminder_view(guild, cooldown_display)
        )

        try:
            from rosemary.core.mentions import mentions_for

            if content:
                await channel.send(
                    content,
                    allowed_mentions=await mentions_for(
                        self.bot, guild_id, "bump.reminder",
                        role_ids=[ping_role_id] if ping_role_id else [],
                    ),
                )
            await channel.send(
                view=view,
                allowed_mentions=await mentions_for(self.bot, guild_id, "bump.reminder"),
            )
            await self.store.mark_reminder_sent(guild_id)
            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "bump.logs.reminder_sent.title"),
                await log_description(
                    self.bot,
                    guild_id,
                    "bump.logs.reminder_sent.description",
                    channel=channel.mention,
                ),
                color="info",
                card_key="bump.logs.reminder_sent.description",
            )
        except Exception as exc:
            log.error("Failed to send bump reminder in %s: %s", guild_id, exc)

    async def send_thank_you(self, guild_id: int, user_id: int) -> None:
        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")
        cooldown_display = TimeParser.format_duration(timedelta(seconds=cooldown))
        next_bump_time = datetime.now(UTC) + timedelta(seconds=cooldown)

        view = (
            await maybe_view(self.bot, guild_id, "bump.thank_you")
            or await self._build_thank_you_view(
                guild,
                user_id,
                cooldown_display,
                int(next_bump_time.timestamp()),
            )
        )

        try:
            from rosemary.core.mentions import mentions_for

            await channel.send(
                view=view,
                allowed_mentions=await mentions_for(
                    self.bot, guild_id, "bump.thank_you", user_ids=[user_id],
                ),
            )
        except Exception as exc:
            log.error("Failed to send bump thank you in %s: %s", guild_id, exc)

    async def _build_reminder_view(
        self, guild: discord.Guild, cooldown_display: str
    ) -> discord.ui.DesignerView:
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        title = await self.bot.translator.t(guild.id, "bump.messages.reminder_title")
        description = await self.bot.translator.t(
            guild.id,
            "bump.messages.reminder_description",
            cooldown=cooldown_display,
        )

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color(self.bot.theme.style("bump_reminder").color),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(description),
            )
        )
        return view

    async def _build_thank_you_view(
        self,
        guild: discord.Guild,
        user_id: int,
        cooldown_display: str,
        next_bump_timestamp: int,
    ) -> discord.ui.DesignerView:
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        title = await self.bot.translator.t(guild.id, "bump.messages.thank_you_title")
        description = await self.bot.translator.t(
            guild.id,
            "bump.messages.thank_you_description",
            mention=f"<@{user_id}>",
            cooldown=cooldown_display,
            next_bump_timestamp=next_bump_timestamp,
        )

        view = DesignerView(store=False)
        member = guild.get_member(user_id)
        if member and member.display_avatar:
            title_section = discord.ui.Section(
                TextDisplay(self.bot.theme.md("title", title=title)),
                accessory=discord.ui.Thumbnail(member.display_avatar.url),
            )
            items = [title_section, TextDisplay(description)]
        else:
            items = [
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(description),
            ]

        view.add_item(
            designer_container(
                self.bot.theme.color(self.bot.theme.style("bump_reminder").color),
                *items,
            )
        )
        return view

    # -- Listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != DISBOARD_BOT_ID:
            return

        guild = message.guild
        if not guild:
            return

        if not await get_setting(self.bot.storage, guild.id, "bump.enabled"):
            return

        # Marker text Disboard includes in successful bump messages (configurable
        # per guild, since Disboard's language can differ from the bot's).
        marker = (await get_setting(self.bot.storage, guild.id, "bump.detection_text")).lower()
        text = (message.content or "").lower()
        for embed in message.embeds:
            text += " " + (embed.description or "").lower() + " " + (embed.title or "").lower()

        # A bump is detected when any of three language-independent signals fire:
        #  1) the configured marker matches the message text (explicit),
        #  2) the message is a slash-command interaction response (Disboard only
        #     has /bump, and interaction_metadata is always present for those),
        #  3) the brand text "DISBOARD" appears in the message (its markdown
        #     link footer is present on every embed/card, regardless of locale).
        is_bump = bool(marker) and marker in text
        if not is_bump and (message.interaction_metadata or DISBOARD_MARKER in text):
            is_bump = True

        log.info(
            "Disboard message in guild %s: content=%r embeds=%d interaction_metadata=%s "
            "marker=%r disboard_text=%s detected=%s",
            guild.id,
            (message.content or "")[:60],
            len(message.embeds),
            bool(message.interaction_metadata),
            marker,
            DISBOARD_MARKER in text,
            is_bump,
        )

        if not is_bump:
            return

        # Cooldown guard: a /bump response inside the cooldown window is always
        # an error ("wait X hours") — a successful bump cannot happen earlier
        # than Disboard's enforced cooldown. This filters error responses while
        # letting any-language success messages through.
        cooldown = await get_setting(self.bot.storage, guild.id, "bump.cooldown")
        last_bump = await self.store.get_last_bump_time(guild.id)
        if last_bump and (datetime.now(UTC) - last_bump).total_seconds() < cooldown:
            log.info(
                "Ignoring Disboard response in %s (within cooldown, likely error)", guild.id
            )
            return

        user_id = None
        if message.interaction_metadata:
            user_id = message.interaction_metadata.user.id
        elif message.mentions:
            user_id = message.mentions[0].id

        if not user_id:
            return

        recorded = await self.store.record_bump(guild.id, user_id)
        if not recorded:
            log.info("Ignoring stale/replayed bump in %s", guild.id)
            return

        await self.store.add_bump(guild.id, user_id)

        cooldown = await get_setting(self.bot.storage, guild.id, "bump.cooldown")
        self._schedule_reminder(guild.id, cooldown)

        await self.send_thank_you(guild.id, user_id)
        await send_channel_log(
            self.bot,
            guild.id,
            await self.bot.translator.t(guild.id, "bump.logs.bump_recorded.title"),
            await log_description(
                self.bot,
                guild.id,
                "bump.logs.bump_recorded.description",
                user=f"<@{user_id}>",
                channel=message.channel.mention,
            ),
            color="success",
            card_key="bump.logs.bump_recorded.description",
            mention_user_ids=[user_id],
        )

    # -- Commands ------------------------------------------------------------

    @discord.slash_command(
        name="test_bump",
        description="[ADMIN] Simula um bump para testar o sistema",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def test_bump(self, ctx: discord.ApplicationContext) -> None:
        """Simulate a bump."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "bump.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "bump.error_disabled"), ephemeral=True
            )

        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "bump.error_no_channel"), ephemeral=True
            )

        await self.store.record_bump(guild_id, ctx.author.id)
        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")
        self._schedule_reminder(guild_id, cooldown)
        await ctx.response.defer(ephemeral=True)
        await self.send_thank_you(guild_id, ctx.author.id)

        await ctx.respond("✅ Bump simulated successfully.", ephemeral=True)

    @discord.slash_command(
        name="test_bump_reminder",
        description="[ADMIN] Envia o lembrete de bump imediatamente",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def test_bump_reminder(self, ctx: discord.ApplicationContext) -> None:
        """Send the reminder immediately."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "bump.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "bump.error_disabled"), ephemeral=True
            )

        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        if not channel_id:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "bump.error_no_channel"), ephemeral=True
            )

        await ctx.response.defer(ephemeral=True)
        await self.send_reminder(guild_id)
        await ctx.respond("✅ Reminder sent successfully.", ephemeral=True)

    @discord.slash_command(
        name="bump_status",
        description="[ADMIN] Mostra o status do sistema de bump",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def bump_status(self, ctx: discord.ApplicationContext) -> None:
        """Show bump system status."""
        guild_id = ctx.guild_id
        enabled = await get_setting(self.bot.storage, guild_id, "bump.enabled")
        state = await self.store.get_reminder(guild_id)

        last_bump = await self.store.get_last_bump_time(guild_id)
        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")

        next_bump = "N/A"
        if last_bump:
            next_bump = f"<t:{int((last_bump + timedelta(seconds=cooldown)).timestamp())}:R>"

            last_bump_display = (
                f"**Last Bump:** <t:{int(last_bump.timestamp())}:R>"
                if last_bump
                else "**Last Bump:** N/A"
            )
            lines = [
                f"**Enabled:** {enabled}",
                f"**Channel Locked:** {state.get('channel_locked', False)}",
                f"**Reminder Sent:** {state.get('reminder_sent', True)}",
                last_bump_display,
                f"**Next Reminder:** {next_bump}",
            ]

        await ctx.respond("\n".join(lines), ephemeral=True)


def setup(bot) -> None:
    bot.add_cog(BumpReminderCog(bot))

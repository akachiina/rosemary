"""Bump Reminder cog for Rosemary.

Detects Disboard bumps, tracks the cooldown, manages anti-camping channel locks,
and sends a ping when the next bump is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import commands, tasks

from rosemary.core.bump import LOCK_CAMPING, LOCK_SCHEDULE, BumpStore, schedule_open
from rosemary.core.cards import log_description, maybe_view, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.time_parser import TimeParser
from rosemary.core.timezone import resolve_timezone

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
        self._schedule_loop.start()

    def cog_unload(self) -> None:
        for task in self._reminder_tasks.values():
            task.cancel()
        for task in self._unlock_tasks.values():
            task.cancel()
        self._schedule_loop.cancel()

    @tasks.loop(minutes=1)
    async def _schedule_loop(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self.check_schedule(guild)
            except Exception as exc:
                log.error("Bump schedule check failed in %s: %s", guild.id, exc)

    @_schedule_loop.before_loop
    async def _schedule_before_loop(self) -> None:
        await self.bot.wait_until_ready()

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
            if await self._schedule_closed(guild_id):
                # Boot during closed hours: adopt the lock as the schedule's
                # instead of briefly unlocking it.
                await self.store.mark_channel_locked(guild_id, LOCK_SCHEDULE)
                return
            log.warning("Channel was locked in %s on startup, recovering", guild_id)
            await self.unlock_channel(guild_id)

    async def _schedule_closed(self, guild_id: int, *, now: datetime | None = None) -> bool:
        """Whether the fixed schedule currently mandates a locked channel."""
        if not await get_setting(self.bot.storage, guild_id, "bump.schedule.enabled"):
            return False
        tz = resolve_timezone(
            await get_setting(self.bot.storage, guild_id, "general.timezone")
        )
        local = (now or datetime.now(UTC)).astimezone(tz)
        return not schedule_open(
            await get_setting(self.bot.storage, guild_id, "bump.schedule.open_time"),
            await get_setting(self.bot.storage, guild_id, "bump.schedule.close_time"),
            local.hour,
            local.minute,
        )

    async def check_schedule(self, guild: discord.Guild, *, now: datetime | None = None) -> None:
        """Enforce the fixed open/close schedule (transition-only actions).

        ``now`` is injectable for tests; the minute loop always passes ``None``.
        Acting only on transitions keeps messages/logs to one per switch, and
        re-checking every minute recovers missed switches after downtime.
        """
        guild_id = guild.id
        if not await get_setting(self.bot.storage, guild_id, "bump.enabled"):
            return
        if not await get_setting(self.bot.storage, guild_id, "bump.schedule.enabled"):
            return
        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return

        closed = await self._schedule_closed(guild_id, now=now)
        locked = await self.store.is_channel_locked(guild_id)
        source = await self.store.get_lock_source(guild_id)
        if closed and not locked:
            await self._set_schedule_state(guild, channel, locked=True)
        elif not closed and locked and source == LOCK_SCHEDULE:
            await self._set_schedule_state(guild, channel, locked=False)

    async def _set_schedule_state(
        self, guild: discord.Guild, channel: discord.TextChannel, *, locked: bool
    ) -> None:
        """Lock/unlock the channel on behalf of the schedule with its message."""
        from rosemary.core.mentions import mentions_for

        guild_id = guild.id
        try:
            overwrites = channel.overwrites
            default_role = guild.default_role
            if default_role not in overwrites:
                overwrites[default_role] = discord.PermissionOverwrite()
            if locked:
                overwrites[default_role].send_messages = False
                key, log_key, color = (
                    "bump.schedule.close",
                    "bump.logs.schedule_closed.description",
                    "warning",
                )
            else:
                overwrites[default_role].send_messages = None
                key, log_key, color = (
                    "bump.schedule.open",
                    "bump.logs.schedule_opened.description",
                    "success",
                )
            await channel.edit(overwrites=overwrites)
            if locked:
                await self.store.mark_channel_locked(guild_id, LOCK_SCHEDULE)
            else:
                await self.store.mark_channel_unlocked(guild_id)

            ping_role, ping_role_id = await self._ping_role_mention(guild_id)
            message = await text_or(
                self.bot,
                guild_id,
                key,
                await self._localized_message(
                    guild_id,
                    f"bump.schedule.{'close' if locked else 'open'}_message",
                    f"bump.schedule.{'close' if locked else 'open'}_message_default",
                    {"ping_role": ping_role},
                ),
            )
            if message:
                await channel.send(
                    message,
                    allowed_mentions=await mentions_for(
                        self.bot,
                        guild_id,
                        key,
                        role_ids=[ping_role_id] if ping_role_id else [],
                    ),
                )
            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(
                    guild_id,
                    f"bump.logs.{'schedule_closed' if locked else 'schedule_opened'}.title",
                ),
                await log_description(
                    self.bot, guild_id, log_key, channel=channel.mention
                ),
                color=color,
                card_key=log_key,
            )
        except Exception as exc:
            log.error("Failed to apply bump schedule in %s: %s", guild_id, exc)

    async def _ping_role_mention(self, guild_id: int) -> tuple[str, int | None]:
        """Render ``{ping_role}`` from the guild's bump ping role (or empty)."""
        ping_role_id = await get_setting(self.bot.storage, guild_id, "bump.ping_role")
        if ping_role_id:
            return f"<@&{ping_role_id}>", int(ping_role_id)
        return "", None

    async def _localized_message(
        self, guild_id: int, setting_key: str, default_key: str, variables: dict | None = None
    ) -> str:
        """Guild-customized message, else the default in the guild's language.

        A stored setting value always wins (zero migration: customized guilds
        keep their text). Guilds that never touched the setting get the
        translated default, so e.g. pt-BR guilds no longer receive the
        English fallback baked into the setting spec.

        Formatting happens in a single ``safe_format`` pass over theme emojis
        plus ``variables``: going through ``t()`` instead would abort the
        whole format (emojis included) on the first unknown placeholder.
        """
        from rosemary.core.cards import safe_format

        raw = await self.bot.storage.get(guild_id)
        if setting_key in raw:
            template = await get_setting(self.bot.storage, guild_id, setting_key)
        else:
            template = await self.bot.translator.raw(guild_id, default_key)
        if not isinstance(template, str):
            template = str(template)
        mapping = {**(self.bot.theme.emojis if self.bot.theme else {}), **dict(variables or {})}
        return safe_format(template, mapping)

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
            await self.store.mark_channel_locked(guild_id, LOCK_CAMPING)

            from rosemary.core.mentions import mentions_for

            message = await text_or(
                self.bot,
                guild_id,
                "bump.anti_camping",
                await self._localized_message(
                    guild_id,
                    "bump.anti_camping.message",
                    "bump.anti_camping.message_default",
                ),
            )
            if message:
                lock_message = await channel.send(
                    message,
                    allowed_mentions=await mentions_for(
                        self.bot, guild_id, "bump.anti_camping"
                    ),
                )
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

        if await self._schedule_closed(guild_id):
            # Schedule wins: keep the channel locked, only drop the
            # anti-camping lock notice so it does not linger.
            lock_message_id = await self.store.get_lock_message_id(guild_id)
            if lock_message_id:
                with contextlib.suppress(discord.NotFound, discord.HTTPException):
                    await channel.get_partial_message(lock_message_id).delete()
                await self.store.set_lock_message_id(guild_id, None)
            await self.store.mark_channel_locked(guild_id, LOCK_SCHEDULE)
            log.info("Keeping bump channel locked in %s (schedule closed)", guild_id)
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

        view = await maybe_view(
            self.bot,
            guild_id,
            "bump.reminder",
            {"cooldown": cooldown_display},
        ) or (await self._build_reminder_view(guild, cooldown_display))

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

        view = await maybe_view(
            self.bot,
            guild_id,
            "bump.thank_you",
            {
                "mention": f"<@{user_id}>",
                "cooldown": cooldown_display,
                "next_bump_timestamp": int(next_bump_time.timestamp()),
            },
        ) or await self._build_thank_you_view(
            guild,
            user_id,
            cooldown_display,
            int(next_bump_time.timestamp()),
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

    async def _is_bump_bot(self, message: discord.Message) -> bool:
        """Check the author against the guild-configured bump bot ID.

        Falls back to the Disboard default when the setting is empty/invalid,
        so existing guilds keep working without migration.
        """
        if not message.guild:
            return message.author.id == DISBOARD_BOT_ID
        try:
            configured = await get_setting(
                self.bot.storage, message.guild.id, "bump.detection_bot_id"
            )
            return message.author.id == int(str(configured).strip())
        except (TypeError, ValueError, AttributeError):
            return message.author.id == DISBOARD_BOT_ID

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._is_bump_bot(message):
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
        description="[ADMIN] Simulate a bump to test the system",
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

        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        await self.store.record_bump(guild_id, ctx.author.id)
        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")
        self._schedule_reminder(guild_id, cooldown)
        await self.send_thank_you(guild_id, ctx.author.id)

        await ctx.respond(
            await self.bot.translator.t(guild_id, "bump.test.simulated"), ephemeral=True
        )

    @discord.slash_command(
        name="test_bump_reminder",
        description="[ADMIN] Send the bump reminder immediately",
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

        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        await self.send_reminder(guild_id)
        await ctx.respond(
            await self.bot.translator.t(guild_id, "bump.test.reminder_sent"), ephemeral=True
        )

    @discord.slash_command(
        name="bump_status",
        description="[ADMIN] Show the bump system status",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def bump_status(self, ctx: discord.ApplicationContext) -> None:
        """Show bump system status."""
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        enabled = await get_setting(self.bot.storage, guild_id, "bump.enabled")
        state = await self.store.get_reminder(guild_id)

        last_bump = await self.store.get_last_bump_time(guild_id)
        cooldown = await get_setting(self.bot.storage, guild_id, "bump.cooldown")

        none_text = await t(guild_id, "bump.status.none")
        next_bump = none_text
        if last_bump:
            next_bump = f"<t:{int((last_bump + timedelta(seconds=cooldown)).timestamp())}:R>"
            last_bump_display = await t(
                guild_id, "bump.status.last_bump", timestamp=int(last_bump.timestamp())
            )
        else:
            last_bump_display = await t(guild_id, "bump.status.last_bump_na")
        lines = [
            await t(guild_id, "bump.status.enabled", enabled=enabled),
            await t(
                guild_id, "bump.status.channel_locked", locked=state.get("channel_locked", False)
            ),
            await t(
                guild_id, "bump.status.reminder_sent", sent=state.get("reminder_sent", True)
            ),
            last_bump_display,
            await t(guild_id, "bump.status.next_reminder", next=next_bump),
        ]

        await ctx.respond("\n".join(lines), ephemeral=True)


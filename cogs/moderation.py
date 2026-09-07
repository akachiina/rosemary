"""Moderation commands: /ban, /kick, /warn, /mute, /warnings, /unwarn.

All user-facing strings are translation keys resolved through ``t()``. Actions
are gated by a confirmation dialog (notify / silent / cancel) and logged to the
guild's configured log channel via ``core.debug.send_channel_log``.

Note: unlike the other new modules, this file intentionally does NOT import
``from __future__ import annotations``: py-cord inspects slash-command
annotations with ``inspect.signature`` and never evaluates them, so stringified
``discord.Option(...)`` annotations would break option parsing at runtime.
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from discord.ext import commands

from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.storage import GuildStorage
from rosemary.core.time_parser import TimeParser, localized_aliases
from rosemary.core.timezone import resolve_timezone
from rosemary.ui.confirm import ConfirmView
from rosemary.ui.containers import (
    ActionRow,
    TextDisplay,
    designer_container,
    header_display,
)

log = logging.getLogger(__name__)

#: action -> permissions the author and the bot must hold.
_ACTION_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "ban": ("ban_members",),
    "kick": ("kick_members",),
    "mute": ("moderate_members",),
    "warn": ("moderate_members",),
}

_ACTION_LOG_COLORS: dict[str, str] = {
    "ban": "danger",
    "kick": "danger",
    "mute": "warning",
    "warn": "warning",
}


class WarningsStore:
    """Warnings persistence in its own ``warnings.json`` (per-guild).

    Accepts either a :class:`GuildStorage` (uses its ``data_dir``) or a raw
    data directory, so warnings never mix with ``settings.json`` defaults
    like ``language``. Existing tests pass ``GuildStorage(tmp_path)`` and
    keep working unchanged.
    """

    def __init__(self, storage: GuildStorage | Any) -> None:
        from pathlib import Path

        data_dir = storage.data_dir if isinstance(storage, GuildStorage) else Path(storage)
        self.storage = GuildStorage(data_dir, filename="warnings.json", use_defaults=False)

    async def _load(self, guild_id: int) -> dict[str, list[dict[str, Any]]]:
        data = await self.storage.get(guild_id)
        return dict(data.get("warnings") or {})

    async def get_warnings(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return list((await self._load(guild_id)).get(str(user_id), []))

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        reason: str,
        moderator_id: int,
        moderator_tag: str,
    ) -> int:
        """Append a warning; returns the member's new warning count."""
        warnings = await self._load(guild_id)
        entry = {
            "reason": reason,
            "moderator_id": moderator_id,
            "moderator_tag": moderator_tag,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        entries = list(warnings.get(str(user_id), []))
        entries.append(entry)
        warnings[str(user_id)] = entries
        await self.storage.set(guild_id, "warnings", warnings)
        return len(entries)

    async def remove_warning(self, guild_id: int, user_id: int, index: int) -> bool:
        """Remove the warning at ``index`` (0-based); ``False`` when out of range."""
        warnings = await self._load(guild_id)
        entries = list(warnings.get(str(user_id), []))
        if not 0 <= index < len(entries):
            return False
        del entries[index]
        if entries:
            warnings[str(user_id)] = entries
        else:
            warnings.pop(str(user_id), None)
        await self.storage.set(guild_id, "warnings", warnings)
        return True

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        """Remove every warning for a member; returns how many were removed."""
        warnings = await self._load(guild_id)
        removed = len(warnings.get(str(user_id), []))
        if removed:
            warnings.pop(str(user_id), None)
            await self.storage.set(guild_id, "warnings", warnings)
        return removed


class ModerationConfirmView(ConfirmView):
    """Confirmation dialog: confirm + notify, confirm silent, or cancel.

    The container color reflects the outcome: success (green) after the action,
    danger (red) on failure, warning (yellow) while pending or cancelled. The
    terminal state shows only the result: no title, no summary, no buttons.
    """

    def __init__(
        self,
        bot,
        *,
        guild_id: int,
        owner_id: int | None,
        action: str,
        summary: str,
        on_confirm: Any,
    ) -> None:
        self.action = action
        self.summary = summary
        self._execute = on_confirm
        self._notify = True
        super().__init__(
            bot,
            guild_id=guild_id,
            owner_id=owner_id,
            on_confirm=self._run,
            failure_key="moderation.error_failed",
            cancelled_key="moderation.cancelled",
        )
        self.register("mod_confirm_notify", self._confirm_notify)
        self.register("mod_confirm_silent", self._confirm_silent)
        self.register("mod_cancel", self._cancel)

    async def _run(self, interaction: discord.Interaction) -> str:
        return await self._execute(self._notify)

    async def _confirm_notify(self, interaction: discord.Interaction) -> None:
        self._notify = True
        await self._confirm(interaction)

    async def _confirm_silent(self, interaction: discord.Interaction) -> None:
        self._notify = False
        await self._confirm(interaction)

    async def question_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        return [
            TextDisplay(
                self.bot.theme.md(
                    "title",
                    title=await t(self.guild_id, f"{self.action}.confirm_title"),
                )
            ),
            TextDisplay(self.summary),
        ]

    async def action_rows(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        return [
            ActionRow(
                self.make_button(
                    custom_id="mod_confirm_notify",
                    label=await t(self.guild_id, "moderation.confirm_notify"),
                    style=discord.ButtonStyle.success,
                ),
                self.make_button(
                    custom_id="mod_confirm_silent",
                    label=await t(self.guild_id, "moderation.confirm_silent"),
                    style=discord.ButtonStyle.secondary,
                ),
                self.make_button(
                    custom_id="mod_cancel",
                    label=await t(self.guild_id, "moderation.cancel"),
                    style=discord.ButtonStyle.danger,
                ),
            )
        ]


class ClearWarningsView(ConfirmView):
    """Confirmation dialog for removing every warning of a member."""

    def __init__(
        self,
        bot,
        *,
        guild_id: int,
        owner_id: int | None,
        member_mention: str,
        on_confirm: Any,
    ) -> None:
        self.member_mention = member_mention
        self._clear = on_confirm
        super().__init__(
            bot,
            guild_id=guild_id,
            owner_id=owner_id,
            on_confirm=self._run,
            confirm_style=discord.ButtonStyle.danger,
            failure_key="moderation.error_failed",
            cancelled_key="moderation.cancelled",
        )
        self.register("clear_confirm", self._confirm)
        self.register("clear_cancel", self._cancel)

    async def _run(self, interaction: discord.Interaction) -> str:
        count = await self._clear()
        return await self.bot.translator.t(
            self.guild_id, "unwarn.cleared", member=self.member_mention, count=count
        )

    async def question_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        return [
            TextDisplay(
                self.bot.theme.md(
                    "title", title=await t(self.guild_id, "unwarn.clear_all_title")
                )
            ),
            TextDisplay(
                await t(
                    self.guild_id, "unwarn.clear_all_summary", member=self.member_mention
                )
            ),
        ]

    async def action_rows(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        return [
            ActionRow(
                self.make_button(
                    custom_id="clear_confirm",
                    label=await t(self.guild_id, "moderation.confirm_clear"),
                    style=discord.ButtonStyle.danger,
                ),
                self.make_button(
                    custom_id="clear_cancel",
                    label=await t(self.guild_id, "moderation.cancel"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        ]


class ModerationCog(commands.Cog):
    """Moderation commands."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.warnings = WarningsStore(bot.storage)

    # -- helpers -----------------------------------------------------------

    def _response_view(self, title: str, *, color: str = "info") -> discord.ui.DesignerView:
        view = discord.ui.DesignerView(store=False)
        view.add_item(designer_container(self.bot.theme.color(color), header_display(title)))
        return view

    def _ctx_guild_id(self, ctx) -> int | None:
        """Guild ID for a slash (:class:`ApplicationContext`) or prefix
        (:class:`commands.Context`) command context."""
        guild_id = getattr(ctx, "guild_id", None)
        if guild_id is None and ctx.guild is not None:
            guild_id = ctx.guild.id
        return guild_id

    async def _respond(self, ctx, *, title: str, color: str) -> None:
        """Send a plain one-message response on a slash or prefix context."""
        view = self._response_view(title, color=color)
        if isinstance(ctx, discord.ApplicationContext):
            await ctx.respond(view=view, ephemeral=True)
        else:
            await ctx.reply(view=view)

    async def _respond_error(self, ctx, key: str) -> None:
        await self._respond(
            ctx, title=await self.bot.translator.t(self._ctx_guild_id(ctx), key), color="danger"
        )

    async def _respond_success(self, ctx, key: str) -> None:
        await self._respond(
            ctx, title=await self.bot.translator.t(self._ctx_guild_id(ctx), key), color="success"
        )

    async def _send_view(self, ctx, view: discord.ui.View) -> None:
        """Attach an interactive menu to a slash or prefix message."""
        if isinstance(ctx, discord.ApplicationContext):
            await ctx.respond(view=view, ephemeral=True)
        else:
            await ctx.reply(view=view)

    async def _can_moderate(
        self, ctx: discord.ApplicationContext, target: discord.Member, action: str
    ) -> tuple[bool, str]:
        """Return ``(allowed, error_key)`` for acting on ``target``."""
        if not await get_setting(self.bot.storage, self._ctx_guild_id(ctx), "moderation.enabled"):
            return False, "moderation.error_disabled"
        if target.id == ctx.author.id:
            return False, "moderation.error_self"
        if target.id == ctx.guild.me.id:
            return False, "moderation.error_self_bot"
        if ctx.author.id != ctx.guild.owner_id:
            required = _ACTION_PERMISSIONS[action]
            if not _has_permissions(ctx.author.guild_permissions, required):
                return False, "moderation.error_author_permissions"
        if not _has_permissions(ctx.guild.me.guild_permissions, _ACTION_PERMISSIONS[action]):
            return False, "moderation.error_bot_permissions"
        # Discord only enforces role hierarchy for non-owners: the server owner
        # can act on any member regardless of roles (a role-less owner would
        # otherwise be blocked by their own @everyone role).
        if (
            ctx.author.id != ctx.guild.owner_id
            and target.top_role >= ctx.author.top_role
        ):
            return False, "moderation.error_hierarchy"
        if target.top_role >= ctx.guild.me.top_role:
            return False, "moderation.error_bot_hierarchy"
        return True, ""

    async def _confirm_and_execute(
        self,
        ctx,
        action: str,
        targets: list[discord.Member],
        reason: str | None,
        duration: timedelta | None = None,
    ) -> None:
        t = self.bot.translator.t
        reason_text = reason or await t(self._ctx_guild_id(ctx), "moderation.no_reason")
        if action == "mute":
            summary = await t(
                self._ctx_guild_id(ctx),
                "mute.summary",
                member="\n".join(target.mention for target in targets),
                reason=reason_text,
                duration=(
                    TimeParser.format_duration(duration) if duration is not None else "-"
                ),
            )
        elif len(targets) == 1:
            summary = await t(
                self._ctx_guild_id(ctx),
                "moderation.summary",
                member=targets[0].mention,
                reason=reason_text,
            )
        else:
            summary = await t(
                self._ctx_guild_id(ctx),
                "moderation.multi_summary",
                targets="\n".join(target.mention for target in targets),
                reason=reason_text,
            )

        async def execute(notify: bool) -> str:
            results = []
            for target in targets:
                results.append(
                    await self._execute_action(
                        self._ctx_guild_id(ctx),
                        ctx.guild.name,
                        ctx.author,
                        target,
                        action,
                        reason_text,
                        duration,
                        notify,
                    )
                )
            return "\n".join(results)

        view = ModerationConfirmView(
            self.bot,
            guild_id=self._ctx_guild_id(ctx),
            owner_id=ctx.author.id,
            action=action,
            summary=summary,
            on_confirm=execute,
        )
        await view.prepare()
        await self._send_view(ctx, view)

    async def _execute_action(
        self,
        guild_id: int,
        guild_name: str,
        moderator: discord.Member,
        target: discord.Member,
        action: str,
        reason: str,
        duration: timedelta | None,
        notify: bool,
    ) -> str:
        t = self.bot.translator.t
        moderator_tag = str(moderator)

        if notify:
            from rosemary.core.cards import text_or

            # Only mute carries a duration; ban/kick/warn pass None.
            duration_display = (
                TimeParser.format_duration(duration) if duration is not None else None
            )
            common = {"guild": guild_name, "moderator": moderator.mention, "reason": reason}
            dm_template_key = f"{action}.dm"
            translated = (
                await t(
                    guild_id,
                    "mute.dm",
                    duration=duration_display,
                    **common,
                )
                if action == "mute"
                else await t(guild_id, dm_template_key, **common)
            )
            variables = {**common}
            if duration_display is not None:
                variables["duration"] = duration_display
            dm_text = await text_or(self.bot, guild_id, dm_template_key, translated, **variables)
            with contextlib.suppress(discord.Forbidden):
                await target.send(dm_text)

        if action == "ban":
            await target.ban(reason=reason)
        elif action == "kick":
            await target.kick(reason=reason)
        elif action == "mute":
            await target.timeout_for(duration, reason=reason)
        elif action == "warn":
            count = await self.warnings.add_warning(
                guild_id, target.id, reason, moderator.id, moderator_tag
            )

        fields = [
            self.bot.theme.md(
                "entry",
                label=await t(guild_id, "moderation.log.member"),
                value=target.mention,
            ),
            self.bot.theme.md(
                "entry",
                label=await t(guild_id, "moderation.log.moderator"),
                value=moderator.mention,
            ),
            self.bot.theme.md(
                "entry",
                label=await t(guild_id, "moderation.log.reason"),
                value=reason,
            ),
        ]
        if action == "mute":
            fields.append(
                self.bot.theme.md(
                    "entry",
                    label=await t(guild_id, "moderation.log.duration"),
                    value=(
                        TimeParser.format_duration(duration)
                        if duration is not None
                        else "-"
                    ),
                )
            )
        await send_channel_log(
            self.bot,
            guild_id,
            await t(guild_id, f"moderation.log.{action}.title"),
            "\n".join(fields),
            color=_ACTION_LOG_COLORS[action],
            card_key=f"moderation.logs.{action}.description",
        )

        if action == "warn":
            limit = await get_setting(self.bot.storage, guild_id, "moderation.warn_limit")
            result = await t(
                guild_id,
                "warn.success",
                member=target.mention,
                count=count,
                limit=limit,
            )
            if count >= limit and await get_setting(
                self.bot.storage, guild_id, "moderation.auto_ban_enabled"
            ):
                try:
                    await target.ban(reason=reason)
                    result += "\n" + await t(guild_id, "moderation.auto_ban_applied", limit=limit)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Auto-ban failed for %s: %s", target, exc)
                    result += "\n" + await t(guild_id, "moderation.auto_ban_failed")
            return result
        if action == "mute":
            return await t(
                guild_id,
                "mute.success",
                member=target.mention,
                duration=(
                    TimeParser.format_duration(duration) if duration is not None else "-"
                ),
            )
        return await t(guild_id, f"{action}.success", member=target.mention)

    # -- commands ----------------------------------------------------------

    @discord.slash_command(
        name="ban",
        description="Ban a member",
        default_member_permissions=discord.Permissions(ban_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def ban(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to ban"),
        reason: discord.Option(str, description="Reason", default=None),
    ) -> None:
        """Ban a member from the server."""
        ok, error_key = await self._can_moderate(ctx, member, "ban")
        if not ok:
            return await self._respond_error(ctx, error_key)
        await self._confirm_and_execute(ctx, "ban", [member], reason)

    @discord.slash_command(
        name="kick",
        description="Kick a member",
        default_member_permissions=discord.Permissions(kick_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def kick(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to kick"),
        reason: discord.Option(str, description="Reason", default=None),
    ) -> None:
        """Kick a member from the server."""
        ok, error_key = await self._can_moderate(ctx, member, "kick")
        if not ok:
            return await self._respond_error(ctx, error_key)
        await self._confirm_and_execute(ctx, "kick", [member], reason)

    @discord.slash_command(
        name="mute",
        description="Mute a member for a duration",
        default_member_permissions=discord.Permissions(moderate_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def mute(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to mute"),
        duration: discord.Option(
            str, description="Duration (e.g. 10m, 2h, 3d)", default=None
        ),
        reason: discord.Option(str, description="Reason", default=None),
    ) -> None:
        """Timeout a member for the given duration."""
        if duration is None:
            seconds = await get_setting(
                self.bot.storage, self._ctx_guild_id(ctx), "moderation.mute_seconds"
            )
            parsed = timedelta(seconds=seconds)
        else:
            aliases = await localized_aliases(self.bot.translator, self._ctx_guild_id(ctx))
            parsed = TimeParser.parse(duration, aliases=aliases)
            if parsed is None:
                return await self._respond_error(ctx, "mute.invalid_duration")
        ok, error_key = await self._can_moderate(ctx, member, "mute")
        if not ok:
            return await self._respond_error(ctx, error_key)
        await self._confirm_and_execute(ctx, "mute", [member], reason, parsed)

    @discord.slash_command(
        name="warn",
        description="Warn a member",
        default_member_permissions=discord.Permissions(moderate_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def warn(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to warn"),
        reason: discord.Option(str, description="Reason", default=None),
    ) -> None:
        """Record a warning for a member."""
        ok, error_key = await self._can_moderate(ctx, member, "warn")
        if not ok:
            return await self._respond_error(ctx, error_key)
        await self._confirm_and_execute(ctx, "warn", [member], reason)

    @discord.slash_command(
        name="warnings",
        description="List a member's warnings",
        default_member_permissions=discord.Permissions(moderate_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def warnings(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to inspect"),
    ) -> None:
        """Show every warning recorded for a member."""
        ok, error_key = await self._can_moderate(ctx, member, "warn")
        if not ok:
            return await self._respond_error(ctx, error_key)
        t = self.bot.translator.t
        theme = self.bot.theme
        guild_id = self._ctx_guild_id(ctx)
        tz = resolve_timezone(await get_setting(self.bot.storage, guild_id, "general.timezone"))

        def _local_date(raw: str) -> str:
            try:
                return datetime.fromisoformat(raw).astimezone(tz).strftime("%Y-%m-%d")
            except ValueError:
                return "-"

        entries = await self.warnings.get_warnings(guild_id, member.id)
        parts = [
            TextDisplay(
                theme.md(
                    "title",
                    title=await t(guild_id, "warnings.title", member=member.mention),
                )
            )
        ]
        if not entries:
            parts.append(TextDisplay(await t(guild_id, "warnings.none")))
        else:
            for index, entry in enumerate(entries, start=1):
                parts.append(
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=f"#{index}",
                            value=await t(
                                guild_id,
                                "warnings.entry",
                                reason=entry.get("reason", "-"),
                                moderator=f"<@{entry.get('moderator_id', 0)}>",
                                date=_local_date(entry.get("timestamp", "")),
                            ),
                        )
                    )
                )
        view = discord.ui.DesignerView(store=False)
        view.add_item(
            designer_container(theme.color("success" if not entries else "brand"), *parts)
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="unwarn",
        description="Remove a warning",
        default_member_permissions=discord.Permissions(moderate_members=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def unwarn(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to un-warn"),
        index: discord.Option(int, description="Warning number (omit to clear all)", default=None),
    ) -> None:
        """Remove one warning, or clear all of a member's warnings."""
        ok, error_key = await self._can_moderate(ctx, member, "warn")
        if not ok:
            return await self._respond_error(ctx, error_key)
        t = self.bot.translator.t

        if index is None:
            count = len(await self.warnings.get_warnings(self._ctx_guild_id(ctx), member.id))
            if count == 0:
                return await self._respond_success(ctx, "unwarn.none_for")

            async def clear_all() -> int:
                return await self.warnings.clear_warnings(self._ctx_guild_id(ctx), member.id)

            view = ClearWarningsView(
                self.bot,
                guild_id=self._ctx_guild_id(ctx),
                owner_id=ctx.author.id,
                member_mention=member.mention,
                on_confirm=clear_all,
            )
            await view.prepare()
            return await ctx.respond(view=view, ephemeral=True)

        removed = await self.warnings.remove_warning(self._ctx_guild_id(ctx), member.id, index - 1)
        if not removed:
            return await self._respond_error(ctx, "unwarn.invalid_index")
        await ctx.respond(
            view=self._response_view(
                await t(
                    self._ctx_guild_id(ctx),
                    "unwarn.removed",
                    member=member.mention,
                    index=index,
                ),
                color="success",
            ),
            ephemeral=True,
        )

    # -- prefix commands ---------------------------------------------------

    @commands.command(name="ban")
    async def ban_prefix(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],
        *,
        reason: str | None = None,
    ) -> None:
        """Prefix alias: !ban @member... [reason]"""
        await self._prefix_action(ctx, "ban", members, reason)

    @commands.command(name="kick")
    async def kick_prefix(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],
        *,
        reason: str | None = None,
    ) -> None:
        """Prefix alias: !kick @member... [reason]"""
        await self._prefix_action(ctx, "kick", members, reason)

    @commands.command(name="warn")
    async def warn_prefix(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],
        *,
        reason: str | None = None,
    ) -> None:
        """Prefix alias: !warn @member... [reason]"""
        await self._prefix_action(ctx, "warn", members, reason)

    @commands.command(name="mute")
    async def mute_prefix(
        self,
        ctx: commands.Context,
        members: commands.Greedy[discord.Member],
        duration: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        """Prefix alias: !mute @member... [duration] [reason]"""
        await self._prefix_action(ctx, "mute", members, reason, duration)

    async def _prefix_action(
        self,
        ctx: commands.Context,
        action: str,
        members: list[discord.Member],
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        """Shared flow for prefix commands: validate, then confirm/execute."""
        t = self.bot.translator.t
        targets = list(dict.fromkeys(members))
        if not targets:
            return await self._respond(
                ctx,
                title=await t(self._ctx_guild_id(ctx), "moderation.error_no_members"),
                color="danger",
            )
        parsed = None
        if action == "mute":
            aliases = await localized_aliases(self.bot.translator, self._ctx_guild_id(ctx))
            if not duration or TimeParser.parse(duration, aliases=aliases) is None:
                return await self._respond(
                    ctx,
                    title=await t(self._ctx_guild_id(ctx), "mute.invalid_duration"),
                    color="danger",
                )
            parsed = TimeParser.parse(duration, aliases=aliases)
        blocked: list[str] = []
        valid: list[discord.Member] = []
        for target in targets:
            ok, error_key = await self._can_moderate(ctx, target, action)
            if ok:
                valid.append(target)
            else:
                blocked.append(await t(self._ctx_guild_id(ctx), error_key))
        if blocked:
            return await self._respond(
                ctx, title="\n".join(blocked), color="danger"
            )
        await self._confirm_and_execute(ctx, action, valid, reason, parsed)


def _has_permissions(permissions: discord.Permissions, names: tuple[str, ...]) -> bool:
    return all(getattr(permissions, name, False) for name in names)

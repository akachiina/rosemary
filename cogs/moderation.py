"""Moderation commands: /ban, /kick, /warn, /mute, /warnings, /unwarn.

Every command also has a prefix alias (!ban, !kick, !warn, !mute, !warnings,
!unwarn) that reuses the same handler, confirmation dialog and logs through
:class:`rosemary.ui.responses.PrefixContext`.

All user-facing strings are translation keys resolved through ``t()``. Actions
are gated by a confirmation dialog (notify / silent / cancel) and logged to the
guild's configured log channel via ``core.debug.send_log``.

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

from rosemary.core.command_surface import Surface, notice, run
from rosemary.core.debug import send_log
from rosemary.core.settings import get_setting
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import theme_for
from rosemary.core.time_parser import TimeParser, localized_aliases
from rosemary.core.timezone import resolve_timezone
from rosemary.ui.confirm import ConfirmResult, ConfirmView
from rosemary.ui.containers import (
    ActionRow,
    TextDisplay,
)
from rosemary.ui.responses import PrefixContext

log = logging.getLogger(__name__)

#: The surface of the moderation family: the confirmation panel is the answer
#: (the command never defers), the answer is ephemeral for a slash command and
#: public for a prefix one, and the family checks the permissions itself (it
#: knows the role hierarchy, which a plain permission check does not).
MODERATION = Surface(card_key="moderation.error_failed", defer=False)

#: action -> permissions the author and the bot must hold.
_ACTION_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "ban": ("ban_members",),
    "kick": ("kick_members",),
    "mute": ("moderate_members",),
    "warn": ("moderate_members",),
}

#: The verb icon of each success notice (the notice layer owns the icon).
_SUCCESS_ICONS: dict[str, str] = {
    "ban.success": "ban",
    "kick.success": "kick",
    "mute.success": "mute",
    "warn.success": "warning",
    "unwarn.cleared": "minus",
    "unwarn.removed": "minus",
    "unwarn.none_for": "info",
}

#: The icon of an error whose subject is clearer than the generic tone.
_ERROR_ICONS: dict[str, str] = {
    "mute.invalid_duration": "clock",
    "unwarn.invalid_index": "minus",
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

    async def _run(self, interaction: discord.Interaction) -> ConfirmResult:
        text = await self._execute(self._notify)
        icon = _SUCCESS_ICONS.get(f"{self.action}.success", "success")
        return ConfirmResult(text=text, state="success", icon=icon)

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

    async def _run(self, interaction: discord.Interaction) -> ConfirmResult:
        count = await self._clear()
        text = await self.bot.translator.t(
            self.guild_id, "unwarn.cleared", member=self.member_mention, count=count
        )
        return ConfirmResult(text=text, state="success", icon="minus")

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

    #: helpers -----------------------------------------------------------

    def _ctx_guild_id(self, ctx) -> int | None:
        """Guild ID for a slash (:class:`ApplicationContext`) or prefix
        (:class:`commands.Context`) command context."""
        guild_id = getattr(ctx, "guild_id", None)
        if guild_id is None and ctx.guild is not None:
            guild_id = ctx.guild.id
        return guild_id

    async def _respond(
        self, ctx, *, title: str, color: str, icon: str | None = None
    ) -> None:
        """Send one plain notice line on a slash or prefix context."""
        from rosemary.core.feedback import line as notice_line

        content = notice_line(
            self.bot, self._ctx_guild_id(ctx), title, style=color, icon=icon
        )
        # The notice never notifies: the copy names the target of the action.
        mentions = discord.AllowedMentions.none()
        if isinstance(ctx, discord.ApplicationContext):
            await ctx.respond(content, ephemeral=True, allowed_mentions=mentions)
        else:
            await ctx.reply(content, allowed_mentions=mentions)

    async def _respond_error(self, ctx, key: str, **variables) -> None:
        """One error notice (the theme's prefix, never a ping)."""
        await notice(
            ctx,
            self.bot,
            self._ctx_guild_id(ctx),
            key,
            style="danger",
            icon=_ERROR_ICONS.get(key, "error"),
            ephemeral=isinstance(ctx, discord.ApplicationContext),
            **variables,
        )

    async def _respond_success(self, ctx, key: str, **variables) -> None:
        """One success notice (the theme's prefix, never a ping)."""
        await notice(
            ctx,
            self.bot,
            self._ctx_guild_id(ctx),
            key,
            style="success",
            icon=_SUCCESS_ICONS.get(key),
            ephemeral=isinstance(ctx, discord.ApplicationContext),
            **variables,
        )

    async def _send_view(self, ctx, view: discord.ui.View) -> None:
        """Attach an interactive menu to a slash or prefix message."""
        kwargs = {}
        message_kwargs = getattr(view, "message_kwargs", None)
        if callable(message_kwargs):
            kwargs = await message_kwargs()
        if isinstance(ctx, discord.ApplicationContext):
            await ctx.respond(**kwargs, ephemeral=True)
        else:
            await ctx.reply(**kwargs)

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

    def _stamp_bot_action(self, guild_id: int, user_id: int, kind: str) -> None:
        """Stamp an action this cog is about to execute (ban/kick/timeout).

        The feature posts its own log card with the real moderator, so the
        gateway cards (audit ban, audit timeout, welcome leave) would only
        duplicate it. Stamped BEFORE the API call: delivery order is not
        guaranteed and the stamp window covers a slow call.
        """
        from rosemary.cogs.audit import AuditCog

        get_cog = getattr(self.bot, "get_cog", None)
        audit = get_cog("AuditCog") if callable(get_cog) else None
        if isinstance(audit, AuditCog):
            noter = {
                "ban": audit.note_bot_ban,
                "kick": audit.note_bot_kick,
                "timeout": audit.note_bot_timeout,
            }.get(kind)
            if noter is not None:
                noter(guild_id, user_id)

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
            from rosemary.ui.boost_dm import send as send_dm

            # Themed overrides resolve through the shared DM pipeline: plain
            # docs stay plain, rich docs go out as Components V2. Only mute
            # carries a duration; ban/kick/warn pass None.
            variables: dict[str, Any] = {
                "guild": guild_name,
                "moderator": moderator.mention,
                "reason": reason,
            }
            if action == "mute" and duration is not None:
                variables["duration"] = TimeParser.format_duration(duration)
            with contextlib.suppress(discord.Forbidden):
                await send_dm(self.bot, target, guild_id, f"{action}.dm", **variables)

        if action == "ban":
            # Stamp before the API call: this ban posts its own log card below
            # and the gateway audit card must not race in first.
            self._stamp_bot_action(guild_id, target.id, "ban")
            await target.ban(reason=reason)
        elif action == "kick":
            self._stamp_bot_action(guild_id, target.id, "kick")
            await target.kick(reason=reason)
        elif action == "mute":
            self._stamp_bot_action(guild_id, target.id, "timeout")
            await target.timeout_for(duration, reason=reason)
        elif action == "warn":
            count = await self.warnings.add_warning(
                guild_id, target.id, reason, moderator.id, moderator_tag
            )

        await send_log(
            self.bot,
            guild_id,
            f"moderation.logs.{action}",
            member=target.mention,
            member_id=str(target.id),
            user_avatar=target.display_avatar.url,
            moderator=moderator.mention,
            reason=reason,
            duration=(
                TimeParser.format_duration(duration) if duration is not None else "-"
            ),
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
                banned = False
                try:
                    self._stamp_bot_action(guild_id, target.id, "ban")
                    await target.ban(reason=reason)
                    banned = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Auto-ban failed for %s: %s", target, exc)
                if banned:
                    result += "\n" + await t(guild_id, "moderation.auto_ban_applied", limit=limit)
                    # Staff log card: the auto-ban reaches no moderation card
                    # otherwise (the ban is the bot's own action, so the audit
                    # gateway stays quiet too).
                    await send_log(
                        self.bot,
                        guild_id,
                        "moderation.logs.auto_ban",
                        member=target.mention,
                        member_id=str(target.id),
                        user_avatar=target.display_avatar.url,
                        moderator=moderator.mention,
                        reason=reason,
                    )
                else:
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

    #: the shared flow ---------------------------------------------------

    async def _moderate(
        self,
        ctx,
        action: str,
        targets: list[discord.Member],
        reason: str | None,
        duration: timedelta | None = None,
    ) -> None:
        """Check every target, then open the confirmation panel."""
        for target in targets:
            ok, error_key = await self._can_moderate(ctx, target, action)
            if not ok:
                await self._respond_error(ctx, error_key)
                return
        await self._confirm_and_execute(ctx, action, targets, reason, duration)

    async def _mute(
        self,
        ctx,
        member: discord.Member,
        reason: str | None,
        duration: str | None,
    ) -> None:
        """The timeout command: the duration comes from the text or the setting."""
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
        await self._moderate(ctx, "mute", [member], reason, parsed)

    #: commands ----------------------------------------------------------

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
        await run(ctx, MODERATION, lambda: self._moderate(ctx, "ban", [member], reason))

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
        await run(ctx, MODERATION, lambda: self._moderate(ctx, "kick", [member], reason))

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
        await run(ctx, MODERATION, lambda: self._mute(ctx, member, reason, duration))

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
        await run(ctx, MODERATION, lambda: self._moderate(ctx, "warn", [member], reason))

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
        await run(ctx, MODERATION, lambda: self._warnings(ctx, member))

    async def _warnings(self, ctx: discord.ApplicationContext, member: discord.Member) -> None:
        """The warning list of one member (the card carries the copy)."""
        ok, error_key = await self._can_moderate(ctx, member, "warn")
        if not ok:
            return await self._respond_error(ctx, error_key)
        t = self.bot.translator.t
        guild_id = self._ctx_guild_id(ctx)
        tz = resolve_timezone(await get_setting(self.bot.storage, guild_id, "general.timezone"))

        def _local_date(raw: str) -> str:
            try:
                return datetime.fromisoformat(raw).astimezone(tz).strftime("%Y-%m-%d")
            except ValueError:
                return "-"

        entries = await self.warnings.get_warnings(guild_id, member.id)
        lines = [
            f"**#{index}:** "
            + await t(
                guild_id,
                "warnings.entry",
                reason=entry.get("reason") or await t(guild_id, "moderation.no_reason"),
                moderator=f"<@{entry.get('moderator_id', 0) or 0}>",
                date=_local_date(entry.get("timestamp", "")),
            )
            for index, entry in enumerate(entries, start=1)
        ]
        title = await t(guild_id, "warnings.title", member=member.mention)
        body = (
            "\n\n".join(lines)
            if entries
            else await t(guild_id, "warnings.none")
        )
        from rosemary.core.card_service import render_card_message, simple_payload

        # The state chooses the color token of the card (the document writes
        # the alternatives): a clean member reads green, a warned one the
        # moderation tone. The guild's theme owns what the token means.
        state_style = "success" if not entries else "moderation_warnings"
        state_color = theme_for(self.bot, guild_id).style(state_style).color
        payload, _allowed = await render_card_message(
            self.bot,
            guild_id,
            "moderation.warnings",
            {
                "user": member.mention,
                "user_name": member.display_name,
                "user_avatar": (
                    member.display_avatar.url if member.display_avatar else ""
                ),
                "has_warnings": bool(entries),
                "state_color": state_color,
                "title": title,
                "body": body,
                "timestamp": discord.utils.format_dt(discord.utils.utcnow(), style="R"),
            },
        )
        if payload is None:
            payload = simple_payload(
                self.bot,
                guild_id,
                "moderation.warnings",
                title=title,
                body=body,
                color=state_color,
                thumbnail=member.display_avatar.url if member.display_avatar else None,
            )
        await ctx.respond(**payload.message_kwargs(), ephemeral=True)

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
        await run(ctx, MODERATION, lambda: self._unwarn(ctx, member, index))

    async def _unwarn(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Member,
        index: int | None,
    ) -> None:
        """Remove one warning, or clear all of them (behind a confirmation)."""
        ok, error_key = await self._can_moderate(ctx, member, "warn")
        if not ok:
            return await self._respond_error(ctx, error_key)

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
            return await ctx.respond(**await view.message_kwargs(), ephemeral=True)

        removed = await self.warnings.remove_warning(self._ctx_guild_id(ctx), member.id, index - 1)
        if not removed:
            return await self._respond_error(ctx, "unwarn.invalid_index")
        return await self._respond_success(
            ctx, "unwarn.removed", member=member.mention, index=index
        )

    #: prefix commands ---------------------------------------------------

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

    @commands.command(name="warnings")
    async def warnings_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        """Prefix alias: !warnings @member"""
        # ``self.warnings`` is the WarningsStore instance attribute, so the
        # slash command of the same name must come from the class.
        await type(self).warnings(PrefixContext(ctx), member)

    @commands.command(name="unwarn")
    async def unwarn_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member,
        index: int | None = None,
    ) -> None:
        """Prefix alias: !unwarn @member [index]"""
        await self.unwarn(PrefixContext(ctx), member, index)

    async def _prefix_action(
        self,
        ctx: commands.Context,
        action: str,
        members: list[discord.Member],
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        """Shared flow for prefix commands: validate, then confirm/execute."""
        await run(
            PrefixContext(ctx),
            MODERATION,
            lambda: self._prefix_flow(ctx, action, members, reason, duration),
        )

    async def _prefix_flow(
        self,
        ctx: commands.Context,
        action: str,
        members: list[discord.Member],
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        """Validate every target (the blocked ones answer together), then act."""
        t = self.bot.translator.t
        targets = list(dict.fromkeys(members))
        if not targets:
            return await self._respond(
                ctx,
                title=await t(self._ctx_guild_id(ctx), "moderation.error_no_members"),
                color="danger",
                icon="error",
            )
        parsed = None
        if action == "mute":
            aliases = await localized_aliases(self.bot.translator, self._ctx_guild_id(ctx))
            if duration:
                parsed = TimeParser.parse(duration, aliases=aliases)
                if parsed is None:
                    return await self._respond(
                        ctx,
                        title=await t(self._ctx_guild_id(ctx), "mute.invalid_duration"),
                        color="danger",
                        icon="clock",
                    )
            else:
                # Same default as /mute: the moderation.mute_seconds setting.
                seconds = await get_setting(
                    self.bot.storage, self._ctx_guild_id(ctx), "moderation.mute_seconds"
                )
                parsed = timedelta(seconds=seconds)
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
                ctx, title="\n".join(blocked), color="danger", icon="error"
            )
        await self._confirm_and_execute(ctx, action, valid, reason, parsed)


def _has_permissions(permissions: discord.Permissions, names: tuple[str, ...]) -> bool:
    return all(getattr(permissions, name, False) for name in names)

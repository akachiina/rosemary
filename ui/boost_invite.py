"""Persistent DM buttons for boost-role invites.

The invite DM is plain text (see :mod:`rosemary.ui.boost_dm`); the accept and
decline buttons travel on their own buttons-only Components V2 message, and
the role-preview card is yet another message (``send_preview``). Discord
forbids combining top-level ``content`` with Components V2, so this view must
never be sent with ``content=``; outcome text is rendered as a TextDisplay
inside the view instead.

Each invite gets unique custom_ids ``boost_invite_accept:{invite_id}`` /
``boost_invite_decline:{invite_id}`` so the interaction store never sees two
components sharing an id. Views have no timeout and are re-registered on startup
via ``bot.add_view`` so buttons keep working after a restart; the accept/decline
logic re-reads the invite from the store and re-validates it (expiry, invitee
identity, role still existing, boost system enabled).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

import discord

from rosemary.core.boost_roles import BoostRoleStore, parse_datetime
from rosemary.core.cards import text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.ui import boost_dm

log = logging.getLogger(__name__)

#: Suffix used by the cog to rebuild views after a restart.
ACCEPT_PREFIX = "boost_invite_accept"
DECLINE_PREFIX = "boost_invite_decline"


def invite_id_from_custom_id(custom_id: str, prefix: str) -> str | None:
    """Extract the invite id from a button custom_id."""
    if not custom_id.startswith(f"{prefix}:"):
        return None
    return custom_id.split(":", 1)[1]


class BoostInviteView(discord.ui.DesignerView):
    """Accept/decline buttons for one boost-role invite, sent via DM.

    The view is a buttons-only ``DesignerView`` message (the invite text is a
    separate plain message, so this view is never sent with ``content=``). On
    accept/decline/error the message is replaced with an outcome
    :class:`~discord.ui.TextDisplay` above the disabled action row. The view
    has no timeout and is re-registered on startup via ``bot.add_view`` so the
    buttons keep working after a restart; the accept/decline logic re-reads the
    invite from the store and re-validates it (expiry, invitee identity, role
    still existing, boost system enabled).
    """

    def __init__(
        self,
        bot,
        *,
        guild_id: int,
        invite_id: str,
        accept_label: str,
        decline_label: str,
    ) -> None:
        super().__init__(timeout=None, store=False)
        self.bot = bot
        self.guild_id = guild_id
        self.invite_id = invite_id
        self.store = BoostRoleStore(bot.storage.data_dir)

        accept = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=accept_label,
            custom_id=f"{ACCEPT_PREFIX}:{invite_id}",
        )
        accept.callback = self._accept
        decline = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label=decline_label,
            custom_id=f"{DECLINE_PREFIX}:{invite_id}",
        )
        decline.callback = self._decline
        self.accept_button = accept
        self.decline_button = decline
        self.add_item(discord.ui.ActionRow(accept, decline))

    # -- helpers ------------------------------------------------------------

    async def _t(self, key: str, **variables) -> str:
        return await self.bot.translator.t(self.guild_id, key, **variables)

    async def _edit(self, interaction: discord.Interaction, content: str) -> None:
        """Replace the DM with an outcome TextDisplay and disable both buttons."""
        self.accept_button.disabled = True
        self.decline_button.disabled = True
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(content))
        self.add_item(discord.ui.ActionRow(self.accept_button, self.decline_button))
        await interaction.response.edit_message(view=self)

    async def _error(self, interaction: discord.Interaction, key: str, **variables) -> None:
        body = await self._t(key, **variables)
        text = await self._t("boost.dm.error", body=body)
        await self._edit(interaction, text)

    async def _log(self, key: str, *, color: str, **variables) -> None:
        description_key = f"boost.logs.{key}.description"
        await send_channel_log(
            self.bot,
            self.guild_id,
            await self._t(f"boost.logs.{key}.title"),
            await text_or(
                self.bot,
                self.guild_id,
                description_key,
                await self._t(description_key, **variables),
                **variables,
            ),
            color=color,
        )

    # -- handlers -----------------------------------------------------------

    async def _accept(self, interaction: discord.Interaction) -> None:
        invite = await self.store.get_invite(self.guild_id, self.invite_id)
        if invite is None:
            return await self._error(interaction, "boost.errors.invite_not_found")
        if interaction.user.id != invite.get("invitee_id"):
            return await self._error(interaction, "boost.errors.invite_not_for_you")
        expires_at = parse_datetime(invite.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(UTC):
            await self.store.remove_invite(self.guild_id, self.invite_id)
            return await self._error(interaction, "boost.errors.invite_expired")
        enabled = await get_setting(self.bot.storage, self.guild_id, "boost.enabled")
        if not enabled:
            return await self._error(interaction, "boost.error_disabled")
        guild = self.bot.get_guild(self.guild_id)
        role = guild.get_role(invite["role_id"]) if guild else None
        member = guild.get_member(invite["invitee_id"]) if guild else None
        if guild is None or role is None or member is None:
            await self.store.remove_invite(self.guild_id, self.invite_id)
            return await self._error(interaction, "boost.errors.invite_invalid_old")
        try:
            await member.add_roles(role, reason="Boost role invite accepted")
        except discord.Forbidden:
            return await self._error(interaction, "boost.errors.forbidden_add_role")
        await self.store.remove_invite(self.guild_id, self.invite_id)
        member_mention = f"<@{invite.get('invitee_id')}>"
        inviter_mention = f"<@{invite.get('inviter_id', 0)}>"
        owner = guild.get_member(invite.get("inviter_id"))
        if owner is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await boost_dm.send(
                    self.bot,
                    owner,
                    self.guild_id,
                    "boost.dm.owner_accepted",
                    member_mention=member_mention,
                    role_name=role.name,
                )
        await self._log(
            "invite_accepted",
            color="success",
            role=role.mention,
            member=member_mention,
            inviter=inviter_mention,
        )
        await self._edit(
            interaction,
            await self._t("boost.dm.accepted", role_name=role.name),
        )

    async def _decline(self, interaction: discord.Interaction) -> None:
        invite = await self.store.get_invite(self.guild_id, self.invite_id)
        if invite is None:
            return await self._error(interaction, "boost.errors.invite_not_found")
        if interaction.user.id != invite.get("invitee_id"):
            return await self._error(interaction, "boost.errors.invite_not_for_you")
        await self.store.remove_invite(self.guild_id, self.invite_id)
        guild = self.bot.get_guild(self.guild_id)
        role = guild.get_role(invite["role_id"]) if guild else None
        member_mention = f"<@{invite.get('invitee_id')}>"
        inviter_mention = f"<@{invite.get('inviter_id', 0)}>"
        if role is not None:
            owner = guild.get_member(invite.get("inviter_id")) if guild else None
            if owner is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await boost_dm.send(
                        self.bot,
                        owner,
                        self.guild_id,
                        "boost.dm.owner_declined",
                        member_mention=member_mention,
                        role_name=role.name,
                    )
            await self._log(
                "invite_declined",
                color="warning",
                role=role.mention,
                member=member_mention,
                inviter=inviter_mention,
            )
        role_name = role.name if role else "-"
        await self._edit(
            interaction,
            await self._t("boost.dm.declined", role_name=role_name),
        )

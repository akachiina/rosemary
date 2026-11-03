"""Anonymous confessions with optional staff review and threaded replies.

Members send confessions through ``/confissão enviar`` (or the write button
on any public card); the post is anonymous everywhere except this guild's
``confessions.json``. With ``confessions.review_required`` the confession
waits in the review channel (Approve / Deny / Ban, deny and ban through an
optional-reason modal); otherwise it posts straight to the public channel.

Approved posts carry a random stable color and two buttons (write another,
reply anonymously); replies nest inside a lazily created thread as numbered
``#N-M`` cards, each with its own reply button. The author is DM'd on
approval, denial and every reply; staff sees authors only with
``confessions.reveal_author`` on.

The public post is a plain Discord card: the alias name is the card's title
(in bold) with the generated identicon beside it, exactly like the audit
logs put the member's avatar next to the title. The confession text keeps
its normal weight and the numbered title ("Confissão (#60)") moves to the
footer. The reply written by the confession's own author carries the ``OP``
emoji (``confessions.mark_op``), an application emoji shipped in
``assets/emojis/`` and referenced as ``{emojis/op_emoji}``.

Note: this file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations
would break option parsing at runtime (same reason as ``cogs/info.py``).
"""

import asyncio
import contextlib
import io
import logging
import re
import secrets

import discord
from discord.ext import commands

from rosemary.core.confessions import ConfessionStore
from rosemary.core.debug import send_log
from rosemary.core.localized_group import LocalizedSlashCommandGroup
from rosemary.core.pseudonym import PSEUDONYM_COLORS, Pseudonym, make_pseudonym, new_seed
from rosemary.core.settings import get_setting
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

CONFESS_MAX_LENGTH = 2000
CONFESS_IMAGE_MAX_BYTES = 8 * 1024 * 1024

_NEW_ID = "confess_new"
_REPLY_ID = "confess_reply"
_APPROVE_ID = "confess_approve"
_DENY_ID = "confess_deny"
_BAN_ID = "confess_ban"

#: The asset that marks the confession's own author (an application emoji
#: shipped in ``assets/emojis/``): ``{emojis/op_emoji}``.
OP_EMOJI_ASSET = "emojis/op_emoji"

#: Stable pastel accents, one drawn per confession at post time and kept in
#: the store so repaints never reshuffle the color. The same palette gives
#: every pseudonymous participant its name color.
_CONFESSION_COLORS = PSEUDONYM_COLORS


def _safe_filename(name: str) -> str:
    """Attachment-safe filename (``attachment://`` URLs reject spaces)."""
    clean = re.sub(r"[^\w.\-]+", "_", (name or "").strip()).strip("._")
    return (clean or "confession.png")[:80]


def _identicon_filename(seed: str) -> str:
    """The filename of the generated identicon of an alias seed."""
    return f"anonimo_{seed}.png"


def _image_url_from(attachments) -> str:
    """First image attachment's CDN URL (empty when none)."""
    for attachment in attachments or []:
        if (getattr(attachment, "content_type", "") or "").startswith("image/"):
            return attachment.url
    return ""


def _image_url_for(attachments, filename: str) -> str:
    """The CDN URL of one named image attachment (empty when absent).

    A confession message carries the identicon and the user's own picture:
    picking by name keeps the store pointing at the real picture instead of
    the identicon.
    """
    for attachment in attachments or []:
        if getattr(attachment, "filename", "") == filename:
            return getattr(attachment, "url", "") or ""
    return ""


def _is_staff(guild: discord.Guild, user_id: int) -> bool:
    """Whether a user may moderate confessions (manage messages)."""
    member = guild.get_member(user_id)
    return bool(
        member is not None and member.guild_permissions.manage_messages
    )


async def default_confession_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default public card (``confessions.card``).

    The alias name is the card's title: in bold, with the ``OP`` emoji
    beside it when the author is the confession's own, and the generated
    identicon as the section's thumbnail (the same frame the audit logs
    use for the member's avatar). The confession text keeps its normal
    weight, the picture rides the self-dropping gallery and the numbered
    title ("Confissão (#60)") closes the card as the small (``-#``)
    footer, the standard every other card uses.

    Deliberately no real avatar anywhere: the post is anonymous and the
    author's avatar image would identify them.
    """
    content: list[dict] = []
    if str(variables.get("alias_name") or "").strip():
        content.append({"type": "text", "body": "**{alias_name}**{op_emoji}"})
    if str(variables.get("body") or "").strip():
        content.append({"type": "text", "body": "{body}"})
    children: list[dict] = []
    avatar = str(variables.get("avatar_url") or "").strip()
    if avatar and content:
        children.append(
            {
                "type": "section",
                "accessory": {"type": "thumbnail", "url": "{avatar_url}"},
                "children": content,
            }
        )
    else:
        children.extend(content)
    image_url = str(variables.get("image_url") or "").strip()
    if image_url:
        children.append({"type": "gallery", "urls": [image_url]})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {title}"})
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": variables.get("color_hex") or "info",
                "children": children,
            }
        ],
    }


async def default_confession_review_document(
    bot, guild_id: int, **variables
) -> dict:
    """Catalog-default review card (``confessions.review``).

    Same body as the public card plus the ``author_line`` block, which the
    send site leaves empty unless ``reveal_author`` is on (an empty line
    renders nothing). No avatar thumbnail either: with reveal off, the
    author's avatar image would unmask them to staff. Buttons ride as
    code, like every interactive row. The review stays plain text on
    purpose: the staff must read, copy and search the confession. It
    closes with the bot's standard small (``-#``) timestamp footer.
    """
    children: list[dict] = [
        {"type": "text", "body": "# {title}"},
        {"type": "text", "body": "{body}"},
        {"type": "gallery", "urls": ["{image_url}"]},
    ]
    if variables.get("author_line"):
        children.append({"type": "text", "body": "{author_line}"})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {timestamp}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "info", "children": children}],
    }


async def default_confession_response_document(
    bot, guild_id: int, **variables) -> dict:
    """Catalog-default reply card (``confessions.response``).

    The title is the alias name (bold, with the ``OP`` emoji when the
    reply comes from the confession's own author) and the identicon is the
    section's thumbnail, exactly like the confession card. The footer
    follows the bot's standard: a divider and small (``-#``) lines with
    the reference to the parent comment and the numbered title. The
    parent's own text never rides the card (the reference links to it) and
    the ``OP`` emoji alone marks the confession's author.
    """
    content: list[dict] = []
    if str(variables.get("alias_name") or "").strip():
        content.append({"type": "text", "body": "**{alias_name}**{op_emoji}"})
    if str(variables.get("body") or "").strip():
        content.append({"type": "text", "body": "{body}"})
    children: list[dict] = []
    avatar = str(variables.get("avatar_url") or "").strip()
    if avatar and content:
        children.append(
            {
                "type": "section",
                "accessory": {"type": "thumbnail", "url": "{avatar_url}"},
                "children": content,
            }
        )
    else:
        children.extend(content)
    image_url = str(variables.get("image_url") or "").strip()
    if image_url:
        children.append({"type": "gallery", "urls": [image_url]})
    children.append({"type": "divider"})
    if str(variables.get("parent_line") or "").strip():
        children.append({"type": "text", "body": "-# {parent_line}"})
    children.append({"type": "text", "body": "-# {title}"})
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": variables.get("color_hex") or "info",
                "children": children,
            }
        ],
    }


async def _confession_dm_shell(
    bot, guild_id: int, *, body_key: str, color: str, variables: dict,
    jump_url: str, reason: str = "",
) -> dict:
    """One DM document: literal body template, optional motive line, link row.

    The body comes from ``raw()`` (``t()`` would log a formatting warning
    without variables); the reason line resolves only when non-empty.
    """
    raw = bot.translator.raw
    body = await raw(guild_id, body_key)
    jump_label = await bot.translator.t(guild_id, "confess.dm.jump")
    children: list[dict] = [{"type": "text", "body": body}]
    if reason:
        reason_line = await bot.translator.t(
            guild_id, "confess.dm.reason", reason=reason
        )
        children.append({"type": "text", "body": reason_line})
    if variables.get("message_link"):
        children.append(
            {
                "type": "row",
                "buttons": [{"label": jump_label, "url": "{message_link}"}],
            }
        )
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": color, "children": children}],
    }


async def default_confession_approved_dm(bot, guild_id: int, **variables) -> dict:
    """Catalog-default approval DM (``confessions.dm.approved``)."""
    return await _confession_dm_shell(
        bot, guild_id, body_key="confess.dm.approved", color="success",
        variables=variables, jump_url=variables.get("message_link") or "",
    )


async def default_confession_rejected_dm(bot, guild_id: int, **variables) -> dict:
    """Catalog-default denial DM (``confessions.dm.rejected``)."""
    return await _confession_dm_shell(
        bot, guild_id, body_key="confess.dm.rejected", color="danger",
        variables=variables, jump_url=variables.get("message_link") or "",
        reason=(variables.get("reason") or "").strip(),
    )


async def default_confession_response_dm(bot, guild_id: int, **variables) -> dict:
    """Catalog-default reply notice DM (``confessions.dm.response``)."""
    return await _confession_dm_shell(
        bot, guild_id, body_key="confess.dm.response", color="info",
        variables=variables, jump_url=variables.get("message_link") or "",
    )


def _register_default_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("confessions.card", default_confession_document)
    set_default_builder("confessions.review", default_confession_review_document)
    set_default_builder("confessions.response", default_confession_response_document)
    set_default_builder("confessions.dm.approved", default_confession_approved_dm)
    set_default_builder("confessions.dm.rejected", default_confession_rejected_dm)
    set_default_builder("confessions.dm.response", default_confession_response_dm)


_register_default_builders()


class ConfessionView(MenuView):
    """Public and reply cards: write another + reply (fixed ids)."""

    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.register(_NEW_ID, self._new)
        self.register(_REPLY_ID, self._reply)

    def _emoji(self, name: str, fallback: str) -> str | None:
        theme = getattr(self.bot, "theme", None)
        value = theme.emoji(name) if theme is not None else ""
        return value or fallback or None

    async def public_row(self) -> discord.ui.ActionRow:
        """Write + reply buttons for public confession cards."""
        return discord.ui.ActionRow(
            self.make_button(
                custom_id=_NEW_ID,
                label=await self.bot.translator.t(self.guild_id, "confess.buttons.new"),
                emoji=self._emoji("pencil", "✏️"),
                style=discord.ButtonStyle.primary,
            ),
            self.make_button(
                custom_id=_REPLY_ID,
                label=await self.bot.translator.t(
                    self.guild_id, "confess.buttons.reply"
                ),
                emoji=self._emoji("envelope", "✉️"),
                style=discord.ButtonStyle.secondary,
            ),
        )

    async def reply_row(self) -> discord.ui.ActionRow:
        """Reply-only row for reply cards in the thread."""
        return discord.ui.ActionRow(
            self.make_button(
                custom_id=_REPLY_ID,
                label=await self.bot.translator.t(
                    self.guild_id, "confess.buttons.reply"
                ),
                emoji=self._emoji("envelope", "✉️"),
                style=discord.ButtonStyle.secondary,
            ),
        )

    async def build_items(self) -> list[discord.ui.ViewItem]:
        return [await self.public_row()]

    async def _new(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        cog = self.bot.get_cog("ConfessionsCog")
        t = self.bot.translator.t
        if cog is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        if not await get_setting(self.bot.storage, guild.id, "confessions.enabled"):
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_disabled"), ephemeral=True
            )
        if await cog.store.is_banned(guild.id, interaction.user.id):
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        if await cog._public_channel(guild) is None:
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_no_channel"), ephemeral=True
            )
        try:
            await interaction.response.send_modal(
                await cog.build_confess_modal(guild.id)
            )
        except discord.HTTPException as exc:
            log.warning("Confession modal failed in %s: %s", guild.id, exc)

    async def _reply(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        message = getattr(interaction, "message", None)
        if guild is None or message is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        cog = self.bot.get_cog("ConfessionsCog")
        t = self.bot.translator.t
        if cog is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        if not await get_setting(self.bot.storage, guild.id, "confessions.enabled"):
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_disabled"), ephemeral=True
            )
        if await cog.store.is_banned(guild.id, interaction.user.id):
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        resolved = await cog.resolve_thread_context(guild.id, message.id)
        if resolved is None:
            return await interaction.response.send_message(
                await t(guild.id, "confess.error_gone"), ephemeral=True
            )
        confession_id, parent_id = resolved
        cog._modal_context[interaction.user.id] = ("reply", confession_id, parent_id)
        try:
            await interaction.response.send_modal(
                await cog.build_reply_modal(guild.id)
            )
        except discord.HTTPException as exc:
            log.warning("Confession reply modal failed in %s: %s", guild.id, exc)


class ConfessionReviewView(MenuView):
    """Review cards: approve / deny / ban (staff only, checked on click)."""

    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.register(_APPROVE_ID, self._approve)
        self.register(_DENY_ID, self._deny)
        self.register(_BAN_ID, self._ban)

    def _emoji(self, name: str, fallback: str) -> str | None:
        theme = getattr(self.bot, "theme", None)
        value = theme.emoji(name) if theme is not None else ""
        return value or fallback or None

    async def review_row(self) -> discord.ui.ActionRow:
        """The staff decision row for review cards."""
        return discord.ui.ActionRow(
            self.make_button(
                custom_id=_APPROVE_ID,
                label=await self.bot.translator.t(
                    self.guild_id, "confess.buttons.approve"
                ),
                emoji=self._emoji("check", "✅"),
                style=discord.ButtonStyle.success,
            ),
            self.make_button(
                custom_id=_DENY_ID,
                label=await self.bot.translator.t(
                    self.guild_id, "confess.buttons.deny"
                ),
                emoji=self._emoji("error", "❌"),
                style=discord.ButtonStyle.danger,
            ),
            self.make_button(
                custom_id=_BAN_ID,
                label=await self.bot.translator.t(
                    self.guild_id, "confess.buttons.ban"
                ),
                emoji=self._emoji("ban", "🚫"),
                style=discord.ButtonStyle.secondary,
            ),
        )

    async def build_items(self) -> list[discord.ui.ViewItem]:
        return [await self.review_row()]

    async def _context(
        self, interaction: discord.Interaction
    ):
        """Staff-guarded review context: ``(cog, guild, entry)`` or ``None``."""
        guild = interaction.guild
        message = getattr(interaction, "message", None)
        if guild is None or message is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return None
        cog = self.bot.get_cog("ConfessionsCog")
        t = self.bot.translator.t
        if cog is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return None
        if not _is_staff(guild, interaction.user.id):
            await interaction.response.send_message(
                await t(guild.id, "confess.error_staff_only"), ephemeral=True
            )
            return None
        entry = await cog.store.get(guild.id, message.id)
        if entry is None or entry.get("status") != "pending":
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await t(guild.id, "confess.error_gone"), ephemeral=True
                )
            return None
        return cog, guild, entry

    async def _approve(self, interaction: discord.Interaction) -> None:
        resolved = await self._context(interaction)
        if resolved is None:
            return
        cog, guild, _entry = resolved
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            outcome, _public = await cog.approve_confession(
                guild, interaction.message.id, interaction.user
            )
        except Exception as exc:
            log.error("Confession approve failed: %s", exc)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    await self.bot.translator.t(guild.id, "general.unknown_error"),
                    ephemeral=True,
                )
            return
        t = self.bot.translator.t
        if outcome != "ok":
            key = (
                "confess.error_no_channel"
                if outcome == "no_channel"
                else "confess.error_gone"
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    await t(guild.id, key), ephemeral=True
                )
            return
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(
                await t(guild.id, "confess.review_approved"), ephemeral=True
            )

    async def _reason_modal_for(
        self, interaction: discord.Interaction, kind: str
    ) -> None:
        """Open the optional-reason modal for deny/ban (staff already checked)."""
        resolved = await self._context(interaction)
        if resolved is None:
            return
        cog, guild, _entry = resolved
        cog._modal_context[interaction.user.id] = (kind, interaction.message.id, 0)
        try:
            await interaction.response.send_modal(
                await cog.build_reason_modal(guild.id)
            )
        except discord.HTTPException as exc:
            log.warning("Confession reason modal failed in %s: %s", guild.id, exc)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await self._reason_modal_for(interaction, "deny")

    async def _ban(self, interaction: discord.Interaction) -> None:
        await self._reason_modal_for(interaction, "ban")


class ConfessionsCog(commands.Cog):
    """Anonymous confessions with staff review and threaded replies."""

    #: Single-cog group: ``/confissão enviar`` opens the intake modal.
    confess_group = LocalizedSlashCommandGroup(
        "confess",
        description="Send anonymous confessions",
        contexts={discord.InteractionContextType.guild},
    )

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = ConfessionStore(bot.storage.data_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        #: Button-to-modal context (user id -> payload): component clicks and
        #: modal submits are separate interactions, so the resolve context
        #: travels here (consumed on submit; a second click overwrites).
        self._modal_context: dict[int, tuple] = {}
        self._started = False

    def _lock_for(self, confession_id: int) -> asyncio.Lock:
        if confession_id not in self._locks:
            self._locks[confession_id] = asyncio.Lock()
        return self._locks[confession_id]

    async def start(self) -> None:
        """Register the two persistent dispatchers once per process."""
        if self._started:
            return
        self._started = True
        # py-cord's ViewStore indexes only walk_children() results: an empty
        # dispatcher view registers nothing and every click dies unanswered.
        public = ConfessionView(self.bot, 0)
        public.add_item(await public.public_row())
        self.bot.add_view(public)
        review = ConfessionReviewView(self.bot, 0)
        review.add_item(await review.review_row())
        self.bot.add_view(review)

    #: helpers ---------------------------------------------------------------

    async def _public_channel(self, guild: discord.Guild):
        channel_id = await get_setting(
            self.bot.storage, guild.id, "confessions.public_channel"
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _review_channel(self, guild: discord.Guild):
        channel_id = await get_setting(
            self.bot.storage, guild.id, "confessions.review_channel"
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _enabled(self, guild_id: int) -> bool:
        return bool(await get_setting(self.bot.storage, guild_id, "confessions.enabled"))

    async def _review_required(self, guild_id: int) -> bool:
        return bool(
            await get_setting(self.bot.storage, guild_id, "confessions.review_required")
        )

    async def _mark_op(self, guild_id: int) -> bool:
        """Whether the ``OP`` emoji marks the public posts."""
        return bool(await get_setting(self.bot.storage, guild_id, "confessions.mark_op"))

    async def _op_emoji(self, guild_id: int) -> str:
        """The ``OP`` emoji mention (empty when the marker is off).

        The emoji is a real application emoji shipped in ``assets/emojis/``
        and uploaded at startup, so it renders natively in the card.
        """
        if not await self._mark_op(guild_id):
            return ""
        assets = getattr(self.bot, "assets", None)
        if assets is None:
            return ""
        return assets.mention(OP_EMOJI_ASSET)

    async def _alias_name(self, guild_id: int, seed: str) -> str:
        """The localized pseudonym of a seed ("Anonymous K7Q")."""
        return await self.bot.translator.t(guild_id, "confess.alias", seed=seed)

    async def _identity(
        self, guild_id: int, number: int, user_id: int
    ) -> Pseudonym:
        """The pseudonymous identity of a user inside one confession thread.

        The seed is stored per confession number: the same participant keeps
        the same name and avatar for the whole thread (and gets a fresh one
        in the next confession).
        """
        seed = await self.store.assign_alias(guild_id, number, user_id, new_seed())
        return make_pseudonym(seed)

    def _identicon_file(self, identity: Pseudonym) -> discord.File | None:
        """The generated identicon of an alias (uploaded with the message).

        ``None`` when the image could not be generated: the card then shows
        the alias name without the thumbnail.
        """
        if not identity.avatar:
            return None
        filename = _identicon_filename(identity.seed)
        return discord.File(io.BytesIO(identity.avatar), filename=filename)

    def _message_link(self, guild_id: int, channel_id, message_id: int) -> str:
        """The jump link of a message (empty when the ids are unknown)."""
        if not channel_id or not message_id:
            return ""
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def _recover_image(
        self, guild: discord.Guild, message_id: int
    ) -> tuple[bytes, str] | None:
        """The picture of a posted message (the review post's attachment).

        The approval re-posts the confession publicly: the picture travels
        with the review message, so it is fetched before that message is
        deleted.
        """
        channel = await self._review_channel(guild)
        if channel is None:
            return None
        try:
            message = await channel.get_partial_message(message_id).fetch()
        except Exception as exc:
            # A missing picture is not a failed approval: the post goes out
            # without it (the confession itself is what matters).
            log.info(
                "Confession image recovery failed for %s: %s", message_id, exc
            )
            return None
        for attachment in getattr(message, "attachments", None) or []:
            if not (getattr(attachment, "content_type", "") or "").startswith("image/"):
                continue
            try:
                data = await attachment.read()
            except Exception as exc:
                log.info("Confession image read failed: %s", exc)
                return None
            if data:
                return bytes(data), _safe_filename(attachment.filename)
        return None

    async def _title_for(self, guild_id: int, number: int) -> str:
        return await self.bot.translator.t(
            guild_id, "confess.card.title", confession_number=number
        )

    async def _dm_target(self, guild: discord.Guild, user_id: int):
        """The member (or fetched user) for a DM, ``None`` when gone."""
        if user_id <= 0:
            return None
        target = guild.get_member(user_id)
        if target is not None:
            return target
        try:
            return await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def _dm_card(
        self, guild: discord.Guild, key: str, color_token: str, variables: dict
    ):
        """Themed DM card (or code fallback) for confession notices."""
        from rosemary.core.card_service import CardPayload, render_card_message

        payload, _allowed = await render_card_message(
            self.bot, guild.id, key, variables
        )
        if payload is not None:
            return payload
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        t = self.bot.translator.t
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color(color_token),
                TextDisplay(await t(guild.id, key, **variables)),
            )
        )
        if variables.get("message_link"):
            view.add_item(
                discord.ui.ActionRow(
                    discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=await t(guild.id, "confess.dm.jump"),
                        url=variables["message_link"],
                    )
                )
            )
        return CardPayload(view=view)

    async def _send_dm(self, guild: discord.Guild, user_id: int, payload) -> bool:
        """DM one user a card payload (best effort, never pings)."""
        target = await self._dm_target(guild, user_id)
        if target is None:
            return False
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(
                **payload.message_kwargs(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        return False

    async def _log(
        self,
        guild_id: int,
        key: str,
        color: str,
        user_ids: list[int] | tuple[int, ...] = (),
        **variables,
    ) -> None:
        await send_log(self.bot, guild_id, f"confessions.logs.{key}", **variables)

    #: modals ----------------------------------------------------------------
    async def build_confess_modal(self, guild_id: int):
        """The confession intake modal (text + optional image upload)."""
        from rosemary.ui.modals import make_text_file_modal

        t = self.bot.translator.t
        return make_text_file_modal(
            title=await t(guild_id, "confess.modal.title"),
            custom_id="confess_modal",
            label=await t(guild_id, "confess.modal.label"),
            placeholder=await t(guild_id, "confess.modal.placeholder"),
            max_length=CONFESS_MAX_LENGTH,
            style=discord.InputTextStyle.paragraph,
            file_label=await t(guild_id, "confess.modal.image_label"),
            file_description=await t(guild_id, "confess.modal.image_description"),
            on_submit=self._confess_submit,
        )

    async def build_reply_modal(self, guild_id: int):
        """The anonymous response modal (text + optional image upload)."""
        from rosemary.ui.modals import make_text_file_modal

        t = self.bot.translator.t
        return make_text_file_modal(
            title=await t(guild_id, "confess.reply_modal.title"),
            custom_id="confess_reply_modal",
            label=await t(guild_id, "confess.reply_modal.label"),
            placeholder=await t(guild_id, "confess.reply_modal.placeholder"),
            max_length=1000,
            style=discord.InputTextStyle.paragraph,
            file_label=await t(guild_id, "confess.reply_modal.image_label"),
            file_description=await t(
                guild_id, "confess.reply_modal.image_description"
            ),
            on_submit=self._reply_submit,
        )

    async def build_reason_modal(self, guild_id: int):
        """The optional-reason modal for deny/ban."""
        from rosemary.ui.modals import make_text_modal

        t = self.bot.translator.t
        return make_text_modal(
            title=await t(guild_id, "confess.reason_modal.title"),
            custom_id="confess_reason_modal",
            label=await t(guild_id, "confess.reason_modal.label"),
            placeholder=await t(guild_id, "confess.reason_modal.placeholder"),
            required=False,
            max_length=500,
            style=discord.InputTextStyle.paragraph,
            on_submit=self._reason_submit,
        )

    async def _confess_submit(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        attachments: list[discord.Attachment],
    ) -> None:
        """Validate intake and post straight to review or public."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.defer(ephemeral=True)
            return
        t = self.bot.translator.t
        await interaction.response.defer(ephemeral=True)
        if await self.store.is_banned(guild.id, interaction.user.id):
            return await interaction.followup.send(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        body = (raw_text or "").strip()
        if not body:
            return await interaction.followup.send(
                await t(guild.id, "confess.error_empty"), ephemeral=True
            )
        if len(body) > CONFESS_MAX_LENGTH:
            return await interaction.followup.send(
                await t(guild.id, "confess.error_too_long"), ephemeral=True
            )
        image = None
        if attachments:
            picked = attachments[0]
            if not (picked.content_type or "").startswith("image/"):
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_bad_image"), ephemeral=True
                )
            if (picked.size or 0) > CONFESS_IMAGE_MAX_BYTES:
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_image_too_large"), ephemeral=True
                )
            try:
                data = await picked.read()
            except discord.HTTPException:
                data = b""
            if not data:
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_failed"), ephemeral=True
                )
            image = (bytes(data), _safe_filename(picked.filename))
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.followup.send(
                await t(guild.id, "general.unknown_error"), ephemeral=True
            )
        try:
            outcome, _message = await self.post_confession(
                guild, member, body, image
            )
        except discord.Forbidden:
            log.warning("Confession send forbidden in %s", guild.id)
            return await interaction.followup.send(
                await t(guild.id, "confess.error_no_channel"), ephemeral=True
            )
        except (discord.HTTPException, discord.NotFound) as exc:
            log.warning("Confession send failed in %s: %s", guild.id, exc)
            return await interaction.followup.send(
                await t(guild.id, "confess.error_failed"), ephemeral=True
            )
        if outcome == "no_channel":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_no_channel"), ephemeral=True
            )
        if outcome == "no_review":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_no_review"), ephemeral=True
            )
        key = "confess.sent" if outcome == "posted" else "confess.sent_review"
        await interaction.followup.send(await t(guild.id, key), ephemeral=True)

    async def _reply_submit(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        attachments: list[discord.Attachment],
    ) -> None:
        """Validate an anonymous response and post it in the thread."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.defer(ephemeral=True)
            return
        t = self.bot.translator.t
        await interaction.response.defer(ephemeral=True)
        context = self._modal_context.pop(interaction.user.id, None)
        if context is None or context[0] != "reply":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_gone"), ephemeral=True
            )
        _kind, confession_id, parent_id = context
        if await self.store.is_banned(guild.id, interaction.user.id):
            return await interaction.followup.send(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        body = (raw_text or "").strip()
        if not body:
            return await interaction.followup.send(
                await t(guild.id, "confess.error_empty"), ephemeral=True
            )
        if len(body) > 1000:
            return await interaction.followup.send(
                await t(guild.id, "confess.error_too_long"), ephemeral=True
            )
        image = None
        if attachments:
            picked = attachments[0]
            if not (picked.content_type or "").startswith("image/"):
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_bad_image"), ephemeral=True
                )
            if (picked.size or 0) > CONFESS_IMAGE_MAX_BYTES:
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_image_too_large"), ephemeral=True
                )
            try:
                data = await picked.read()
            except discord.HTTPException:
                data = b""
            if not data:
                return await interaction.followup.send(
                    await t(guild.id, "confess.error_failed"), ephemeral=True
                )
            image = (bytes(data), _safe_filename(picked.filename))
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.followup.send(
                await t(guild.id, "general.unknown_error"), ephemeral=True
            )
        try:
            outcome, _message = await self.post_reply(
                guild, confession_id, parent_id, member, body, image
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            log.warning("Confession reply failed in %s: %s", guild.id, exc)
            return await interaction.followup.send(
                await t(guild.id, "confess.error_failed"), ephemeral=True
            )
        if outcome == "gone":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_gone"), ephemeral=True
            )
        if outcome == "banned":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        if outcome == "thread":
            return await interaction.followup.send(
                await t(guild.id, "confess.error_thread"), ephemeral=True
            )
        await interaction.followup.send(
            await t(guild.id, "confess.replied"), ephemeral=True
        )

    async def _reason_submit(
        self, interaction: discord.Interaction, raw_text: str
    ) -> None:
        """Consume a deny/ban reason modal (optional text)."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.defer(ephemeral=True)
            return
        t = self.bot.translator.t
        await interaction.response.defer(ephemeral=True)
        context = self._modal_context.pop(interaction.user.id, None)
        if context is None or context[0] not in ("deny", "ban"):
            return await interaction.followup.send(
                await t(guild.id, "confess.error_gone"), ephemeral=True
            )
        kind, review_id, _parent = context
        member = guild.get_member(interaction.user.id)
        if member is None or not _is_staff(guild, member.id):
            return await interaction.followup.send(
                await t(guild.id, "confess.error_staff_only"), ephemeral=True
            )
        reason = (raw_text or "").strip()
        try:
            if kind == "deny":
                outcome, _message = await self.deny_confession(
                    guild, review_id, member, reason
                )
                if outcome != "ok":
                    return await interaction.followup.send(
                        await t(guild.id, "confess.error_gone"), ephemeral=True
                    )
                return await interaction.followup.send(
                    await t(guild.id, "confess.review_denied"), ephemeral=True
                )
            outcome, banned_id = await self.ban_author(
                guild, review_id, member, reason
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            log.warning("Confession %s failed in %s: %s", kind, guild.id, exc)
            return await interaction.followup.send(
                await t(guild.id, "confess.error_failed"), ephemeral=True
            )
        if outcome != "ok" or not banned_id:
            return await interaction.followup.send(
                await t(guild.id, "confess.error_gone"), ephemeral=True
            )
        await interaction.followup.send(
            await t(guild.id, "confess.banned_confirm", user=f"<@{banned_id}>"),
            ephemeral=True,
        )

    #: flows -----------------------------------------------------------------

    async def _review_variables(
        self, guild: discord.Guild, entry: dict, *, timestamp: str
    ) -> dict:
        reveal = await get_setting(self.bot.storage, guild.id, "confessions.reveal_author")
        author_id = int(entry.get("author_id") or 0)
        author_line = ""
        if reveal and author_id:
            author_line = await self.bot.translator.t(
                guild.id, "confess.review.author", user=f"<@{author_id}>"
            )
        return {
            "confession_number": entry.get("number", 0),
            "title": await self.bot.translator.t(
                guild.id,
                "confess.review.title",
                confession_number=entry.get("number", 0),
            ),
            "body": entry.get("body") or "",
            "image_url": entry.get("image_url") or "",
            "author_line": author_line,
            "timestamp": timestamp,
        }

    async def _public_variables(
        self, guild: discord.Guild, entry: dict, *, timestamp: str
    ) -> dict:
        seed = str(entry.get("alias") or "")
        alias_name = await self._alias_name(guild.id, seed) if seed else ""
        avatar_url = f"attachment://{_identicon_filename(seed)}" if seed else ""
        return {
            "confession_number": entry.get("number", 0),
            "title": await self.bot.translator.t(
                guild.id, "confess.card.title", confession_number=entry.get("number", 0)
            ),
            "body": entry.get("body") or "",
            "image_url": entry.get("image_url") or "",
            "color_hex": entry.get("color_hex") or "",
            "timestamp": timestamp,
            "alias_name": alias_name,
            "avatar_url": avatar_url,
            "op_emoji": str(entry.get("op_emoji") or ""),
        }

    async def _render_review(
        self, guild: discord.Guild, entry: dict
    ):
        """Themed review card (or code fallback) plus its mention policy."""
        from rosemary.core.card_service import CardPayload, render_card_message

        variables = await self._review_variables(
            guild, entry, timestamp=discord.utils.format_dt(
                discord.utils.utcnow(), style="R"
            ),
        )
        payload, allowed = await render_card_message(
            self.bot, guild.id, "confessions.review", variables
        )
        if payload is not None:
            return payload, allowed
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(f"# {variables['title']}"),
                TextDisplay(str(variables["body"] or "")),
            )
        )
        return CardPayload(view=view), discord.AllowedMentions.none()

    async def _render_public(self, guild: discord.Guild, entry: dict):
        """Themed public card (or code fallback) plus its mention policy."""
        from rosemary.core.card_service import CardPayload, render_card_message

        variables = await self._public_variables(
            guild, entry, timestamp=discord.utils.format_dt(
                discord.utils.utcnow(), style="R"
            ),
        )
        payload, allowed = await render_card_message(
            self.bot,
            guild.id,
            "confessions.card",
            variables,
        )
        if payload is not None:
            return payload, allowed
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(f"# {variables['title']}"),
                TextDisplay(str(variables["body"] or "")),
            )
        )
        return CardPayload(view=view), discord.AllowedMentions.none()

    async def _public_view(self, guild_id: int):
        """Public card row (write + reply) with its translated labels."""
        view = ConfessionView(self.bot, guild_id)
        view.add_item(await view.public_row())
        return view

    async def _review_view(self, guild_id: int):
        """Review card row (approve / deny / ban) with translated labels."""
        view = ConfessionReviewView(self.bot, guild_id)
        view.add_item(await view.review_row())
        return view

    async def _row_into(self, payload, row):
        """Attach a code row whatever form the card took (V2 or embed)."""
        from rosemary.core.card_service import CardPayload

        if payload.embed is not None:
            view = payload.view or discord.ui.View(timeout=None)
            for item in row.children:
                view.add_item(item)
            return CardPayload(embed=payload.embed, view=view)
        payload.view.add_item(row)
        return payload

    async def post_confession(
        self,
        guild: discord.Guild,
        author: discord.Member,
        body: str,
        image: tuple[bytes, str] | None = None,
    ) -> tuple[str, discord.Message | None]:
        """Post straight to review or public. Returns ``(outcome, message)``.

        Outcomes: ``"posted"``, ``"review"``, ``"no_channel"``,
        ``"no_review"``.
        """
        channel = await self._public_channel(guild)
        if channel is None:
            return "no_channel", None
        files: list[discord.File] = []
        image_url = ""
        image_name = ""
        if image is not None:
            data, name = image
            files.append(discord.File(io.BytesIO(data), filename=name))
            image_url = f"attachment://{name}"
            image_name = name
        number = await self.store.next_number(guild.id)
        identity = await self._identity(guild.id, number, author.id)
        draft = {
            "number": number,
            "author_id": author.id,
            "channel_id": channel.id,
            "body": body,
            "user_name": author.display_name,
            "user_avatar": author.display_avatar.url,
            "image_url": image_url,
            "alias": identity.seed,
            "alias_color": identity.color,
        }
        if await self._review_required(guild.id):
            review = await self._review_channel(guild)
            if review is None:
                return "no_review", None
            await self.store.put(guild.id, 0, status="pending", **draft)
            entry = await self.store.get(guild.id, 0)
            payload, allowed = await self._render_review(guild, entry or draft)
            payload = await self._row_into(
                payload, (await self._review_view(guild.id)).children[0]
            )
            send_kwargs = payload.message_kwargs()
            if files:
                send_kwargs["files"] = files
            sent = await review.send(**send_kwargs, allowed_mentions=allowed)
            await self.store.rekey(guild.id, 0, sent.id)
            return "review", sent
        color_hex = secrets.choice(_CONFESSION_COLORS)
        entry = {
            **draft,
            "body": body,
            "color_hex": color_hex,
            "op_emoji": "",
        }
        payload, allowed = await self._render_public(guild, entry)
        payload = await self._row_into(
            payload, (await self._public_view(guild.id)).children[0]
        )
        send_kwargs = payload.message_kwargs()
        identicon = self._identicon_file(identity)
        all_files = [*([identicon] if identicon else []), *files]
        if all_files:
            send_kwargs["files"] = all_files
        sent = await channel.send(**send_kwargs, allowed_mentions=allowed)
        stored_image_url = _image_url_for(sent.attachments, image_name)
        if files and not stored_image_url:
            try:
                fresh = await channel.fetch_message(sent.id)
                stored_image_url = _image_url_for(fresh.attachments, image_name)
            except discord.HTTPException:
                stored_image_url = ""
        draft["image_url"] = stored_image_url
        await self.store.put(
            guild.id, sent.id, status="open", color_hex=color_hex, **draft,
        )
        return "posted", sent

    async def approve_confession(
        self, guild: discord.Guild, review_message_id: int, moderator: discord.Member
    ) -> tuple[str, discord.Message | None]:
        """Approve a pending confession. Returns ``(outcome, public_message)``.

        Outcomes: ``"ok"``, ``"not_found"``, ``"no_channel"``.
        """
        entry = await self.store.get(guild.id, review_message_id)
        if entry is None or entry.get("status") != "pending":
            return "not_found", None
        channel = await self._public_channel(guild)
        if channel is None:
            return "no_channel", None
        number = int(entry.get("number") or 0)
        author_id = int(entry.get("author_id") or 0)
        color_hex = secrets.choice(_CONFESSION_COLORS)
        body = entry.get("body") or ""
        # The picture lives in the review post: fetch it before that post
        # is deleted, so the public message carries it too.
        picture = await self._recover_image(guild, review_message_id)
        image_url = ""
        files: list[discord.File] = []
        if picture is not None:
            data, name = picture
            files.append(discord.File(io.BytesIO(data), filename=name))
            image_url = f"attachment://{name}"
        seed = str(entry.get("alias") or "")
        if not seed and author_id:
            identity = await self._identity(guild.id, number, author_id)
        else:
            identity = make_pseudonym(seed or new_seed())
        public_entry = {
            **entry,
            "body": body,
            "color_hex": color_hex,
            "image_url": image_url,
            "op_emoji": "",
        }
        payload, allowed = await self._render_public(guild, public_entry)
        payload = await self._row_into(
            payload, (await self._public_view(guild.id)).children[0]
        )
        send_kwargs = payload.message_kwargs()
        identicon = self._identicon_file(identity)
        all_files = [*([identicon] if identicon else []), *files]
        if all_files:
            send_kwargs["files"] = all_files
        try:
            public = await channel.send(
                **send_kwargs, allowed_mentions=allowed
            )
        except (discord.Forbidden, discord.HTTPException):
            return "no_channel", None
        stored_image_url = ""
        if picture is not None:
            stored_image_url = _image_url_for(public.attachments, picture[1])
            if not stored_image_url:
                try:
                    fresh = await channel.fetch_message(public.id)
                    stored_image_url = _image_url_for(fresh.attachments, picture[1])
                except discord.HTTPException:
                    stored_image_url = ""
        await self.store.put(
            guild.id,
            public.id,
            number=number,
            author_id=author_id,
            channel_id=channel.id,
            body=body,
            user_name=entry.get("user_name") or "",
            user_avatar=entry.get("user_avatar") or "",
            image_url=stored_image_url,
            color_hex=color_hex,
            status="open",
            alias=identity.seed,
            alias_color=identity.color,
        )
        await self.store.discard(guild.id, review_message_id)
        review_channel = await self._review_channel(guild)
        if review_channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await review_channel.get_partial_message(review_message_id).delete()
        await self._dm_card_send(
            guild,
            author_id,
            "confessions.dm.approved",
            "success",
            {
                "confession_number": number,
                "body": body,
                "message_link": public.jump_url,
            },
            "confess.dm.approved",
        )
        await self._log(
            guild.id, "approved", "success",
            [author_id],
            moderator=moderator.mention, confession_number=number,
        )
        return "ok", public

    async def deny_confession(
        self, guild: discord.Guild, review_message_id: int,
        moderator: discord.Member, reason: str,
    ) -> tuple[str, None]:
        """Deny a pending confession (DM carries the optional reason)."""
        entry = await self.store.get(guild.id, review_message_id)
        if entry is None or entry.get("status") != "pending":
            return "not_found", None
        await self.store.set_status(guild.id, review_message_id, "denied")
        await self._dm_card_send(
            guild,
            int(entry.get("author_id") or 0),
            "confessions.dm.rejected",
            "danger",
            {
                "confession_number": entry.get("number", 0),
                "body": entry.get("body") or "",
                "reason": (reason or "").strip(),
            },
            "confess.dm.rejected",
        )
        review_channel = await self._review_channel(guild)
        if review_channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await review_channel.get_partial_message(review_message_id).delete()
        reason_text = (reason or "").strip() or await self.bot.translator.t(
            guild.id, "confess.no_reason"
        )
        await self._log(
            guild.id, "denied", "danger",
            [int(entry.get("author_id") or 0)],
            moderator=moderator.mention,
            confession_number=entry.get("number", 0),
            reason=reason_text,
        )
        return "ok", None

    async def ban_author(
        self, guild: discord.Guild, review_message_id: int,
        moderator: discord.Member, reason: str,
    ) -> tuple[str, int]:
        """Ban a pending confession's author. Returns ``(outcome, user_id)``."""
        entry = await self.store.get(guild.id, review_message_id)
        if entry is None or entry.get("status") != "pending":
            return "not_found", 0
        author_id = int(entry.get("author_id") or 0)
        if author_id:
            await self.store.ban(guild.id, author_id)
        await self.store.discard(guild.id, review_message_id)
        review_channel = await self._review_channel(guild)
        if review_channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await review_channel.get_partial_message(review_message_id).delete()
        if author_id:
            t = self.bot.translator.t
            reason_text = (reason or "").strip() or await t(
                guild.id, "confess.no_reason"
            )
            text = await self.bot.translator.t(
                guild.id, "confess.dm.banned", reason=reason_text
            )
            await self._send_text_dm(guild, author_id, text)
            await self._log(
                guild.id, "banned", "danger", [author_id],
                moderator=moderator.mention, user=f"<@{author_id}>",
                reason=reason_text,
            )
        return "ok", author_id

    async def _send_text_dm(self, guild: discord.Guild, user_id: int, text: str) -> bool:
        """Plain-text DM (ban notices carry no card)."""
        target = await self._dm_target(guild, user_id)
        if target is None:
            return False
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        return False

    async def _dm_card_send(
        self, guild: discord.Guild, user_id: int, key: str,
        color_token: str, variables: dict, body_key: str,
    ) -> bool:
        """Card DM for confession notices (best effort, never pings)."""
        payload = await self._dm_card(guild.id, key, color_token, variables, body_key)
        target = await self._dm_target(guild, user_id)
        if target is None:
            return False
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(
                **payload.message_kwargs(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        return False

    async def _dm_card(
        self, guild_id: int, key: str, color_token: str, variables: dict,
        body_key: str,
    ):
        """Themed DM card (or code fallback) for confession notices."""
        from rosemary.core.card_service import CardPayload, render_card_message

        payload, _allowed = await render_card_message(
            self.bot, guild_id, key, variables
        )
        if payload is not None:
            return payload
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        t = self.bot.translator.t
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color(color_token),
                TextDisplay(await t(guild_id, body_key, **variables)),
            )
        )
        if variables.get("message_link"):
            view.add_item(
                discord.ui.ActionRow(
                    discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=await t(guild_id, "confess.dm.jump"),
                        url=variables["message_link"],
                    )
                )
            )
        return CardPayload(view=view)

    async def resolve_thread_context(
        self, guild_id: int, message_id: int
    ) -> tuple[int, int] | None:
        """``(confession_message_id, parent_reply_id)`` for a clicked card.

        Public cards resolve to themselves with no parent; reply cards to
        their confession with themselves as parent. ``None`` when unknown
        or the confession is gone/denied.
        """
        entry = await self.store.get(guild_id, message_id)
        if entry is not None:
            if entry.get("status") != "open":
                return None
            return message_id, 0
        reply = await self.store.get_reply(guild_id, message_id)
        if reply is None:
            return None
        confession = await self.store.get(guild_id, int(reply.get("confession_id") or 0))
        if confession is None or confession.get("status") != "open":
            return None
        return int(reply.get("confession_id") or 0), message_id

    async def _ensure_thread(
        self, guild: discord.Guild, confession_id: int, entry: dict
    ):
        """The confession's discussion thread (created lazily, then cached)."""
        thread_id = int(entry.get("thread_id") or 0)
        if thread_id:
            thread = guild.get_thread(thread_id)
            if thread is not None:
                return thread
        channel = guild.get_channel(int(entry.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            confession = await channel.fetch_message(confession_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        name = await self.bot.translator.t(
            guild.id, "confess.thread_name",
            confession_number=entry.get("number", 0),
        )
        try:
            thread = await confession.create_thread(
                name=name[:100], auto_archive_duration=1440,
            )
        except (discord.Forbidden, discord.HTTPException):
            return None
        await self.store.set_thread(guild.id, confession_id, thread.id)
        return thread

    async def post_reply(
        self,
        guild: discord.Guild,
        confession_id: int,
        parent_id: int,
        author: discord.Member,
        body: str,
        image: tuple[bytes, str] | None = None,
    ) -> tuple[str, discord.Message | None]:
        """Post an anonymous reply. Returns ``(outcome, message)``.

        Outcomes: ``"ok"``, ``"gone"`` (confession deleted),
        ``"banned"``, ``"thread"`` (thread unavailable). A deleted parent
        degrades to a direct reply instead of failing.
        """
        entry = await self.store.get(guild.id, confession_id)
        if entry is None or entry.get("status") != "open":
            return "gone", None
        if await self.store.is_banned(guild.id, author.id):
            return "banned", None
        async with self._lock_for(confession_id):
            thread = await self._ensure_thread(guild, confession_id, entry)
            if thread is None:
                return "thread", None
            # Re-read inside the lock: the reply counter must be fresh or
            # two quick replies would show the same number.
            fresh_entry = await self.store.get(guild.id, confession_id)
            if fresh_entry is not None:
                entry = fresh_entry
            number = int(entry.get("number") or 0)
            author_id = int(entry.get("author_id") or 0)
            parent_index = 0
            parent = None
            if parent_id:
                parent = await self.store.get_reply(guild.id, parent_id)
                if (
                    parent is not None
                    and parent.get("number") == entry.get("number")
                ):
                    parent_index = int(parent.get("index") or 0)
            index = int(entry.get("reply_count") or 0) + 1
            # The confession's own author is flagged on their own reply: the
            # OP emoji is the only public trace of who wrote the confession.
            is_op = bool(author_id) and author.id == author_id
            parent_is_op = bool(parent and parent.get("is_op"))
            identity = await self._identity(guild.id, number, author.id)
            op_emoji = await self._op_emoji(guild.id) if is_op else ""
            parent_line = ""
            parent_link = ""
            if parent_index:
                parent_ref = await self.bot.translator.t(
                    guild.id,
                    "confess.response.parent_ref",
                    confession_number=number,
                    parent_index=parent_index,
                )
                parent_link = self._message_link(
                    guild.id,
                    entry.get("thread_id") or entry.get("channel_id"),
                    parent_id,
                )
                label = f"[{parent_ref}]({parent_link})" if parent_link else parent_ref
                key = (
                    "confess.response.parent_op"
                    if parent_is_op
                    else "confess.response.parent"
                )
                parent_line = await self.bot.translator.t(
                    guild.id, key, parent_ref=label
                )
            files: list[discord.File] = []
            image_url = ""
            image_name = ""
            if image is not None:
                data, name = image
                files.append(discord.File(io.BytesIO(data), filename=name))
                image_url = f"attachment://{name}"
                image_name = name
            variables = {
                "confession_number": number,
                "reply_index": index,
                "title": await self.bot.translator.t(
                    guild.id, "confess.response.title",
                    confession_number=number,
                    reply_index=index,
                ),
                "body": body,
                "parent_line": parent_line,
                "color_hex": entry.get("color_hex") or "",
                "image_url": image_url,
                "alias_name": await self._alias_name(guild.id, identity.seed),
                "avatar_url": f"attachment://{_identicon_filename(identity.seed)}",
                "op_emoji": op_emoji,
                "parent_link": parent_link,
            }
            from rosemary.core.card_service import render_card_message

            payload, allowed = await render_card_message(
                self.bot,
                guild.id,
                "confessions.response",
                variables,
            )
            if payload is None:
                from rosemary.ui.containers import (
                    DesignerView,
                    TextDisplay,
                    designer_container,
                )

                view = DesignerView(store=False)
                view.add_item(
                    designer_container(
                        self.bot.theme.color("info"),
                        TextDisplay(f"# {variables['title']}"),
                        TextDisplay(body),
                    )
                )
                from rosemary.core.card_service import CardPayload as _Payload

                payload = _Payload(view=view)
                allowed = discord.AllowedMentions.none()
            view = ConfessionView(self.bot, guild.id)
            view.add_item(await view.reply_row())
            if payload.embed is not None:
                embed_view = payload.view or discord.ui.View(timeout=None)
                for item in view.children[0].children:
                    embed_view.add_item(item)
                from rosemary.core.card_service import CardPayload as _Payload2

                payload = _Payload2(embed=payload.embed, view=embed_view)
            else:
                payload.view.add_item(view.children[0])
            send_kwargs = payload.message_kwargs()
            identicon = self._identicon_file(identity)
            all_files = [*([identicon] if identicon else []), *files]
            if all_files:
                send_kwargs["files"] = all_files
            sent = await thread.send(
                **send_kwargs, allowed_mentions=allowed
            )
            stored_image_url = _image_url_for(sent.attachments, image_name)
            if files and not stored_image_url:
                # Same live gap as confession posts: the send response may
                # omit attachments, so fetch for the signed CDN URL.
                try:
                    fresh = await thread.fetch_message(sent.id)
                    stored_image_url = _image_url_for(fresh.attachments, image_name)
                except discord.HTTPException:
                    stored_image_url = ""
            stored = await self.store.add_reply(
                guild.id, confession_id, sent.id,
                parent_id=parent_id if parent_index else None,
                parent_index=parent_index, author_id=author.id, body=body,
                image_url=stored_image_url,
                alias=identity.seed,
                alias_color=identity.color,
                is_op=is_op,
                parent_is_op=parent_is_op,
            )
            if stored is None:
                return "gone", None
        # The author of the confession hears about every reply but their own:
        # a DM about your own comment is noise.
        if author_id and author.id != author_id:
            await self._dm_card_send(
                guild, author_id,
                "confessions.dm.response", "info",
                {
                    "confession_number": number,
                    "reply_index": index,
                    "body": body,
                    "message_link": sent.jump_url,
                },
                "confess.dm.response",
            )
        if parent_index:
            parent = await self.store.get_reply(guild.id, parent_id)
            parent_author = int(parent.get("author_id") or 0) if parent else 0
            if (
                parent_author
                and parent_author != author.id
                and parent_author != author_id
            ):
                await self._dm_card_send(
                    guild, parent_author, "confessions.dm.response", "info",
                    {
                        "confession_number": number,
                        "reply_index": index,
                        "body": body,
                        "message_link": sent.jump_url,
                    },
                    "confess.dm.response",
                )
        await self._log(
            guild.id, "replied", "info", (),
            confession_number=number, reply_index=index,
        )
        return "ok", sent

    #: commands ------------------------------------------------------------

    @confess_group.command(
        name="send",
        description="Send an anonymous confession",
    )
    async def confess_send(self, ctx: discord.ApplicationContext) -> None:
        """Open the confession intake modal."""
        guild = ctx.guild
        if guild is None:
            return
        t = self.bot.translator.t
        if not await self._enabled(guild.id):
            return await ctx.respond(
                await t(guild.id, "confess.error_disabled"), ephemeral=True
            )
        if await self.store.is_banned(guild.id, ctx.author.id):
            return await ctx.respond(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        if await self._public_channel(guild) is None:
            return await ctx.respond(
                await t(guild.id, "confess.error_no_channel"), ephemeral=True
            )
        await ctx.send_modal(await self.build_confess_modal(guild.id))

    @confess_group.command(
        name="reply",
        description="Reply anonymously to a confession",
    )
    async def confess_reply(
        self,
        ctx: discord.ApplicationContext,
        number: discord.Option(int, description="Confession number", min_value=1),
    ) -> None:
        """Open the anonymous reply modal for one confession."""
        guild = ctx.guild
        if guild is None:
            return
        t = self.bot.translator.t
        if not await self._enabled(guild.id):
            return await ctx.respond(
                await t(guild.id, "confess.error_disabled"), ephemeral=True
            )
        if await self.store.is_banned(guild.id, ctx.author.id):
            return await ctx.respond(
                await t(guild.id, "confess.error_banned"), ephemeral=True
            )
        found = await self.store.get_by_number(guild.id, number)
        if found is None:
            return await ctx.respond(
                await t(guild.id, "confess.error_not_found", confession_number=number),
                ephemeral=True,
            )
        message_id, _entry = found
        self._modal_context[ctx.author.id] = ("reply", message_id, 0)
        await ctx.send_modal(await self.build_reply_modal(guild.id))

    async def _staff_command(
        self, ctx: discord.ApplicationContext
    ) -> discord.Guild | None:
        """Shared gate for staff subcommands (enabled + manage messages)."""
        guild = ctx.guild
        if guild is None:
            return None
        t = self.bot.translator.t
        if not await self._enabled(guild.id):
            await ctx.respond(
                await t(guild.id, "confess.error_disabled"), ephemeral=True
            )
            return None
        member = guild.get_member(ctx.author.id)
        if member is None or not _is_staff(guild, member.id):
            await ctx.respond(
                await t(guild.id, "confess.error_staff_only"), ephemeral=True
            )
            return None
        return guild

    @confess_group.command(
        name="ban",
        description="[ADMIN] Ban a member from confessions",
        default_member_permissions=discord.Permissions(manage_messages=True),
    )
    async def confess_ban(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to ban"),
        reason: discord.Option(
            str, description="Reason shown to the member", required=False
        ),
    ) -> None:
        """Ban a member from confessing (staff)."""
        guild = await self._staff_command(ctx)
        if guild is None:
            return
        t = self.bot.translator.t
        newly = await self.store.ban(guild.id, member.id)
        staff = guild.get_member(ctx.author.id)
        reason_text = (reason or "").strip() or await t(
            guild.id, "confess.no_reason"
        )
        if staff is not None:
            await self._log(
                guild.id, "banned", "danger", [member.id],
                moderator=staff.mention, user=member.mention, reason=reason_text,
            )
        text = await t(guild.id, "confess.dm.banned", reason=reason_text)
        await self._send_text_dm(guild, member.id, text)
        key = "confess.banned_confirm" if newly else "confess.already_banned"
        await ctx.respond(
            await t(guild.id, key, user=member.mention), ephemeral=True
        )

    @confess_group.command(
        name="unban",
        description="[ADMIN] Unban a member from confessions",
        default_member_permissions=discord.Permissions(manage_messages=True),
    )
    async def confess_unban(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to unban"),
    ) -> None:
        """Lift a confession ban (staff)."""
        guild = await self._staff_command(ctx)
        if guild is None:
            return
        t = self.bot.translator.t
        lifted = await self.store.unban(guild.id, member.id)
        key = "confess.unbanned_confirm" if lifted else "confess.not_banned"
        await ctx.respond(
            await t(guild.id, key, user=member.mention), ephemeral=True
        )

    @confess_group.command(
        name="banned",
        description="[ADMIN] List members banned from confessions",
        default_member_permissions=discord.Permissions(manage_messages=True),
    )
    async def confess_banned(self, ctx: discord.ApplicationContext) -> None:
        """List banned authors (staff, ephemeral)."""
        guild = await self._staff_command(ctx)
        if guild is None:
            return
        t = self.bot.translator.t
        banned = await self.store.banned_list(guild.id)
        if not banned:
            return await ctx.respond(
                await t(guild.id, "confess.banned_list.empty"), ephemeral=True
            )
        lines = [
            await t(guild.id, "confess.banned_list.line", user=f"<@{user_id}>")
            for user_id in banned
        ]
        await ctx.respond(
            await t(guild.id, "confess.banned_list.title") + "\n" + "\n".join(lines),
            ephemeral=True,
        )

    #: listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        try:
            entry = await self.store.get(payload.guild_id, payload.message_id)
            if entry is not None:
                if entry.get("status") == "open":
                    thread_id = int(entry.get("thread_id") or 0)
                    if thread_id:
                        guild = self.bot.get_guild(payload.guild_id)
                        thread = (
                            guild.get_thread(thread_id) if guild is not None else None
                        )
                        if thread is not None:
                            with contextlib.suppress(
                                discord.Forbidden, discord.HTTPException
                            ):
                                await thread.delete()
                    await self.store.remove(payload.guild_id, payload.message_id)
                else:
                    await self.store.discard(payload.guild_id, payload.message_id)
                return
            if await self.store.get_reply(payload.guild_id, payload.message_id):
                await self.store.discard(payload.guild_id, payload.message_id)
        except Exception as exc:
            log.error("Confession cleanup failed for %s: %s", payload.message_id, exc)

"""Anonymous confessions with optional staff review and threaded replies.

Members send confessions through ``/confissão enviar`` (or the write button
on any public card); the post is anonymous everywhere except this guild's
``confessions.json``. With ``confessions.review_required`` the confession
waits in the review channel (Approve / Deny / Ban, deny and ban through an
optional-reason modal); otherwise it posts straight to the public channel.

Approved posts carry a random fruit alias and a random pastel accent, and
two buttons (write another, reply anonymously); replies nest inside a
lazily created thread as numbered ``#N-M`` cards, each with its own reply
button. The author is DM'd on approval and denial, and every reply (unless
the author wrote it themselves); staff sees authors only with
``confessions.reveal_author`` on.

The public post is a plain card: the alias (a fruit) is the title, the
confession text is the body, the image rides the gallery (when there is
one) and the numbered title ("Confissão (#60)") closes it. The reply
written by the confession's own author carries the ``OP`` emoji
(``confessions.mark_op``), an application emoji shipped in
``assets/emojis/`` and referenced as ``{emojis/op_emoji}``.

With ``confessions.sealed_author`` (the default, and off while
``confessions.reveal_author`` is on) the author id is encrypted to the
staff's public key before it reaches the store: the bot cannot open it,
and the member gets a claim token to answer the confession
(``/confissao responder <token>``) without the bot linking the reply to an
account. The private half lives offline; the ``confession_keys`` CLI opens a
sealed blob only for a serious case.

Note: this file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations
would break option parsing at runtime (same reason as ``cogs/info.py``).
"""

import asyncio
import contextlib
import io
import logging
import os
import re
import secrets

import discord
from discord.ext import commands

from rosemary.core.alias_identity import (
    KeyRing,
    claim_hash,
    claim_token,
    fingerprint,
    random_alias,
    random_color,
    seal,
)
from rosemary.core.confessions import ConfessionStore
from rosemary.core.debug import send_log
from rosemary.core.interactions import ack
from rosemary.core.localized_group import LocalizedSlashCommandGroup
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

#: How many claim tokens are remembered in memory (the store keeps only the
#: hash; the token is shown once to the member and lives here until the
#: process restarts, so the stateless ``/confissao responder <token>`` can
#: tell which confession the caller wrote without ever storing a user id).
_CLAIM_MEMORY = 2000

#: The asset that marks the confession's own author (an application emoji
#: shipped in ``assets/emojis/``): ``{emojis/op_emoji}``.
OP_EMOJI_ASSET = "emojis/op_emoji"


def _repo_line() -> str:
    """The repository and commit the confirmation points at (the proof)."""
    commit = os.environ.get("ROSEMARY_COMMIT", "").strip()
    if commit:
        return f"rosemary@{commit[:12]}"
    return "rosemary"


def _safe_filename(name: str) -> str:
    """Attachment-safe filename (``attachment://`` URLs reject spaces)."""
    clean = re.sub(r"[^\w.\-]+", "_", (name or "").strip()).strip("._")
    return (clean or "confession.png")[:80]


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
            await self._ack(interaction, suppress_errors=True)
            return None
        cog = self.bot.get_cog("ConfessionsCog")
        t = self.bot.translator.t
        if cog is None:
            await self._ack(interaction, suppress_errors=True)
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
        # The single ACK of every menu click (and a dead token stops here).
        if not await self._ack(interaction):
            return
        try:
            outcome, _public = await cog.approve_confession(
                guild, interaction.message.id, interaction.user
            )
        except Exception as exc:
            log.error("Confession approve failed: %s", exc)
            from rosemary.core.feedback import respond as respond_line

            text = await self.bot.translator.t(guild.id, "general.unknown_error")
            with contextlib.suppress(discord.HTTPException):
                await respond_line(interaction, text, ephemeral=True)
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
        self._keys = KeyRing(bot.storage.data_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        #: The claim tokens handed out in this process (token -> context).
        #: The store keeps only their hash: the token itself is shown once
        #: to the member and lives here until the process restarts, so the
        #: stateless ``/confissao responder <token>`` can tell which
        #: confession the caller wrote without ever storing a user id.
        self._claim_tokens: dict[str, dict] = {}
        self._claim_order: list[str] = []
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
        """Register the two persistent dispatchers and reconcile the settings.

        The boot is the safety net for the reveal/sealed pair: a guild that
        somehow has both on (an old file, a manual edit) gets the reveal
        turned off, because the reveal needs the readable id and the sealed
        mode is the privacy promise.
        """
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
        for guild in self.bot.guilds:
            if await get_setting(
                self.bot.storage, guild.id, "confessions.reveal_author"
            ) and await get_setting(
                self.bot.storage, guild.id, "confessions.sealed_author"
            ):
                from rosemary.core.settings import set_setting

                await set_setting(
                    self.bot.storage, guild.id, "confessions.sealed_author", False
                )
                log.warning(
                    "Confessions of %s had reveal and seal on: the seal was "
                    "turned off (the reveal needs the readable id)",
                    guild.id,
                )

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

    async def _alias_pool(self, guild_id: int) -> list[str]:
        """The catalog's fruit list (empty when the catalog has none)."""
        raw = await getattr(self.bot.translator, "raw", lambda *_: None)(
            guild_id, "confess.identity.aliases"
        )
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if str(item).strip()]

    async def _sealed(self, guild_id: int) -> bool:
        """Whether the guild seals the author (the reveal wins over it)."""
        if await get_setting(self.bot.storage, guild_id, "confessions.reveal_author"):
            return False
        return bool(
            await get_setting(self.bot.storage, guild_id, "confessions.sealed_author")
        )

    async def _reply_alias(
        self, guild_id: int, confession_id: int, user_id: int
    ) -> str:
        """The alias a user already used in this thread (empty when none)."""
        data = await self.store._doc(guild_id)
        for reply in data.get("replies", {}).values():
            if (
                isinstance(reply, dict)
                and reply.get("confession_id") == confession_id
                and reply.get("author_id") == user_id
                and str(reply.get("alias") or "").strip()
            ):
                return str(reply["alias"])
        return ""

    async def _seal_author(self, guild_id: int, author_id: int) -> tuple[str, str]:
        """``(sealed, key_id)`` for the author id (empty when no key exists)."""
        key = await self._keys.active(guild_id)
        if key is None or not author_id:
            return "", ""
        return seal(key.public, key.key_id, str(author_id)), key.key_id

    async def _alias_name(self, guild_id: int, alias: str) -> str:
        """The alias as the member reads it.

        A legacy seed (three characters, the old store) is translated with
        the catalog; a fruit alias (the new store) is already the name.
        """
        if not alias:
            return ""
        if len(alias) <= 4 and alias.isalnum():
            return await self.bot.translator.t(guild_id, "confess.alias", seed=alias)
        return alias

    async def _new_alias(self, guild_id: int) -> str:
        """A fresh alias from the catalog list (or a legacy seed)."""
        alias = random_alias(await self._alias_pool(guild_id))
        if alias:
            return alias
        return "".join(
            secrets.choice("ABCDEFGHJKLMNPQRTUVWXYZ2346789") for _ in range(3)
        )

    async def _identity(
        self, guild_id: int, number: int, user_id: int
    ) -> str:
        """The pseudonymous alias of a user inside one confession thread.

        Get-or-create through the store: the same participant keeps the same
        alias for the whole thread (legacy mode). The sealed mode never
        calls this: the alias is generated per entry and the claim token
        replaces the user link.
        """
        return await self.store.assign_alias(
            guild_id, number, user_id, await self._new_alias(guild_id)
        )

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
        """Validate intake and post straight to review or public.

        The submit is ACKed through :func:`ack`: a submit replayed by a
        gateway RESUME carries a token Discord already forgot (10062), and
        the confession must not post behind a dead interaction.
        """
        guild = interaction.guild
        if guild is None:
            await ack(interaction, ephemeral=True)
            return
        t = self.bot.translator.t
        if not await ack(interaction, ephemeral=True):
            return
        sealed = await self._sealed(guild.id)
        if await self.store.is_banned(
            guild.id, interaction.user.id, sealed=sealed
        ):
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
            from rosemary.core.feedback import respond as respond_line

            return await respond_line(
                interaction,
                await t(guild.id, "general.unknown_error"),
                ephemeral=True,
            )
        try:
            outcome, _message, token = await self.post_confession(
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
        await interaction.followup.send(
            await self._sent_copy(guild.id, key, token),
            ephemeral=True,
        )

    async def _sent_copy(self, guild_id: int, key: str, token: str) -> str:
        """The "confession sent" confirmation (the proof, when sealed).

        The member reads the algorithm, the fingerprint of the staff's
        public key, the claim token (shown once) and the repository/commit.
        The sealed blob is not repeated here (it is long and the member
        cannot check it against the id): the token and the public key
        fingerprint are what the member can actually verify, and the code is
        the real proof.
        """
        base = await self.bot.translator.t(guild_id, key)
        if not token:
            return base
        public = await self._active_public(guild_id)
        lines = [base]
        if public:
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "confess.sent_sealed",
                    fingerprint=fingerprint(public),
                )
            )
        lines.append(
            await self.bot.translator.t(
                guild_id, "confess.sent_token", token=f"`{token}`"
            )
        )
        lines.append(
            await self.bot.translator.t(
                guild_id, "confess.sent_repo", repo=_repo_line()
            )
        )
        return "\n".join(line for line in lines if line)

    async def _active_public(self, guild_id: int) -> str:
        key = await self._keys.active(guild_id)
        return key.public if key is not None else ""

    async def _reply_submit(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        attachments: list[discord.Attachment],
    ) -> None:
        """Validate an anonymous response and post it in the thread.

        Like the confession intake, the submit is ACKed through
        :func:`ack`: a dead token (10062) stops the reply before it posts.
        """
        guild = interaction.guild
        if guild is None:
            await ack(interaction, ephemeral=True)
            return
        t = self.bot.translator.t
        if not await ack(interaction, ephemeral=True):
            return
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
            from rosemary.core.feedback import respond as respond_line

            return await respond_line(
                interaction,
                await t(guild.id, "general.unknown_error"),
                ephemeral=True,
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
        """Consume a deny/ban reason modal (optional text).

        The submit is ACKed through :func:`ack`, so a replayed modal with a
        dead token (10062) never reaches the moderation action.
        """
        guild = interaction.guild
        if guild is None:
            await ack(interaction, ephemeral=True)
            return
        t = self.bot.translator.t
        if not await ack(interaction, ephemeral=True):
            return
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
        return {
            "confession_number": entry.get("number", 0),
            "title": await self.bot.translator.t(
                guild.id, "confess.card.title", confession_number=entry.get("number", 0)
            ),
            "body": entry.get("body") or "",
            "image_url": entry.get("image_url") or "",
            "color_hex": entry.get("alias_color") or entry.get("color_hex") or "",
            "timestamp": timestamp,
            "alias_name": await self._alias_name(guild.id, str(entry.get("alias") or "")),
            "op_emoji": str(entry.get("op_emoji") or ""),
        }

    async def _render_review(
        self, guild: discord.Guild, entry: dict
    ):
        """Themed review card (or code fallback) plus its mention policy."""
        from rosemary.core.card_service import render_card_message

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
        from rosemary.core.card_service import simple_payload

        # The fallback follows the card's own form (a panel).
        body = "\n\n".join(
            str(part)
            for part in (variables.get("body"), variables.get("author_line"))
            if str(part or "").strip()
        )
        payload = simple_payload(
            self.bot,
            guild.id,
            "confessions.review",
            title=str(variables["title"]),
            body=body,
            color="info",
            image=variables.get("image_url"),
            footer=str(variables.get("timestamp") or ""),
        )
        return payload, discord.AllowedMentions.none()

    async def _render_public(self, guild: discord.Guild, entry: dict):
        """Themed public card (or code fallback) plus its mention policy."""
        from rosemary.core.card_service import render_card_message, simple_payload

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
        # The fallback follows the card's own form (an embed).
        payload = simple_payload(
            self.bot,
            guild.id,
            "confessions.card",
            title=str(variables["title"]),
            body=str(variables["body"] or ""),
            color="info",
        )
        return payload, discord.AllowedMentions.none()

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
            # The content (the ping carrier) and the files ride along: an
            # embed card may carry the essential ping in its content.
            return CardPayload(
                embed=payload.embed, view=view, files=payload.files, content=payload.content
            )
        payload.view.add_item(row)
        return payload

    async def post_confession(
        self,
        guild: discord.Guild,
        author: discord.Member,
        body: str,
        image: tuple[bytes, str] | None = None,
    ) -> tuple[str, discord.Message | None, str]:
        """Post straight to review or public.

        Returns ``(outcome, message, claim_token)``. Outcomes:
        ``"posted"``, ``"review"``, ``"no_channel"``, ``"no_review"``.
        In the sealed mode the author id is encrypted before it reaches the
        store and the member gets a claim token (the caller answers with it).
        """
        channel = await self._public_channel(guild)
        if channel is None:
            return "no_channel", None, ""
        files: list[discord.File] = []
        image_url = ""
        image_name = ""
        if image is not None:
            data, name = image
            files.append(discord.File(io.BytesIO(data), filename=name))
            image_url = f"attachment://{name}"
            image_name = name
        number = await self.store.next_number(guild.id)
        sealed = await self._sealed(guild.id)
        alias = await self._new_alias(guild.id)
        token = claim_token()
        author_seal = ""
        if sealed:
            author_seal, _key_id = await self._seal_author(guild.id, author.id)
        draft = {
            "number": number,
            # With the seal on the id is still passed to the store, which
            # replaces it with the sealed blob before it is written (the
            # legacy mode keeps the plain id for the reveal and the DMs).
            "author_id": author.id,
            "channel_id": channel.id,
            "body": body,
            "user_name": "" if sealed else author.display_name,
            "user_avatar": "" if sealed else author.display_avatar.url,
            "image_url": image_url,
            "alias": alias,
            "alias_color": random_color() if sealed else "",
            "author_seal": author_seal,
            "claim_hash": claim_hash(token),
        }
        self._claim_tokens[token] = {
            "guild_id": guild.id,
            "number": number,
            "user_id": author.id,
        }
        self._claim_order.append(token)
        while len(self._claim_order) > _CLAIM_MEMORY:
            old = self._claim_order.pop(0)
            self._claim_tokens.pop(old, None)
        if await self._review_required(guild.id):
            review = await self._review_channel(guild)
            if review is None:
                return "no_review", None, ""
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
            return "review", sent, token
        entry = {
            **draft,
            "body": body,
            "color_hex": draft["alias_color"] or random_color(),
            "op_emoji": "",
        }
        payload, allowed = await self._render_public(guild, entry)
        payload = await self._row_into(
            payload, (await self._public_view(guild.id)).children[0]
        )
        send_kwargs = payload.message_kwargs()
        if files:
            send_kwargs["files"] = files
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
            guild.id, sent.id, status="open", color_hex=entry["color_hex"], **draft,
        )
        return "posted", sent, token

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
        # The author is only readable in the legacy mode: with the seal on
        # the store has no id (the member checks the status with the token).
        author_id = int(entry.get("author_id") or 0)
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
        alias = str(entry.get("alias") or "")
        if not alias and author_id:
            alias = await self._identity(guild.id, number, author_id)
        public_entry = {
            **entry,
            "body": body,
            "color_hex": entry.get("alias_color") or random_color(),
            "image_url": image_url,
            "op_emoji": "",
        }
        payload, allowed = await self._render_public(guild, public_entry)
        payload = await self._row_into(
            payload, (await self._public_view(guild.id)).children[0]
        )
        send_kwargs = payload.message_kwargs()
        if files:
            send_kwargs["files"] = files
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
            color_hex=public_entry["color_hex"],
            status="open",
            alias=alias,
            alias_color=entry.get("alias_color") or "",
            author_seal=entry.get("author_seal") or "",
            claim_hash=entry.get("claim_hash") or "",
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
        motive = (reason or "").strip()
        # The motive rides its own line: the document keeps the frame, the
        # send site owns the wording.
        reason_line = ""
        if motive:
            reason_line = await self.bot.translator.t(
                guild.id, "confess.dm.reason", reason=motive
            )
        await self._dm_card_send(
            guild,
            int(entry.get("author_id") or 0),
            "confessions.dm.rejected",
            "danger",
            {
                "confession_number": entry.get("number", 0),
                "body": entry.get("body") or "",
                "reason": motive,
                "reason_line": reason_line,
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
            await self._send_ban_dm(guild, author_id, reason_text)
            await self._log(
                guild.id, "banned", "danger", [author_id],
                moderator=moderator.mention, user=f"<@{author_id}>",
                reason=reason_text,
            )
        return "ok", author_id

    async def _send_ban_dm(self, guild: discord.Guild, user_id: int, reason: str) -> bool:
        """The ban notice DM: a themed text card, never a ping.

        The card owns the shape (the copy and its icon come from the catalog
        and the theme), so the send site only hands it the motive.
        """
        from rosemary.ui.boost_dm import send as send_dm

        target = await self._dm_target(guild, user_id)
        if target is None:
            return False
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await send_dm(
                self.bot,
                target,
                guild.id,
                "confessions.dm.banned",
                default_key="confess.dm.banned",
                reason=reason,
            )
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
        from rosemary.core.card_service import render_card_message, simple_payload

        payload, _allowed = await render_card_message(
            self.bot, guild_id, key, variables
        )
        if payload is not None:
            return payload
        t = self.bot.translator.t
        buttons = ()
        if variables.get("message_link"):
            jump_label = await t(guild_id, "confess.dm.jump")
            buttons = ((jump_label, variables["message_link"]),)
        # The fallback follows the card's own form: the DM keeps its shape.
        # The content line names the event, the embed carries the subject
        # (the confession or the reply) and the motive.
        body = "\n".join(
            part
            for part in (variables.get("body"), variables.get("reason_line"))
            if part
        )
        return simple_payload(
            self.bot,
            guild_id,
            key,
            content_line=await t(guild_id, body_key, **variables),
            body=body,
            color=color_token,
            buttons=buttons,
        )

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
        parent_id: int | None,
        author: discord.Member,
        body: str,
        image: tuple[bytes, str] | None = None,
        *,
        claim: str = "",
    ) -> tuple[str, discord.Message | None]:
        """Post a reply in the confession's thread.

        With ``claim`` (the stateless path) the caller is the confession's
        own author: the token is matched against the in-memory context, the
        reply is marked OP and no user id is stored. The legacy path (the
        reply button) resolves the author by id and keeps the alias map.

        Outcomes: ``"ok"``, ``"gone"`` (confession deleted),
        ``"banned"``, ``"thread"`` (thread unavailable). A deleted parent
        degrades to a direct reply instead of failing.
        """
        entry = await self.store.get(guild.id, confession_id)
        if entry is None or entry.get("status") != "open":
            return "gone", None
        if await self.store.is_banned(
            guild.id, author.id, sealed=bool(entry.get("author_seal"))
        ):
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
            sealed = bool(entry.get("author_seal"))
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
            # In the sealed mode the claim token is the only proof of
            # authorship (the id was never stored).
            claimed = self._claim_tokens.get(claim) if claim else None
            is_op = bool(
                claimed
                and claimed.get("number") == number
                and claimed.get("guild_id") == guild.id
            )
            if not sealed:
                is_op = is_op or (bool(author_id) and author.id == author_id)
            parent_is_op = bool(parent and parent.get("is_op"))
            # A replier keeps the alias of their previous reply in this
            # thread when they have one (the reply carries its own alias, so
            # the identity survives the process and the sealed mode).
            alias = await self._reply_alias(guild.id, confession_id, author.id)
            if not alias:
                alias = (
                    str(entry.get("alias") or "")
                    if is_op
                    else await self._new_alias(guild.id)
                )
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
            reply_color = entry.get("alias_color") or random_color()
            reply_seal = ""
            if sealed:
                reply_seal, _key_id = await self._seal_author(guild.id, author.id)
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
                "color_hex": reply_color,
                "image_url": image_url,
                "alias_name": await self._alias_name(guild.id, alias),
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
                from rosemary.core.card_service import simple_payload

                payload = simple_payload(
                    self.bot,
                    guild.id,
                    "confessions.response",
                    title=str(variables["title"]),
                    body=body,
                    color="info",
                )
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
            if files:
                send_kwargs["files"] = files
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
                parent_index=parent_index,
                author_id=author.id,
                body=body,
                image_url=stored_image_url,
                alias=alias,
                alias_color=reply_color,
                is_op=is_op,
                parent_is_op=parent_is_op,
                author_seal=reply_seal,
                claim_hash=claim_hash(claim) if claim else "",
            )
            if stored is None:
                return "gone", None
        # The author of the confession hears about every reply but their own:
        # a DM about your own comment is noise.
        # The content line names the answer and, when the answer answers
        # another one, the answer it answers.
        reply_line_key = (
            "confess.dm.response_parent" if parent_line else "confess.dm.response"
        )
        if author_id and author.id != author_id:
            await self._dm_card_send(
                guild, author_id,
                "confessions.dm.response", "info",
                {
                    "confession_number": number,
                    "reply_index": index,
                    "body": body,
                    "message_link": sent.jump_url,
                    "parent_line": parent_line,
                },
                reply_line_key,
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
                        "parent_line": parent_line,
                    },
                    reply_line_key,
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
        await self._send_ban_dm(guild, member.id, reason_text)
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

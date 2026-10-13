"""Server suggestions: modal intake with button voting on a V2 card.

Members run ``/suggest`` (or the send button on any suggestion) to open a
text modal; the suggestion is posted in the configured channel as a
``suggestions.card`` (theme-customizable like every other card) with vote
buttons attached by code. One vote per person: clicking the same side twice
removes the vote, clicking the other side moves it. The card accent color
follows the score (positive gold, negative red, zero neutral).

Vote buttons use FIXED custom_ids served by a single persistent dispatcher
(the roleplay regive pattern): no per-message view accumulation, and clicks
keep working after a restart with nothing per guild to restore.
"""

import asyncio
import contextlib
import io
import logging
import re
import time

import discord
from discord.ext import commands

from rosemary.core.settings import get_setting
from rosemary.core.suggestions import SuggestionStore
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

#: Card repaints are message edits, and Discord caps those at 5 per 5s per
#: message: rapid voting (toggling to watch the ramp) would trip the bucket
#: and leave py-cord retrying for seconds. Votes always record instantly;
#: the card repaint is debounced with a trailing update (the starboard uses
#: the same cooldown, but skipping alone would leave stale counts here).
EDIT_COOLDOWN_SECONDS = 2.0

SUGGEST_MAX_LENGTH = 1000

#: Modal image uploads cap (keeps re-uploads inside Discord attachment limits).
SUGGEST_IMAGE_MAX_BYTES = 8 * 1024 * 1024

_UP_ID = "suggest_up"
_DOWN_ID = "suggest_down"
_NEW_ID = "suggest_new"


def _score_color(score: int) -> str:
    """Theme color token for a vote score (green/red, neutral otherwise)."""
    if score > 0:
        return "success"
    if score < 0:
        return "danger"
    return "info"


def _score_hex(theme, score: int) -> str:
    """Accent color for a vote balance as ``#rrggbb`` (documents speak hex).

    Continuous ramp when the theme defines ``suggestion_ramp`` (every vote
    nudges the tone toward green/red, starboard-style); otherwise the plain
    ``success``/``danger``/``info`` token resolved through the palette, so
    guild themes without a ramp keep a working look.
    """
    if theme is not None and theme.suggestion_ramp_config() is not None:
        return theme.suggestion_ramp(score)
    token = _score_color(score)
    value = theme.color(token).value if theme is not None else 0x3498DB
    return f"#{value:06X}"


def _side_color(side: str | None) -> str:
    """Theme color token for one vote event (green up, red down)."""
    if side == "up":
        return "success"
    if side == "down":
        return "danger"
    return "info"


def _safe_filename(name: str) -> str:
    """Attachment-safe filename (``attachment://`` URLs reject spaces)."""
    clean = re.sub(r"[^\w.\-]+", "_", (name or "").strip()).strip("._")
    return (clean or "suggestion.png")[:80]


def _image_url_from(attachments) -> str:
    """First image attachment's CDN URL (empty when none)."""
    for attachment in attachments or []:
        if (getattr(attachment, "content_type", "") or "").startswith("image/"):
            return attachment.url
    return ""


async def default_suggestion_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default suggestion card (``suggestions.card``).

    Layout: the numbered title beside the author's avatar thumbnail, the
    suggestion body, the optional image gallery (self-drops when empty),
    the live vote counts (``{up}``/``{down}`` resolve from theme emojis),
    the standard author footer and a small hint with the guild's localized
    command name. A decided suggestion (``status`` ``approved``/``rejected``)
    snaps to its fixed accent and gains a staff status line instead of the
    score ramp. Placeholders stay literal: this is a template resolved
    at render.
    """
    from rosemary.core.cards import author_footer_blocks

    score = int(variables.get("score") or 0)
    status = variables.get("status") or "open"
    if status == "approved":
        color: str = "gold"
    elif status == "rejected":
        color = "danger"
    else:
        color = _score_hex(getattr(bot, "theme", None), score)
    command = await bot.translator.t(guild_id, "suggest.command.name")
    hint = await bot.translator.t(guild_id, "suggest.card.hint", command=command)
    children: list[dict] = [
        {
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
            "children": [
                {"type": "text", "body": "# {title}"},
                {"type": "text", "body": "{body}"},
            ],
        },
        {"type": "gallery", "urls": ["{image_url}"]},
        {"type": "text", "body": "{up} {upvotes} · {down} {downvotes}"},
    ]
    if status in ("approved", "rejected"):
        line = await bot.translator.t(guild_id, f"suggest.status.{status}")
        children.append({"type": "text", "body": line})
    children.extend(await author_footer_blocks(bot, guild_id))
    children.append({"type": "text", "body": f"-# {hint}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": color, "children": children}],
    }


async def default_suggestion_vote_dm_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default vote notice DM (``suggestions.dm.vote``).

    Layout: the vote sentence from ``suggest.dm.body`` (who voted, which
    suggestion, quoted text) plus a link button jumping to the suggestion.
    The accent follows the voted side (green up, red down). The template is
    read via ``raw()``: ``t()`` would attempt formatting without variables
    and log a warning on every vote. Placeholders stay literal: this is a
    template resolved at render.
    """
    raw = bot.translator.raw
    body = await raw(guild_id, "suggest.dm.body")
    jump_label = await bot.translator.t(guild_id, "suggest.dm.jump")
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": _side_color(variables.get("side")),
                "children": [
                    {"type": "text", "body": body},
                    {
                        "type": "row",
                        "buttons": [{"label": jump_label, "url": "{message_link}"}],
                    },
                ],
            }
        ],
    }


async def default_suggestion_decision_dm_document(
    bot, guild_id: int, **variables
) -> dict:
    """Catalog-default approval/rejection DM (``suggestions.dm.approved`` etc).

    One builder serves both keys: ``status`` selects the body template
    (``suggest.dm.approved``/``suggest.dm.rejected``) and the accent
    (gold/danger). The optional ``reason`` appends a resolved motive line;
    an empty reason renders nothing. Templates come from ``raw()`` so no
    formatting warning fires; placeholders resolve at render.
    """
    status = variables.get("status") or "approved"
    if status not in ("approved", "rejected"):
        status = "approved"
    raw = bot.translator.raw
    body = await raw(guild_id, f"suggest.dm.{status}")
    jump_label = await bot.translator.t(guild_id, "suggest.dm.jump")
    children: list[dict] = [{"type": "text", "body": body}]
    if variables.get("reason"):
        reason_line = await bot.translator.t(
            guild_id, "suggest.dm.reason", **variables
        )
        children.append({"type": "text", "body": reason_line})
    children.append(
        {
            "type": "row",
            "buttons": [{"label": jump_label, "url": "{message_link}"}],
        }
    )
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": "gold" if status == "approved" else "danger",
                "children": children,
            }
        ],
    }


def _register_default_builder() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("suggestions.card", default_suggestion_document)
    set_default_builder("suggestions.dm.vote", default_suggestion_vote_dm_document)
    set_default_builder("suggestions.dm.approved", default_suggestion_decision_dm_document)
    set_default_builder("suggestions.dm.rejected", default_suggestion_decision_dm_document)


_register_default_builder()


class SuggestionVoteView(MenuView):
    """Fixed-id vote buttons (one dispatcher serves every suggestion)."""

    def __init__(self, bot, guild_id: int, upvotes: int = 0, downvotes: int = 0) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.upvotes = upvotes
        self.downvotes = downvotes
        self.register(_UP_ID, self._vote_up)
        self.register(_DOWN_ID, self._vote_down)
        self.register(_NEW_ID, self._new)

    def _emoji(self, name: str, fallback: str) -> str | None:
        theme = getattr(self.bot, "theme", None)
        value = theme.emoji(name) if theme is not None else ""
        return value or fallback or None

    async def build_row(self) -> discord.ui.ActionRow:
        """The vote buttons showing the current counts, plus the send button."""
        return discord.ui.ActionRow(
            self.make_button(
                custom_id=_UP_ID,
                label=str(self.upvotes),
                emoji=self._emoji("up", "⬆️"),
                style=discord.ButtonStyle.success,
            ),
            self.make_button(
                custom_id=_DOWN_ID,
                label=str(self.downvotes),
                emoji=self._emoji("down", "⬇️"),
                style=discord.ButtonStyle.danger,
            ),
            self.make_button(
                custom_id=_NEW_ID,
                label=await self.bot.translator.t(self.guild_id, "suggest.vote.new"),
                emoji=self._emoji("pencil", "✏️"),
                style=discord.ButtonStyle.primary,
            ),
        )

    async def build_items(self) -> list[discord.ui.ViewItem]:
        return [await self.build_row()]

    async def prepare(self) -> None:
        # Persistent dispatcher views are assembled by hand (build_row) and
        # never go through the menu timeout/rebuild cycle.
        self.clear_items()
        for item in await self.build_items():
            self.add_item(item)

    async def _vote_up(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "up")

    async def _vote_down(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "down")

    async def _new(self, interaction: discord.Interaction) -> None:
        """Open the suggestion modal (works even with the channel locked).

        A modal must be the FIRST response: never defer before send_modal.
        """
        guild = interaction.guild
        if guild is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        cog = self.bot.get_cog("SuggestionsCog")
        t = self.bot.translator.t
        if cog is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        if not await get_setting(self.bot.storage, guild.id, "suggestions.enabled"):
            return await interaction.response.send_message(
                await t(guild.id, "suggest.error_disabled"), ephemeral=True
            )
        if await cog._target_channel(guild) is None:
            return await interaction.response.send_message(
                await t(guild.id, "suggest.error_no_channel"), ephemeral=True
            )
        try:
            await interaction.response.send_modal(await cog.build_suggest_modal(guild.id))
        except discord.HTTPException as exc:
            log.warning("Suggestion modal failed in %s: %s", guild.id, exc)

    async def _vote(self, interaction: discord.Interaction, side: str) -> None:
        guild = interaction.guild
        message = getattr(interaction, "message", None)
        if guild is None or message is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        cog = self.bot.get_cog("SuggestionsCog")
        if cog is None:
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.defer(ephemeral=True)
            return
        entry = await cog.store.get(guild.id, message.id)
        if entry is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(guild.id, "suggest.error_gone"),
                    ephemeral=True,
                )
            return
        if entry.get("status", "open") != "open":
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        guild.id,
                        "suggest.error_decided",
                        suggestion_number=entry.get("number", 0),
                    ),
                    ephemeral=True,
                )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            ok = await cog.apply_vote(guild, message, interaction.user.id, side)
        except Exception as exc:
            log.error("Suggestion vote failed: %s", exc)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    await self.bot.translator.t(guild.id, "general.unknown_error"),
                    ephemeral=True,
                )
            return
        if not ok:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    await self.bot.translator.t(guild.id, "suggest.error_gone"),
                    ephemeral=True,
                )
            return
        entry = await cog.store.get(guild.id, message.id) or {}
        up, down = SuggestionStore.counts(entry)
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(
                await self.bot.translator.t(guild.id, "suggest.voted", up=up, down=down),
                ephemeral=True,
            )


class SuggestionsCog(commands.Cog):
    """Modal intake plus button voting for server suggestions."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = SuggestionStore(bot.storage.data_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_edit: dict[int, float] = {}
        self._pending: set[int] = set()
        self._started = False

    def _lock_for(self, message_id: int) -> asyncio.Lock:
        if message_id not in self._locks:
            self._locks[message_id] = asyncio.Lock()
        return self._locks[message_id]

    async def start(self) -> None:
        """Register the single persistent vote dispatcher once per process."""
        if self._started:
            return
        self._started = True
        # py-cord's ViewStore indexes only walk_children() results: an empty
        # dispatcher view registers nothing and every click dies unanswered.
        view = SuggestionVoteView(self.bot, 0)
        view.add_item(await view.build_row())
        self.bot.add_view(view)

    #: helpers ---------------------------------------------------------------

    async def _target_channel(self, guild: discord.Guild):
        channel_id = await get_setting(self.bot.storage, guild.id, "suggestions.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _title_for(self, guild_id: int, number: int) -> str:
        return await self.bot.translator.t(
            guild_id, "suggest.card.title", suggestion_number=number
        )

    def _variables_for(
        self,
        guild: discord.Guild | None,
        entry: dict,
        *,
        message_link: str = "",
        timestamp: str = "",
    ) -> dict:
        author_id = int(entry.get("author_id") or 0)
        member = guild.get_member(author_id) if guild is not None else None
        if member is not None:
            mention = member.mention
            name = member.display_name
            avatar = member.display_avatar.url
        else:
            mention = f"<@{author_id}>" if author_id else ""
            name = entry.get("user_name") or mention
            avatar = entry.get("user_avatar") or ""
        up, down = SuggestionStore.counts(entry)
        return {
            "user": mention,
            "user_name": name,
            "user_avatar": avatar,
            "suggestion_number": entry.get("number", 0),
            "body": entry.get("body") or "",
            "image_url": entry.get("image_url") or "",
            "upvotes": up,
            "downvotes": down,
            "score": up - down,
            "message_link": message_link,
            "timestamp": timestamp,
            "status": entry.get("status", "open") or "open",
        }

    async def _card_payload(self, guild_id: int, variables: dict):
        """Themed card (or code fallback) plus its mention policy."""
        from rosemary.core.card_service import CardPayload, render_card_message

        payload, allowed = await render_card_message(
            self.bot, guild_id, "suggestions.card", variables
        )
        if payload is not None:
            return payload, allowed
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        command = await self.bot.translator.t(guild_id, "suggest.command.name")
        hint = await self.bot.translator.t(guild_id, "suggest.card.hint", command=command)
        status = variables.get("status") or "open"
        if status == "approved":
            accent = theme.color("gold")
        elif status == "rejected":
            accent = theme.color("danger")
        else:
            accent = discord.Colour(
                int(_score_hex(theme, int(variables.get("score") or 0)).lstrip("#"), 16)
            )
        items: list = [
            TextDisplay(f"# {variables.get('title', '')}"),
            TextDisplay(str(variables.get("body") or "")),
            TextDisplay(f"{variables.get('upvotes', 0)} · {variables.get('downvotes', 0)}"),
        ]
        if status in ("approved", "rejected"):
            items.append(
                TextDisplay(
                    await self.bot.translator.t(guild_id, f"suggest.status.{status}")
                )
            )
        items.append(TextDisplay(f"-# {hint}"))
        view = DesignerView(store=False)
        view.add_item(designer_container(accent, *items))
        return CardPayload(view=view), discord.AllowedMentions.none()

    async def _attach_row(self, payload, guild_id: int, up: int, down: int):
        """Attach the vote row whatever form the card took (V2 or embed)."""
        from rosemary.core.card_service import CardPayload

        row = await SuggestionVoteView(self.bot, guild_id, up, down).build_row()
        if payload.embed is not None:
            view = payload.view or discord.ui.View(timeout=None)
            for item in row.children:
                view.add_item(item)
            return CardPayload(embed=payload.embed, view=view)
        payload.view.add_item(row)
        return payload

    async def post_suggestion(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        author: discord.Member,
        body: str,
        image: tuple[bytes, str] | None = None,
    ):
        """Render, send and record a suggestion. Returns the sent message.

        ``image`` is ``(raw bytes, filename)`` from the modal upload: it is
        re-uploaded with the message (``attachment://`` reference in the
        card) and the persistent CDN URL is stored for later repaints.
        """
        number = await self.store.next_number(guild.id)
        timestamp = discord.utils.format_dt(discord.utils.utcnow(), style="R")
        files: list[discord.File] = []
        image_url = ""
        if image is not None:
            data, name = image
            files.append(discord.File(io.BytesIO(data), filename=name))
            image_url = f"attachment://{name}"
        variables = {
            "user": author.mention,
            "user_name": author.display_name,
            "user_avatar": author.display_avatar.url,
            "suggestion_number": number,
            "title": await self._title_for(guild.id, number),
            "body": body,
            "image_url": image_url,
            "upvotes": 0,
            "downvotes": 0,
            "score": 0,
            "message_link": "",
            "timestamp": timestamp,
            "status": "open",
        }
        payload, allowed = await self._card_payload(guild.id, variables)
        payload = await self._attach_row(payload, guild.id, 0, 0)
        send_kwargs = payload.message_kwargs()
        if files:
            send_kwargs["files"] = files
        sent = await channel.send(**send_kwargs, allowed_mentions=allowed)
        stored_image_url = _image_url_from(sent.attachments)
        if files and not stored_image_url:
            # py-cord's send response may come back without the attachment
            # list; fetch the message to get the signed CDN URL, otherwise
            # every later repaint drops the gallery.
            try:
                fresh = await channel.fetch_message(sent.id)
                stored_image_url = _image_url_from(fresh.attachments)
            except discord.HTTPException:
                stored_image_url = ""
        await self.store.put(
            guild.id,
            sent.id,
            number=number,
            author_id=author.id,
            channel_id=channel.id,
            body=body,
            user_name=author.display_name,
            user_avatar=author.display_avatar.url,
            image_url=stored_image_url,
        )
        # Refresh once with the persistent URLs (CDN image + jump link),
        # but skip the second edit when the card references neither.
        variables["image_url"] = stored_image_url
        variables["message_link"] = sent.jump_url
        from rosemary.core.card_service import get_effective_document

        doc = await get_effective_document(
            self.bot, guild.id, "suggestions.card", variables
        )
        if stored_image_url or (doc is not None and "message_link" in str(doc)):
            payload, allowed = await self._card_payload(guild.id, variables)
            payload = await self._attach_row(payload, guild.id, 0, 0)
            with contextlib.suppress(discord.HTTPException):
                await sent.edit(**payload.message_kwargs(), allowed_mentions=allowed)
        await self._log_created(guild.id, author, number, channel)
        return sent

    async def apply_vote(
        self,
        guild: discord.Guild,
        message: discord.Message,
        voter_id: int,
        side: str,
    ) -> bool:
        """Toggle a vote and repaint the card. Returns ``False`` when gone.

        The vote and the DM always happen immediately; only the card repaint
        is debounced (see :func:`_repaint_card`).
        """
        async with self._lock_for(message.id):
            updated = await self.store.toggle_vote(guild.id, message.id, voter_id, side)
            if updated is None:
                return False
            await self._repaint_card(guild, message, updated)
            await self._dm_owner(guild, updated, voter_id, side, message.jump_url)
            return True

    async def _repaint_card(
        self, guild: discord.Guild, message: discord.Message, entry: dict
    ) -> None:
        """Repaint the card at most once per :data:`EDIT_COOLDOWN_SECONDS`.

        Within the cooldown the repaint is skipped and a single trailing
        update is scheduled: it re-reads the store (the latest consolidated
        counts) and edits once, so rapid toggling never trips the 5-edits/
        5s per-message bucket and the card always converges.
        """
        last = self._last_edit.get(message.id, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < EDIT_COOLDOWN_SECONDS:
            if message.id not in self._pending:
                self._pending.add(message.id)
                delay = EDIT_COOLDOWN_SECONDS - elapsed
                asyncio.create_task(self._trailing_repaint(guild, message, delay))
            return
        self._last_edit[message.id] = time.monotonic()
        await self._render_and_edit(guild, message, entry)

    async def _trailing_repaint(
        self, guild: discord.Guild, message: discord.Message, delay: float
    ) -> None:
        """The consolidated repaint after the vote cooldown elapses."""
        try:
            await asyncio.sleep(max(0.0, delay))
            async with self._lock_for(message.id):
                entry = await self.store.get(guild.id, message.id)
                self._pending.discard(message.id)
                if entry is None:
                    return
                self._last_edit[message.id] = time.monotonic()
                await self._render_and_edit(guild, message, entry)
        except Exception as exc:
            log.error("Trailing suggestion repaint failed: %s", exc)

    async def _render_and_edit(
        self, guild: discord.Guild, message: discord.Message, entry: dict
    ) -> None:
        """Build the card variables for ``entry`` and edit the message."""
        variables = self._variables_for(
            guild,
            entry,
            message_link=message.jump_url,
            timestamp=discord.utils.format_dt(message.created_at, style="R"),
        )
        live_image = _image_url_from(getattr(message, "attachments", None))
        if live_image:
            # The clicked message's own attachments win over the store:
            # entries saved before the CDN URL was persisted self-heal.
            variables["image_url"] = live_image
        variables["title"] = await self._title_for(guild.id, entry.get("number", 0))
        payload, allowed = await self._card_payload(guild.id, variables)
        up, down = SuggestionStore.counts(entry)
        payload = await self._attach_row(payload, guild.id, up, down)
        await message.edit(**payload.message_kwargs(), allowed_mentions=allowed)

    async def _dm_owner(
        self,
        guild: discord.Guild,
        entry: dict,
        voter_id: int,
        side: str,
        message_link: str,
    ) -> None:
        """Tell the author someone voted (skipped for self-votes).

        Best effort: closed DMs or a departed author just skip silently.
        Mentions never ping: the voter mention is informational inside a DM.
        """
        author_id = int(entry.get("author_id") or 0)
        if not author_id or voter_id == author_id:
            return
        if not await get_setting(self.bot.storage, guild.id, "suggestions.dm_votes"):
            return
        voter = guild.get_member(voter_id)
        voter_mention = voter.mention if voter is not None else f"<@{voter_id}>"
        variables = {
            "voter": voter_mention,
            "suggestion_number": entry.get("number", 0),
            "body": entry.get("body") or "",
            "message_link": message_link,
            "vote": await self.bot.translator.t(guild.id, f"suggest.dm.{side}"),
            "side": side,
        }
        from rosemary.core.card_service import CardPayload, render_card_message

        payload, _allowed = await render_card_message(
            self.bot, guild.id, "suggestions.dm.vote", variables
        )
        if payload is None:
            from rosemary.ui.containers import (
                ActionRow,
                DesignerView,
                TextDisplay,
                designer_container,
            )

            theme = self.bot.theme
            t = self.bot.translator.t
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    theme.color(_side_color(side)),
                    TextDisplay(await t(guild.id, "suggest.dm.body", **variables)),
                )
            )
            view.add_item(
                ActionRow(
                    discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=await t(guild.id, "suggest.dm.jump"),
                        url=message_link,
                    )
                )
            )
            payload = CardPayload(view=view)
        target = guild.get_member(author_id)
        if target is None:
            try:
                target = await self.bot.fetch_user(author_id)
            except (discord.NotFound, discord.HTTPException):
                return
        if target is None:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(
                **payload.message_kwargs(),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def decide_suggestion(
        self,
        guild: discord.Guild,
        number: int,
        *,
        moderator: discord.Member,
        decision: str,
        reason: str = "",
    ) -> tuple[str, discord.Message | None]:
        """Staff decision on a suggestion, resolved by its number.

        Returns ``("ok", message)``, ``("not_found", None)``,
        ``("decided", None)`` or ``("gone", None)`` (channel/message
        deleted: the stale entry is dropped).
        """
        found = await self.store.get_by_number(guild.id, number)
        if found is None:
            return "not_found", None
        message_id, entry = found
        if entry.get("status", "open") != "open":
            return "decided", None
        channel = guild.get_channel(int(entry.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            await self.store.remove(guild.id, message_id)
            return "gone", None
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.store.remove(guild.id, message_id)
            return "gone", None
        updated = await self.store.set_status(
            guild.id, message_id, decision, moderator.id
        )
        if updated is None:
            return "gone", None
        async with self._lock_for(message.id):
            self._last_edit[message.id] = time.monotonic()
            await self._render_and_edit(guild, message, updated)
        await self._dm_decision(
            guild, updated, moderator, decision, reason, message.jump_url
        )
        await self._log_decision(guild.id, updated, moderator, decision, reason)
        return "ok", message

    async def _dm_decision(
        self,
        guild: discord.Guild,
        entry: dict,
        moderator: discord.Member,
        decision: str,
        reason: str,
        message_link: str,
    ) -> None:
        """Tell the author their suggestion was approved/rejected (best effort)."""
        author_id = int(entry.get("author_id") or 0)
        if not author_id:
            return
        variables = {
            "suggestion_number": entry.get("number", 0),
            "body": entry.get("body") or "",
            "message_link": message_link,
            "moderator": moderator.mention,
            "reason": (reason or "").strip(),
            "status": decision,
        }
        from rosemary.core.card_service import CardPayload, render_card_message

        payload, _allowed = await render_card_message(
            self.bot, guild.id, f"suggestions.dm.{decision}", variables
        )
        if payload is None:
            from rosemary.ui.containers import (
                ActionRow,
                DesignerView,
                TextDisplay,
                designer_container,
            )

            theme = self.bot.theme
            t = self.bot.translator.t
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    theme.color("gold" if decision == "approved" else "danger"),
                    TextDisplay(
                        await t(guild.id, f"suggest.dm.{decision}", **variables)
                    ),
                )
            )
            view.add_item(
                ActionRow(
                    discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=await t(guild.id, "suggest.dm.jump"),
                        url=variables["message_link"],
                    )
                )
            )
            payload = CardPayload(view=view)
        target = guild.get_member(author_id)
        if target is None:
            try:
                target = await self.bot.fetch_user(author_id)
            except (discord.NotFound, discord.HTTPException):
                return
        if target is None:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(
                **payload.message_kwargs(),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _log_decision(
        self,
        guild_id: int,
        entry: dict,
        moderator: discord.Member,
        decision: str,
        reason: str,
    ) -> None:
        from rosemary.core.cards import log_description
        from rosemary.core.debug import send_channel_log

        reason_text = (reason or "").strip() or await self.bot.translator.t(
            guild_id, "suggest.no_reason"
        )
        author_id = int(entry.get("author_id") or 0)
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(
                guild_id, f"suggestions.logs.{decision}.title"
            ),
            await log_description(
                self.bot,
                guild_id,
                f"suggestions.logs.{decision}.description",
                moderator=moderator.mention,
                user=f"<@{author_id}>" if author_id else "",
                suggestion_number=entry.get("number", 0),
                reason=reason_text,
            ),
            color="success" if decision == "approved" else "danger",
            card_key=f"suggestions.logs.{decision}.description",
            mention_user_ids=[author_id] if author_id else [],
        )

    async def _log_created(
        self,
        guild_id: int,
        author: discord.Member,
        number: int,
        channel: discord.TextChannel,
    ) -> None:
        from rosemary.core.cards import log_description
        from rosemary.core.debug import send_channel_log

        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "suggestions.logs.created.title"),
            await log_description(
                self.bot,
                guild_id,
                "suggestions.logs.created.description",
                user=author.mention,
                suggestion_number=number,
                channel=channel.mention,
            ),
            color="success",
            card_key="suggestions.logs.created.description",
            mention_user_ids=[author.id],
        )

    #: commands ------------------------------------------------------------

    #: Single-cog group: ``/suggest send`` opens the modal, staff decides
    #: with ``/suggest approve`` / ``/suggest reject`` by suggestion number.
    suggest_group = discord.SlashCommandGroup(
        "suggest",
        description="Send server suggestions",
        contexts={discord.InteractionContextType.guild},
    )

    async def build_suggest_modal(self, guild_id: int):
        """The suggestion intake modal (shared by /suggest and the send button)."""
        from rosemary.ui.modals import make_text_file_modal

        t = self.bot.translator.t
        return make_text_file_modal(
            title=await t(guild_id, "suggest.modal.title"),
            custom_id="suggest_modal",
            label=await t(guild_id, "suggest.modal.label"),
            placeholder=await t(guild_id, "suggest.modal.placeholder"),
            max_length=SUGGEST_MAX_LENGTH,
            style=discord.InputTextStyle.paragraph,
            file_label=await t(guild_id, "suggest.modal.image_label"),
            file_description=await t(guild_id, "suggest.modal.image_description"),
            on_submit=self._modal_submit,
        )

    async def _modal_submit(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        attachments: list[discord.Attachment],
    ) -> None:
        """Validate modal text and image, then post the suggestion."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.defer(ephemeral=True)
            return
        t = self.bot.translator.t
        await interaction.response.defer(ephemeral=True)
        body = (raw_text or "").strip()
        if not body:
            return await interaction.followup.send(
                await t(guild.id, "suggest.error_empty"), ephemeral=True
            )
        if len(body) > SUGGEST_MAX_LENGTH:
            return await interaction.followup.send(
                await t(guild.id, "suggest.error_too_long"), ephemeral=True
            )
        image = None
        if attachments:
            picked = attachments[0]
            if not (picked.content_type or "").startswith("image/"):
                return await interaction.followup.send(
                    await t(guild.id, "suggest.error_bad_image"), ephemeral=True
                )
            if (picked.size or 0) > SUGGEST_IMAGE_MAX_BYTES:
                return await interaction.followup.send(
                    await t(guild.id, "suggest.error_image_too_large"), ephemeral=True
                )
            try:
                data = await picked.read()
            except discord.HTTPException:
                data = b""
            if not data:
                return await interaction.followup.send(
                    await t(guild.id, "suggest.error_failed"), ephemeral=True
                )
            image = (bytes(data), _safe_filename(picked.filename))
        channel = await self._target_channel(guild)
        if channel is None:
            return await interaction.followup.send(
                await t(guild.id, "suggest.error_no_channel"), ephemeral=True
            )
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.followup.send(
                await t(guild.id, "general.unknown_error"), ephemeral=True
            )
        try:
            await self.post_suggestion(guild, channel, member, body, image)
        except discord.Forbidden:
            log.warning("Suggestion send forbidden in %s", guild.id)
            return await interaction.followup.send(
                await t(guild.id, "suggest.error_no_perms"), ephemeral=True
            )
        except (discord.HTTPException, discord.NotFound) as exc:
            log.warning("Suggestion send failed in %s: %s", guild.id, exc)
            return await interaction.followup.send(
                await t(guild.id, "suggest.error_failed"), ephemeral=True
            )
        await interaction.followup.send(
            await t(guild.id, "suggest.sent"), ephemeral=True
        )

    @suggest_group.command(
        name="send",
        description="Send a server suggestion",
    )
    async def suggest_send(self, ctx: discord.ApplicationContext) -> None:
        """Open the suggestion modal."""
        guild = ctx.guild
        if guild is None:
            return
        t = self.bot.translator.t
        if not await get_setting(self.bot.storage, guild.id, "suggestions.enabled"):
            return await ctx.respond(
                await t(guild.id, "suggest.error_disabled"), ephemeral=True
            )
        if await self._target_channel(guild) is None:
            return await ctx.respond(
                await t(guild.id, "suggest.error_no_channel"), ephemeral=True
            )
        await ctx.send_modal(await self.build_suggest_modal(guild.id))

    async def _decide_command(
        self,
        ctx: discord.ApplicationContext,
        number: int,
        decision: str,
        reason: str | None,
    ) -> None:
        """Shared body of the approve/reject staff subcommands."""
        guild = ctx.guild
        if guild is None:
            return
        t = self.bot.translator.t
        if not await get_setting(self.bot.storage, guild.id, "suggestions.enabled"):
            return await ctx.respond(
                await t(guild.id, "suggest.error_disabled"), ephemeral=True
            )
        member = guild.get_member(ctx.author.id)
        if member is None:
            return await ctx.respond(
                await t(guild.id, "general.unknown_error"), ephemeral=True
            )
        await ctx.defer(ephemeral=True)
        outcome, _message = await self.decide_suggestion(
            guild,
            number,
            moderator=member,
            decision=decision,
            reason=reason or "",
        )
        if outcome == "not_found":
            return await ctx.followup.send(
                await t(guild.id, "suggest.error_not_found", suggestion_number=number),
                ephemeral=True,
            )
        if outcome == "decided":
            return await ctx.followup.send(
                await t(guild.id, "suggest.error_decided", suggestion_number=number),
                ephemeral=True,
            )
        if outcome == "gone":
            return await ctx.followup.send(
                await t(guild.id, "suggest.error_gone"), ephemeral=True
            )
        await ctx.followup.send(
            await t(guild.id, f"suggest.decided_{decision}", suggestion_number=number),
            ephemeral=True,
        )

    @suggest_group.command(
        name="approve",
        description="Approve a suggestion by number",
        default_member_permissions=discord.Permissions(manage_messages=True),
    )
    async def suggest_approve(
        self,
        ctx: discord.ApplicationContext,
        number: discord.Option(int, description="Suggestion number", min_value=1),
        reason: discord.Option(
            str, description="Reason shown to the author", required=False
        ),
    ) -> None:
        """Mark a suggestion approved (staff)."""
        await self._decide_command(ctx, number, "approved", reason)

    @suggest_group.command(
        name="reject",
        description="Reject a suggestion by number",
        default_member_permissions=discord.Permissions(manage_messages=True),
    )
    async def suggest_reject(
        self,
        ctx: discord.ApplicationContext,
        number: discord.Option(int, description="Suggestion number", min_value=1),
        reason: discord.Option(
            str, description="Reason shown to the author", required=False
        ),
    ) -> None:
        """Mark a suggestion rejected (staff)."""
        await self._decide_command(ctx, number, "rejected", reason)

    #: listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        self._last_edit.pop(payload.message_id, None)
        try:
            await self.store.remove(payload.guild_id, payload.message_id)
        except Exception as exc:
            log.error("Suggestion cleanup failed for %s: %s", payload.message_id, exc)

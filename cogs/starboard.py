"""Starboard: highlight popular messages on a dedicated board.

Reactions with the theme star emoji are counted (with an optional self-star
rule and bot filter); once the configured threshold is reached the message is
published as a V2 card whose accent color climbs through ``star_tier_*``
theme styles. Edits are debounced per message; deleting an original removes
its post. Nothing here is customizable through /customize: the card mirrors
someone else's message: but every knob lives in /settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import discord
from discord.ext import commands

from rosemary.core.settings import get_setting
from rosemary.core.starboard import StarboardStore

log = logging.getLogger(__name__)

EDIT_COOLDOWN_SECONDS = 2.0


class StarboardCog(commands.Cog):
    """Raw reaction listeners driving the starboard."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = StarboardStore(bot.storage.data_dir)
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_edit: dict[int, float] = {}

    def _lock_for(self, message_id: int) -> asyncio.Lock:
        if message_id not in self._locks:
            self._locks[message_id] = asyncio.Lock()
        return self._locks[message_id]

    async def _star_emoji(self, guild_id: int) -> str:
        return self.bot.theme.emoji("star") or "⭐"

    async def _config(self, guild_id: int) -> dict:
        return {
            "enabled": await get_setting(self.bot.storage, guild_id, "starboard.enabled"),
            "channel": await get_setting(self.bot.storage, guild_id, "starboard.channel"),
            "threshold": await get_setting(self.bot.storage, guild_id, "starboard.threshold"),
            "self_star": await get_setting(
                self.bot.storage, guild_id, "starboard.self_star_counts"
            ),
            "allow_bots": await get_setting(self.bot.storage, guild_id, "starboard.allow_bots"),
        }

    #: counting ------------------------------------------------------------

    @staticmethod
    async def _effective_stars(
        reaction: discord.Reaction, *, author_id: int, self_star_counts: bool
    ) -> int:
        try:
            users = [user async for user in reaction.users() if not user.bot]
        except discord.HTTPException:
            return 0
        others = sum(1 for user in users if user.id != author_id)
        if not self_star_counts:
            return others
        return len(users)

    #: rendering -----------------------------------------------------------

    def _build_card(
        self,
        message: discord.Message,
        stars: int,
        jump_label: str,
        no_content_label: str,
        footer_text: str,
    ) -> discord.ui.DesignerView:
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        star = theme.emoji("star")
        channel_mention = getattr(message.channel, "mention", f"#{message.channel}")
        title = f"{star} {stars} • {channel_mention}"
        body = message.content or no_content_label
        image_url = next(
            (
                attachment.url
                for attachment in message.attachments
                if (attachment.content_type or "").startswith("image/")
            ),
            None,
        )
        container_items: list[discord.ui.ViewItem] = [
            discord.ui.Section(
                TextDisplay(theme.md("title", title=title)),
                TextDisplay(body),
                accessory=discord.ui.Thumbnail(message.author.display_avatar.url),
            )
        ]
        if image_url is not None:
            container_items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)))
        container_items.append(discord.ui.TextDisplay(footer_text))
        container_items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label=jump_label,
                    url=message.jump_url,
                )
            )
        )
        color = _ramp_color(theme, stars)
        view = DesignerView(store=False)
        view.add_item(designer_container(color, *container_items))
        return view

    async def _card_view(
        self,
        guild_id: int,
        message: discord.Message,
        stars: int,
        jump_label: str,
        no_content_label: str,
    ) -> discord.ui.DesignerView:
        """Themed ``starboard.card`` when defined, else the code-built default.

        Routed like every other card so a theme can restyle the mural post;
        the default builder stays in code because the layout reacts to the
        star tier (accent color) and the original attachments."""
        from rosemary.core.card_service import render_card_message

        star = _ramp_emoji(self.bot.theme, stars)
        channel_mention = getattr(message.channel, "mention", f"#{message.channel}")
        title = f"{star} {stars} • {channel_mention}"
        body = message.content or no_content_label
        image_url = next(
            (
                attachment.url
                for attachment in message.attachments
                if (attachment.content_type or "").startswith("image/")
            ),
            None,
        )
        payload, _allowed = await render_card_message(
            self.bot,
            guild_id,
            "starboard.card",
            {
                "user": message.author.mention,
                "user_name": message.author.display_name,
                "user_avatar": message.author.display_avatar.url,
                "stars": stars,
                "title": title,
                "body": body,
                "image_url": image_url or "",
                "timestamp": discord.utils.format_dt(message.created_at, style="R"),
            },
        )
        if payload is not None:
            return payload
        from rosemary.core.card_service import CardPayload

        footer_text = await self._footer_text(
            guild_id,
            message.author.mention,
            discord.utils.format_dt(message.created_at, style="R"),
        )
        return CardPayload(
            view=self._build_card(message, stars, jump_label, no_content_label, footer_text)
        )

    async def _footer_text(self, guild_id: int, user_mention: str, timestamp: str) -> str:
        """The author footer line for the code-built fallback (``-#`` small)."""
        from rosemary.core.cards import author_footer_template, author_footer_text

        template = await author_footer_template(self.bot, guild_id)
        return author_footer_text(template, user_mention, timestamp)

    #: core update ---------------------------------------------------------

    async def _update_starboard(
        self, channel: discord.TextChannel, message: discord.Message, stars: int
    ) -> None:
        config = await self._config(channel.guild.id)
        board = channel.guild.get_channel(config["channel"]) if config["channel"] else None
        if not isinstance(board, discord.TextChannel):
            return
        entry = await self.store.get_entry(channel.guild.id, message.id)
        jump_label = await self.bot.translator.t(channel.guild.id, "starboard.jump")
        no_content_label = await self.bot.translator.t(
            channel.guild.id, "starboard.no_content"
        )

        async with self._lock_for(message.id):
            if stars == 0 and entry is not None:
                await self._delete_post(board, entry)
                await self.store.remove(channel.guild.id, message.id)
                return

            if entry is not None:
                last = self._last_edit.get(message.id, 0.0)
                if time.monotonic() - last < EDIT_COOLDOWN_SECONDS:
                    return
                try:
                    post = await board.fetch_message(entry["post_id"])
                except discord.NotFound:
                    entry = None
                else:
                    card = await self._card_view(
                        channel.guild.id, message, stars, jump_label, no_content_label
                    )
                    await post.edit(**card.message_kwargs(),
                                    allowed_mentions=discord.AllowedMentions.none())
                    self._last_edit[message.id] = time.monotonic()
                    await self.store.update_stars(channel.guild.id, message.id, stars)
                    return

            if stars >= config["threshold"]:
                card = await self._card_view(
                    channel.guild.id, message, stars, jump_label, no_content_label
                )
                post = await board.send(
                    **card.message_kwargs(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.store.upsert(
                    channel.guild.id,
                    message.id,
                    post_id=post.id,
                    stars=stars,
                    channel_id=channel.id,
                    author_id=message.author.id,
                )

    async def _delete_post(self, board: discord.TextChannel, entry: dict) -> None:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            await board.get_partial_message(entry["post_id"]).delete()

    #: listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        config = await self._config(payload.guild_id)
        star = self.bot.theme.emoji("star") or "⭐"
        if not config["enabled"] or str(payload.emoji) != star:
            return
        if config["channel"] and payload.channel_id == config["channel"]:
            return
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if message.author.bot and not config["allow_bots"]:
            return
        reaction = next(
            (r for r in message.reactions if str(r.emoji) == star), None
        )
        if reaction is None:
            return
        stars = await self._effective_stars(
            reaction,
            author_id=message.author.id,
            self_star_counts=config["self_star"],
        )
        try:
            await self._update_starboard(channel, message, stars)
        except Exception as exc:
            log.error("Starboard update failed: %s", exc)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        entry = await self.store.get_entry(payload.guild_id, payload.message_id)
        if entry is None:
            return
        config = await self._config(payload.guild_id)
        board = (
            self.bot.get_channel(config["channel"])
            if config["channel"]
            else None
        )
        if isinstance(board, discord.TextChannel):
            await self._delete_post(board, entry)
        await self.store.remove(payload.guild_id, payload.message_id)


#: default builder -----------------------------------------------------------

#: Star counts where a higher ``star_tier_*`` style kicks in (theme-defined).
#: Only used when the theme has no continuous ``star_ramp`` (legacy fallback).
_TIER_THRESHOLDS = (1, 2, 3, 5, 8, 13)


def _tier_style_for(theme, stars: int) -> str:
    """The highest defined ``star_tier_*`` style the count reaches."""
    for threshold in sorted(_TIER_THRESHOLDS, reverse=True):
        if stars >= threshold and f"star_tier_{threshold}" in theme.styles:
            return f"star_tier_{threshold}"
    return "star_tier_1"


def _ramp_hex(theme, stars: int) -> str:
    """Accent color for a star count as ``#rrggbb`` (documents speak hex):
    continuous ramp when the theme has one, else the tier style token, which
    resolves through the theme palette (guild themes without ``star_ramp``
    keep working)."""
    if theme.star_ramp_config() is not None:
        return theme.star_ramp(stars)
    return f"#{theme.color(theme.style(_tier_style_for(theme, stars)).color).value:06X}"


def _ramp_color(theme, stars: int) -> discord.Colour:
    """Same ramp as :func:`_ramp_hex`, resolved to a ``discord.Colour``."""
    return theme.color(theme.style(_tier_style_for(theme, stars)).color) if (
        theme.star_ramp_config() is None
    ) else discord.Colour(int(theme.star_ramp(stars).lstrip("#"), 16))


def _ramp_emoji(theme, stars: int) -> str:
    """Title emoji for a star count (milestone map, plain star as fallback)."""
    return theme.star_title_emoji(stars)


async def default_starboard_document(bot, guild_id: int, **variables) -> dict:
    """Full featured default for ``starboard.card`` (no theme override).

    The tiered container wraps: the tiered title beside the author's avatar
    thumbnail, the attachment gallery (skipped when ``{image_url}`` resolves
    empty: no image in the starred message), a divider and the standard
    author footer (``card.author_footer.text``). Placeholders stay literal:
    this is a template.
    """
    from rosemary.core.cards import author_footer_blocks

    stars = int(variables.get("stars") or 1)
    footer = await author_footer_blocks(bot, guild_id)
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": _ramp_hex(bot.theme, stars),
                "children": [
                    {
                        "type": "section",
                        "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
                        "children": [
                            {"type": "text", "body": "# {title}"},
                            {"type": "text", "body": "{body}"},
                        ],
                    },
                    {"type": "gallery", "urls": ["{image_url}"]},
                    *footer,
                ],
            }
        ],
    }


def _register_default_builder() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("starboard.card", default_starboard_document)


_register_default_builder()


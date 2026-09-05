"""Starboard: highlight popular messages on a dedicated board.

Reactions with the theme star emoji are counted (with an optional self-star
rule and bot filter); once the configured threshold is reached the message is
published as a V2 card whose accent color climbs through ``star_tier_*``
theme styles. Edits are debounced per message; deleting an original removes
its post. Nothing here is customizable through /customize — the card mirrors
someone else's message — but every knob lives in /settings.
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

    _TIER_THRESHOLDS = (1, 2, 3, 5, 8, 13)

    def _tier_style(self, stars: int) -> str:
        for threshold in sorted(self._TIER_THRESHOLDS, reverse=True):
            if stars >= threshold and f"star_tier_{threshold}" in self.bot.theme.styles:
                return f"star_tier_{threshold}"
        return "star_tier_1"

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

    # -- counting ------------------------------------------------------------

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

    # -- rendering -----------------------------------------------------------

    def _build_card(
        self,
        message: discord.Message,
        stars: int,
        jump_label: str,
        no_content_label: str,
    ) -> discord.ui.DesignerView:
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        star = theme.emoji("star")
        channel_mention = getattr(message.channel, "mention", f"#{message.channel}")
        title = f"{star} {stars} • {channel_mention}"
        body = message.content or no_content_label

        view = DesignerView(store=False)
        items: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=title)),
            TextDisplay(body),
        ]
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
                *items[:2],
                accessory=discord.ui.Thumbnail(message.author.display_avatar.url),
            )
        ]
        if image_url is not None:
            container_items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)))
        container_items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label=jump_label,
                    url=message.jump_url,
                )
            )
        )
        color_token = theme.style(self._tier_style(stars)).color
        view.add_item(designer_container(theme.color(color_token), *container_items))
        return view

    # -- core update ---------------------------------------------------------

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
                    card = self._build_card(message, stars, jump_label, no_content_label)
                    await post.edit(view=card, allowed_mentions=discord.AllowedMentions.none())
                    self._last_edit[message.id] = time.monotonic()
                    await self.store.update_stars(channel.guild.id, message.id, stars)
                    return

            if stars >= config["threshold"]:
                post = await board.send(
                    view=self._build_card(message, stars, jump_label, no_content_label),
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

    # -- listeners -----------------------------------------------------------

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


def setup(bot) -> None:
    bot.add_cog(StarboardCog(bot))

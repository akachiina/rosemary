"""Info commands: ``/userinfo``, ``/emojiinfo``, ``/roleinfo`` and ``/channelinfo``.

All are first-class CardSpecs (``utility.userinfo`` and friends): a theme may
restyle them, and ``debug.card_paths`` traces the path like any other send.
The builders below are the *default* layouts used when the theme has no
override, mirroring the ``/serverinfo`` builder shape (header section with a
thumbnail accessory, ``**label:**`` value stat lines, optional blocks omitted
when their variable resolves empty, small ID footer).

This file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations
would break option parsing at runtime (same reason as ``cogs/invites.py``).
"""

import logging

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload, render_card_message

log = logging.getLogger(__name__)


def _ts(moment, style: str) -> str:
    """Discord timestamp for an aware datetime, ``"-"`` when missing."""
    if moment is None:
        return "-"
    return f"<t:{int(moment.timestamp())}:{style}>"


def _compose(bot, guild_id: int, card_key: str, variables: dict):
    """Closure bundle for a default builder (serverinfo shape).

    ``raw`` formats one catalog key, ``line`` builds one ``**label:** value``
    stat line (an optional ``<name>_value`` template wins over the plain
    variable), ``optional`` renders the line only when its gate variable
    carries content (defaults to the line's own name), and ``subtitle``
    resolves the card's optional subtitle.
    """
    from rosemary.core.cards import safe_format

    mapping = {
        **bot.theme.emojis,
        **{name: f"{{{name}}}" for name in variables},
    }

    async def raw(key: str) -> str:
        return safe_format(
            await bot.translator.raw(guild_id, f"card.{card_key}.{key}"), mapping
        )

    async def line(name: str) -> str:
        label = await raw(f"{name}_label")
        value_key = f"{name}_value"
        full_key = f"card.{card_key}.{value_key}"
        template = await bot.translator.raw(guild_id, full_key)
        value = template if template != full_key else f"{{{name}}}"
        return f"**{label}:** {value}"

    async def optional(name: str, *, gate: str | None = None):
        value = variables.get(gate or name)
        if isinstance(value, str) and value.strip():
            return await line(name)
        return None

    async def subtitle():
        found = await bot.translator.raw(guild_id, f"card.{card_key}.subtitle")
        if isinstance(found, str) and found.strip() and found != f"card.{card_key}.subtitle":
            return safe_format(found, mapping)
        return None

    return raw, line, optional, subtitle


def _header_children(card_key: str, variables: dict, header_texts: list[str]) -> list[dict]:
    """Header section (thumbnail accessory) when the image exists, plain texts
    otherwise: a thumbnail with an empty url would 400 the whole payload."""
    image_key = {
        "utility.userinfo": "user_avatar",
        "utility.emojiinfo": "emoji_url",
        "utility.roleinfo": "role_icon",
    }.get(card_key)
    image = variables.get(image_key) if image_key else None
    if image_key and isinstance(image, str) and image.strip():
        return [
            {
                "type": "section",
                "accessory": {"type": "thumbnail", "url": f"{{{image_key}}}"},
                "children": [{"type": "text", "body": text} for text in header_texts],
            }
        ]
    return [{"type": "text", "body": text} for text in header_texts]


async def default_userinfo_document(bot, guild_id, **variables) -> dict:
    """Catalog-default ``/userinfo`` card (no theme override)."""
    _raw, line, optional, subtitle = _compose(bot, guild_id, "utility.userinfo", variables)

    header_texts = ["# {user_name}"]
    sub = await subtitle()
    if sub:
        header_texts.append(sub)
    children = _header_children("utility.userinfo", variables, header_texts)
    if isinstance(variables.get("banner_url"), str) and variables["banner_url"].strip():
        # Banner right after the header: V2 renders in document order, so
        # appending later would drop the image at the card's bottom.
        children.append({"type": "gallery", "urls": ["{banner_url}"]})
    children.append({"type": "divider"})
    lines = [
        await line("created"),
        await line("joined"),
        await optional("nickname"),
        await line("roles"),
        await optional("boosting_since"),
        await line("timeout"),
        await line("is_bot"),
    ]
    children.append({"type": "text", "body": "\n".join(ln for ln in lines if ln)})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {user_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


async def default_emojiinfo_document(bot, guild_id, **variables) -> dict:
    """Catalog-default ``/emojiinfo`` card (custom emoji or unicode glyph)."""
    _raw, line, optional, subtitle = _compose(bot, guild_id, "utility.emojiinfo", variables)

    header_texts = ["# {emoji}"]
    sub = await subtitle()
    if sub:
        header_texts.append(sub)
    children = _header_children("utility.emojiinfo", variables, header_texts)
    children.append({"type": "divider"})
    # Source lives in the subtitle and the id in the footer: the body only
    # carries the optional created/animated lines (unicode drops both).
    lines = [
        await optional("created", gate="created_at"),
        await optional("animated"),
    ]
    body = "\n".join(ln for ln in lines if ln)
    if body:
        children.append({"type": "text", "body": body})
    if variables.get("emoji_id"):
        children.append({"type": "divider"})
        children.append({"type": "text", "body": "-# {emoji_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


async def default_stickerinfo_document(bot, guild_id, **variables) -> dict:
    """Catalog-default sticker card: the image rides a gallery (a lottie
    sticker's ``sticker_url`` resolves empty, so the gallery drops itself)."""
    _raw, line, optional, _subtitle = _compose(bot, guild_id, "utility.stickerinfo", variables)

    children: list[dict] = [{"type": "text", "body": "# {sticker_name}"}]
    if isinstance(variables.get("sticker_url"), str) and variables["sticker_url"].strip():
        children.append({"type": "gallery", "urls": ["{sticker_url}"]})
    children.append({"type": "divider"})
    lines = [
        await line("format"),
        await optional("tags"),
        await optional("uploader"),
        await optional("description"),
        await line("created"),
    ]
    children.append({"type": "text", "body": "\n".join(ln for ln in lines if ln)})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {sticker_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


async def default_roleinfo_document(bot, guild_id, **variables) -> dict:
    """Catalog-default ``/roleinfo`` card (no theme override)."""
    _raw, line, optional, subtitle = _compose(bot, guild_id, "utility.roleinfo", variables)

    header_texts = ["# {role_name}"]
    sub = await subtitle()
    if sub:
        header_texts.append(sub)
    children = _header_children("utility.roleinfo", variables, header_texts)
    children.append({"type": "divider"})
    lines = [
        await line("members"),
        await line("position"),
        await optional("role_color"),
        await line("mentionable"),
        await line("hoist"),
        await line("created"),
    ]
    children.append({"type": "text", "body": "\n".join(ln for ln in lines if ln)})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {role_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


async def default_channelinfo_document(bot, guild_id, **variables) -> dict:
    """Catalog-default ``/channelinfo`` card (no image; voice-only stats are
    optional variables that resolve empty on text channels)."""
    _raw, line, optional, _subtitle = _compose(bot, guild_id, "utility.channelinfo", variables)

    children: list[dict] = [{"type": "text", "body": "# {channel_name}"}]
    children.append({"type": "divider"})
    lines = [
        await line("channel_type"),
        await optional("category"),
        await optional("topic"),
        await optional("slowmode"),
        await optional("bitrate"),
        await optional("user_limit"),
        await line("nsfw"),
        await line("position"),
        await line("created"),
    ]
    children.append({"type": "text", "body": "\n".join(ln for ln in lines if ln)})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {channel_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


def _register_default_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("utility.userinfo", default_userinfo_document)
    set_default_builder("utility.emojiinfo", default_emojiinfo_document)
    set_default_builder("utility.stickerinfo", default_stickerinfo_document)
    set_default_builder("utility.roleinfo", default_roleinfo_document)
    set_default_builder("utility.channelinfo", default_channelinfo_document)


_register_default_builders()


class InfoCog(commands.Cog):
    """Member, emoji, sticker, role and channel info commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def _fallback_payload(self, title: str) -> CardPayload:
        """Defensive plain card when the default builder itself fails."""
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("brand"),
                TextDisplay(self.bot.theme.md("title", title=title)),
            )
        )
        return CardPayload(view=view)

    @discord.slash_command(
        name="userinfo",
        description="Show member information",
        contexts={discord.InteractionContextType.guild},
    )
    async def userinfo(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(
            discord.Member, description="Member to inspect", required=False
        ),
    ) -> None:
        """Show member information as the rich default card (or a theme override)."""
        await ctx.defer()
        target = member or ctx.author
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        timeout_dt = _timeout_until(target)
        timeout_value = (
            f"<t:{int(timeout_dt.timestamp())}:R>"
            if timeout_dt is not None
            else await t(guild_id, "userinfo.timeout_none")
        )
        banner_url = ""
        guild_banner = getattr(target, "guild_banner", None)
        if guild_banner is not None and guild_banner.url:
            banner_url = guild_banner.url
        else:
            try:
                fetched = await self.bot.fetch_user(target.id)
                fetched_banner = getattr(fetched, "banner", None)
                if fetched_banner is not None:
                    banner_url = fetched_banner.url
            except discord.HTTPException as exc:
                log.warning("Could not fetch banner for %s: %s", target.id, exc)
        variables = {
            "user": target.mention,
            "user_name": target.display_name,
            "user_avatar": target.display_avatar.url,
            "banner_url": banner_url,
            "nickname": target.nick or "",
            "created_at": _ts(target.created_at, "D"),
            "created_rel": _ts(target.created_at, "R"),
            "joined_at": _ts(target.joined_at, "D"),
            "joined_rel": _ts(target.joined_at, "R"),
            "roles": len([r for r in target.roles if r != ctx.guild.default_role]),
            # @everyone has no mention pill (its <@&guild_id> token renders
            # oddly and duplicates with the subtitle): plain text instead.
            "top_role": target.top_role.mention
            if not target.top_role.is_default()
            else "@everyone",
            "boosting_since": _ts(target.premium_since, "R")
            if target.premium_since
            else "",
            "timeout": timeout_value,
            "is_bot": await t(guild_id, "general.yes" if target.bot else "general.no"),
            "user_id": str(target.id),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "utility.userinfo", variables
        )
        if payload is None:
            payload = self._fallback_payload(
                await t(guild_id, "card.utility.userinfo.title")
            )
        await ctx.followup.send(**payload.message_kwargs(), allowed_mentions=allowed)

    @discord.slash_command(
        name="emojiinfo",
        description="Show emoji or sticker information",
        contexts={discord.InteractionContextType.guild},
    )
    async def emojiinfo(
        self,
        ctx: discord.ApplicationContext,
        emoji: discord.Option(str, description="Custom or unicode emoji, or a sticker ID"),
    ) -> None:
        """Inspect a custom emoji, a unicode glyph or a sticker by ID."""
        value = (emoji or "").strip()
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        if not value:
            return await ctx.respond(
                await t(guild_id, "emojiinfo.error"), ephemeral=True
            )
        if value.startswith("<") and value.endswith(">"):
            partial = discord.PartialEmoji.from_str(value)
            if partial.id is None:
                return await ctx.respond(
                    await t(guild_id, "emojiinfo.error"), ephemeral=True
                )
            resolved = await self._resolve_emoji(ctx, partial)
            if resolved is None:
                return await ctx.respond(
                    await t(guild_id, "emojiinfo.not_found"), ephemeral=True
                )
            return await self._respond_emoji(ctx, resolved)
        if value.isdigit():
            return await self._respond_sticker(ctx, int(value))
        # Unicode glyphs never carry < or >: anything shaped like an emoji
        # mention that failed from_str is garbage input.
        if "<" in value or ">" in value or len(value) > 32:
            return await ctx.respond(
                await t(guild_id, "emojiinfo.error"), ephemeral=True
            )
        # Unicode glyph (or any short text): no id, no origin, no fetch.
        return await self._respond_emoji(
            ctx, _UnicodeEmoji(value), fetch_origin=False
        )

    async def _resolve_emoji(self, ctx, partial: discord.PartialEmoji):
        """Cache -> guild -> application cascade; ``None`` when unresolvable."""
        resolved = self.bot.get_emoji(partial.id)
        if resolved is not None:
            return resolved
        try:
            return await ctx.guild.fetch_emoji(partial.id)
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            return await self.bot.fetch_emoji(partial.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Expected flow for dead emojis: the card renders from the partial
            # alone ("unknown origin"), so this is info, not a warning.
            log.info("Emoji %s not resolvable: %s", partial.id, exc)
            return _PartialRef(partial)

    async def _respond_emoji(self, ctx, emoji, fetch_origin: bool = True) -> None:
        """Render the emojiinfo card for one resolved emoji-like object."""
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        guild = getattr(emoji, "guild", None)
        if not fetch_origin:
            source = ""
        elif guild is not None:
            source = await t(guild_id, "emojiinfo.source_guild", server=guild.name)
        elif isinstance(emoji, _PartialRef):
            source = await t(guild_id, "emojiinfo.source_unknown")
        else:
            source = await t(guild_id, "emojiinfo.source_app")
        created = getattr(emoji, "created_at", None)
        if created is None and getattr(emoji, "id", None):
            created = discord.utils.snowflake_time(emoji.id)
        animated = bool(getattr(emoji, "animated", False))
        has_id = bool(getattr(emoji, "id", None))
        variables = {
            "emoji": str(emoji),
            "emoji_name": emoji.name,
            "emoji_id": str(emoji.id) if has_id else "",
            # A unicode glyph carries no id: the animated/created lines drop
            # themselves instead of rendering noise.
            "animated": (await t(guild_id, "general.yes" if animated else "general.no"))
            if has_id
            else "",
            "emoji_url": emoji.url,
            "source": source,
            "created_at": _ts(created, "D") if created else "",
            "created_rel": _ts(created, "R") if created else "",
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "utility.emojiinfo", variables
        )
        if payload is None:
            payload = self._fallback_payload(
                await t(guild_id, "card.utility.emojiinfo.title")
            )
        await ctx.respond(**payload.message_kwargs(), allowed_mentions=allowed)

    async def _respond_sticker(self, ctx, sticker_id: int) -> None:
        """Sticker by ID; a sticker miss falls back to the app-emoji lookup."""
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        try:
            sticker = await ctx.guild.fetch_sticker(sticker_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Sticker %s fetch failed: %s", sticker_id, exc)
            try:
                app_emoji = await self.bot.fetch_emoji(sticker_id)
            except (discord.Forbidden, discord.HTTPException):
                return await ctx.respond(
                    await t(guild_id, "emojiinfo.not_found"), ephemeral=True
                )
            return await self._respond_emoji(ctx, app_emoji)
        uploader = getattr(sticker, "user", None)
        is_lottie = sticker.format.name == "lottie"
        created = discord.utils.snowflake_time(sticker.id)
        variables = {
            "sticker_name": sticker.name,
            "sticker_id": str(sticker.id),
            "description": sticker.description or "",
            "format": sticker.format.name.upper(),
            "tags": ", ".join(sticker.tags) if sticker.tags else "",
            "uploader": uploader.mention if uploader else "",
            # Lottie is a JSON animation: no raster image to show.
            "sticker_url": "" if is_lottie else sticker.url,
            "created_at": _ts(created, "D"),
            "created_rel": _ts(created, "R"),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "utility.stickerinfo", variables
        )
        if payload is None:
            payload = self._fallback_payload(
                await t(guild_id, "card.utility.stickerinfo.title")
            )
        await ctx.respond(**payload.message_kwargs(), allowed_mentions=allowed)

    @discord.slash_command(
        name="roleinfo",
        description="Show role information",
        contexts={discord.InteractionContextType.guild},
    )
    async def roleinfo(
        self,
        ctx: discord.ApplicationContext,
        role: discord.Option(discord.Role, description="Role to inspect", required=False),
    ) -> None:
        """Show role information (defaults to the author's top role)."""
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        target = role or ctx.author.top_role
        color_value = (
            "" if target.colour == discord.Colour.default() else str(target.colour)
        )
        icon = target.icon
        variables = {
            "role": target.mention,
            "role_name": target.name,
            "role_color": color_value,
            "members": len(target.members),
            "position": target.position,
            "mentionable": await t(
                guild_id, "general.yes" if target.mentionable else "general.no"
            ),
            "hoist": await t(
                guild_id, "general.yes" if target.hoist else "general.no"
            ),
            "role_icon": icon.url if icon else "",
            "role_id": str(target.id),
            "created_at": _ts(target.created_at, "D"),
            "created_rel": _ts(target.created_at, "R"),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "utility.roleinfo", variables
        )
        if payload is None:
            payload = self._fallback_payload(
                await t(guild_id, "card.utility.roleinfo.title")
            )
        await ctx.respond(**payload.message_kwargs(), allowed_mentions=allowed)

    @discord.slash_command(
        name="channelinfo",
        description="Show channel information",
        contexts={discord.InteractionContextType.guild},
    )
    async def channelinfo(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(
            discord.abc.GuildChannel, description="Channel to inspect", required=False
        ),
    ) -> None:
        """Show channel information (defaults to the current channel)."""
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        target = channel or ctx.channel
        type_key = f"channelinfo.types.{target.type.name}"
        type_value = await t(guild_id, type_key)
        if type_value == type_key:
            type_value = target.type.name.replace("_", " ").capitalize()
        category = getattr(target, "category", None)
        slowmode = getattr(target, "slowmode_delay", 0) or 0
        bitrate = getattr(target, "bitrate", None)
        user_limit = getattr(target, "user_limit", None)
        created = discord.utils.snowflake_time(target.id)
        variables = {
            "channel": getattr(target, "mention", f"<#{target.id}>"),
            "channel_name": target.name,
            "channel_type": type_value,
            "topic": getattr(target, "topic", None) or "",
            "category": category.name if category is not None else "",
            "nsfw": await t(
                guild_id,
                "general.yes" if getattr(target, "nsfw", False) else "general.no",
            ),
            "slowmode": f"{slowmode}s" if slowmode else "",
            "bitrate": f"{bitrate // 1000}kbps" if bitrate else "",
            "user_limit": str(user_limit) if user_limit else "",
            "position": target.position,
            "channel_id": str(target.id),
            "created_at": _ts(created, "D"),
            "created_rel": _ts(created, "R"),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "utility.channelinfo", variables
        )
        if payload is None:
            payload = self._fallback_payload(
                await t(guild_id, "card.utility.channelinfo.title")
            )
        await ctx.respond(**payload.message_kwargs(), allowed_mentions=allowed)


class _PartialRef:
    """Emoji-like view over an unresolvable PartialEmoji (deleted or unknown
    origin): keeps the card renderable from name/id/animated/url alone."""

    def __init__(self, partial: discord.PartialEmoji) -> None:
        self._partial = partial
        self.name = partial.name
        self.id = partial.id
        self.animated = partial.animated
        self.url = partial.url
        self.guild = None

    def __str__(self) -> str:
        return str(self._partial)


class _UnicodeEmoji:
    """Emoji-like view over a unicode glyph (no id, no origin, no url)."""

    def __init__(self, glyph: str) -> None:
        self.name = glyph
        self.id = ""
        self.animated = False
        self.url = ""
        self.guild = None

    def __str__(self) -> str:
        return self.name


def _timeout_until(member):
    """Timeout expiry via the audit cog's py-cord vs discord.py-safe resolver."""
    from rosemary.cogs.audit import _timeout_until as resolve

    return resolve(member)

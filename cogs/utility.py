"""Utility commands: ``/ping`` and ``/serverinfo`` (Components V2 cards).

Both are first-class CardSpecs (``utility.ping`` / ``utility.serverinfo``):
a theme may restyle them, and ``debug.card_paths`` traces the path like any
other send. The code below is the *default* layout used when the theme has
no override.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload, render_card_message
from rosemary.ui.containers import DesignerView, TextDisplay, designer_container


async def default_serverinfo_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``/info_servidor`` card (no theme override).

    Layout: brand container with the server name beside the icon thumbnail
    (the single accessory -- V2 sections stack their texts, so stats live in
    a full-width ``**label:** value`` block below, owner/member carried by
    the subtitle), the banner as a full-width media gallery and a small
    ID footer. Content-reactive
    blocks (description, banner) read the real send-site variables and are
    omitted when they resolve empty; other placeholders stay literal so the
    document doubles as the theme template.
    """
    from rosemary.core.cards import safe_format

    # Theme emojis resolve in the builder (labels, subtitle) while every
    # contracted placeholder self-echoes: the document stays a literal
    # template and {members} is not swallowed by the members emoji token
    # at build time -- the render resolves it with the real variables.
    mapping = {
        **bot.theme.emojis,
        **{name: f"{{{name}}}" for name in variables},
    }

    async def raw(key: str) -> str:
        return safe_format(
            await bot.translator.raw(guild_id, f"card.utility.serverinfo.{key}"), mapping
        )

    async def line(name: str) -> str:
        """One ``**label:** value`` stat line (the bot-wide format); optional
        composed value templates (e.g. channels: total + per-type split) win
        over the plain variable (``raw`` echoes the full key when absent)."""
        label = await raw(f"{name}_label")
        value_key = f"{name}_value"
        full_key = f"card.utility.serverinfo.{value_key}"
        template = await bot.translator.raw(guild_id, full_key)
        value = template if template != full_key else f"{{{name}}}"
        return f"**{label}:** {value}"

    # The heading is the server name itself, bare (no emoji decoration) per
    # the visual standard for this card. Single icon: the header accessory.
    # Owner/member live in the subtitle only -- repeating them as stat lines
    # is the clutter this layout avoids.
    heading = "# {server}"
    header_texts = [heading, await raw("subtitle")]
    description = safe_format("{description}", mapping)
    if isinstance(variables.get("description"), str) and variables["description"].strip():
        header_texts.append(description)
    children: list[dict] = [
        {
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "{server_icon}"},
            "children": [
                {"type": "text", "body": text} for text in header_texts
            ],
        },
        {"type": "divider"},
        {
            "type": "text",
            "body": "\n".join(
                [
                    await line("created"),
                    await line("channels"),
                    await line("roles"),
                    await line("boosts"),
                    " · ".join(
                        [
                            await line("emojis"),
                            await line("stickers"),
                            await line("verification"),
                        ]
                    ),
                ]
            ),
        },
    ]
    if isinstance(variables.get("banner_url"), str) and variables["banner_url"].strip():
        children.append({"type": "gallery", "urls": ["{banner_url}"]})
    children.append({"type": "divider"})
    children.append({"type": "text", "body": "-# {server_id}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


def _register_default_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("utility.serverinfo", default_serverinfo_document)


_register_default_builders()


class UtilityCog(commands.Cog):
    """Latency and server diagnostics."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="ping",
        description="Show the bot latency",
        contexts={discord.InteractionContextType.guild},
    )
    async def ping(self, ctx: discord.ApplicationContext) -> None:
        """Show the gateway latency."""
        latency = getattr(self.bot, "latency", None)
        ms = f"{latency * 1000:.0f}" if latency is not None else "-"
        payload, _allowed = await render_card_message(
            self.bot, ctx.guild_id, "utility.ping", {"ms": ms}
        )
        if payload is None:
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    self.bot.theme.color("info"),
                    TextDisplay(
                        self.bot.theme.md(
                            "title",
                            title=await self.bot.translator.t(ctx.guild_id, "ping.title"),
                        )
                    ),
                    TextDisplay(
                        await self.bot.translator.t(ctx.guild_id, "ping.latency", ms=ms)
                    ),
                )
            )
            payload = CardPayload(view=view)
        await ctx.respond(**payload.message_kwargs(), ephemeral=True)

    @discord.slash_command(
        name="serverinfo",
        description="Show server information",
        contexts={discord.InteractionContextType.guild},
    )
    async def serverinfo(self, ctx: discord.ApplicationContext) -> None:
        """Show guild information as the rich default card (or a theme override)."""
        guild = ctx.guild
        created = (
            f"<t:{int(guild.created_at.timestamp())}:D>" if guild.created_at else "-"
        )
        created_rel = (
            f"<t:{int(guild.created_at.timestamp())}:R>" if guild.created_at else "-"
        )
        t = self.bot.translator.t
        verification = await t(
            ctx.guild_id,
            f"card.utility.serverinfo.verification.{guild.verification_level.name}",
        )
        variables = {
            "server": guild.name,
            "owner": f"<@{guild.owner_id}>" if guild.owner_id else "-",
            "members": guild.member_count or 0,
            "roles": len(guild.roles),
            "channels": len(guild.text_channels) + len(guild.voice_channels),
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "boosts": guild.premium_subscription_count or 0,
            "emojis": len(guild.emojis),
            "stickers": len(guild.stickers),
            "verification": verification,
            "description": guild.description or "",
            "server_icon": guild.icon.url if guild.icon else "",
            "banner_url": guild.banner.url if guild.banner else "",
            "created_at": created,
            "created_rel": created_rel,
            "server_id": guild.id,
            "title": await t(ctx.guild_id, "serverinfo.title"),
            "body": "",
        }
        payload, _allowed = await render_card_message(
            self.bot, ctx.guild_id, "utility.serverinfo", variables
        )
        if payload is None:
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    self.bot.theme.color("brand"),
                    TextDisplay(self.bot.theme.md("title", title=guild.name)),
                )
            )
            payload = CardPayload(view=view)
        await ctx.respond(**payload.message_kwargs())

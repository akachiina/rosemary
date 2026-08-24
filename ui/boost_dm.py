"""Plain-text DMs for boost roles, with a themed role-preview card.

Messages are sent as regular text (markdown from the language catalogs, with
emojis injected by the theme) so users can read and copy them anywhere. When a
role is involved, a single preview card is appended as its own message, showing
the role icon, name, owner and member count. No embeds anywhere.
"""

from __future__ import annotations

from typing import Any

import discord

from rosemary.core.cards import maybe_text, maybe_view
from rosemary.core.settings import get_setting
from rosemary.ui.containers import TextDisplay, designer_container


async def send(
    bot,
    destination,
    guild_id: int,
    key: str,
    **variables: Any,
) -> discord.Message:
    """Send a translated plain-text DM, honoring a card override when set."""
    # Plain documents stay plain messages; rich compositions go out as V2.
    override = await maybe_text(bot, guild_id, key, **variables)
    if override is not None:
        return await destination.send(override)
    override_view = await maybe_view(bot, guild_id, key, variables)
    if override_view is not None:
        return await destination.send(view=override_view)
    content = await bot.translator.t(guild_id, key, **variables)
    return await destination.send(content)


async def preview_view(
    bot,
    guild_id: int,
    role: discord.Role,
    data: dict[str, Any],
) -> discord.ui.DesignerView:
    """Card with the role icon, name, owner and member count."""
    t = bot.translator.t
    theme = bot.theme
    style = theme.style("boost_preview")
    max_members = await get_setting(bot.storage, guild_id, "boost.max_members")
    items: list[discord.ui.ViewItem] = []
    role_name = TextDisplay(theme.md("title", title=f"@{role.name}"))
    if role.icon is not None:
        items.append(
            discord.ui.Section(
                role_name,
                accessory=discord.ui.Thumbnail(role.icon.url),
            )
        )
    else:
        items.append(role_name)
    items.append(
        TextDisplay(
            theme.md(
                "entry",
                label=await t(guild_id, "boost.emoji.owner"),
                value=f"<@{data.get('owner_id', 0)}>",
            )
        )
    )
    items.append(
        TextDisplay(
            theme.md(
                "entry",
                label=await t(guild_id, "boost.emoji.members"),
                value=await t(
                    guild_id,
                    "boost.descriptions.members_count",
                    count=len(data.get("members", [])),
                    max_members=max_members,
                ),
            )
        )
    )
    view = discord.ui.DesignerView(store=False)
    view.add_item(designer_container(theme.color(style.color), *items))
    return view


async def send_preview(
    bot,
    destination,
    guild_id: int,
    role: discord.Role,
    data: dict[str, Any],
) -> None:
    """Send the role-preview card as its own message."""
    await destination.send(view=await preview_view(bot, guild_id, role, data))

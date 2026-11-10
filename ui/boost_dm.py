"""Plain-text DMs for boost roles, with a themed role-preview card.

Messages are sent as regular text (markdown from the language catalogs, with
emojis injected by the theme) so users can read and copy them anywhere. When a
role is involved, a single preview card is appended as its own message, showing
the role icon, name, owner and member count. No embeds anywhere.
"""

from __future__ import annotations

from typing import Any

import discord

from rosemary.core.settings import get_setting


async def send(
    bot,
    destination,
    guild_id: int,
    key: str,
    *,
    default_key: str | None = None,
    **variables: Any,
) -> discord.Message:
    """Send a translated DM, honoring the card's form and the theme.

    The card pipeline resolves the copy (the guild's override first, then the
    catalog default) and the shape: a text card goes out as one notice line
    (the theme prefix plus the copy), an embed card as a classic embed and a
    Components V2 override as a view. A card with no default at all falls
    back to the raw catalog copy.

    ``default_key`` translates the fallback copy when the card key itself is
    a catalog section (``trap.dm`` -> ``trap.dm.text``); without it the card
    key is translated directly.
    """
    from rosemary.core.card_service import render_card_message

    payload, _allowed = await render_card_message(bot, guild_id, key, variables)
    if payload is not None:
        # A DM never pings: the mention policy belongs to the guild
        # messages, so the allowed-mentions bundle stays out.
        return await destination.send(**payload.message_kwargs())
    content = await bot.translator.t(guild_id, default_key or key, **variables)
    return await destination.send(content)


async def send_preview(
    bot,
    destination,
    guild_id: int,
    role: discord.Role,
    data: dict[str, Any],
) -> None:
    """Send the role-preview card as its own message (in the card's form)."""
    from rosemary.core.card_service import render_card_message, simple_payload

    max_members = await get_setting(bot.storage, guild_id, "boost.max_members")
    variables = {
        "role_name": role.name,
        "owner": f"<@{data.get('owner_id', 0)}>",
        "count": len(data.get("members", [])),
        "max_members": max_members,
    }
    payload, _allowed = await render_card_message(
        bot, guild_id, "boost.preview", variables
    )
    if payload is None:
        # No document resolved: the fallback follows the card's own form.
        payload = simple_payload(
            bot,
            guild_id,
            "boost.preview",
            title=f"@{role.name}",
            body=(
                f"**{await bot.translator.t(guild_id, 'boost.emoji.owner')}:** "
                f"<@{data.get('owner_id', 0)}>\n"
                f"**{await bot.translator.t(guild_id, 'boost.emoji.members')}:** "
                f"{len(data.get('members', []))}/{max_members}"
            ),
            color="success",
            thumbnail=role.icon.url if role.icon is not None else None,
        )
    await destination.send(**payload.message_kwargs())

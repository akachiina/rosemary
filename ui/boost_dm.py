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
) -> discord.Message | None:
    """Send a translated DM, honoring the card's form and the theme.

    The card pipeline resolves the copy (the guild's override first, then the
    catalog default) and the shape: a text card goes out as one notice line
    (the theme prefix plus the copy), an embed card as a classic embed and a
    Components V2 override as a view. A card with no default at all falls
    back to the raw catalog copy.

    A themed sequence sends every message, in order: the topology of a DM is
    data too. The last message is returned (the call sites that only track
    delivery read it).

    ``default_key`` translates the fallback copy when the card key itself is
    a catalog section (``trap.dm`` -> ``trap.dm.text``); without it the card
    key is translated directly.
    """
    from rosemary.core.card_service import render_card_messages

    messages = await render_card_messages(bot, guild_id, key, variables)
    if messages:
        sent = None
        for message in messages:
            # A DM never pings: the mention policy belongs to the guild
            # messages, so the allowed-mentions bundle stays out and the copy
            # ships silent.
            sent = await destination.send(
                **message.payload.message_kwargs(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return sent
    content = await bot.translator.t(guild_id, default_key or key, **variables)
    return await destination.send(content, allowed_mentions=discord.AllowedMentions.none())


async def send_preview(
    bot,
    destination,
    guild_id: int,
    role: discord.Role,
    data: dict[str, Any],
) -> None:
    """Send the role-preview card (in the card's form and topology)."""
    from rosemary.core.card_payload import RenderedMessage
    from rosemary.core.card_service import render_card_messages, simple_payload

    max_members = await get_setting(bot.storage, guild_id, "boost.max_members")
    variables = {
        "role_name": role.name,
        "owner": f"<@{data.get('owner_id', 0)}>",
        "count": len(data.get("members", [])),
        "max_members": max_members,
    }
    messages = await render_card_messages(bot, guild_id, "boost.preview", variables)
    if not messages:
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
        messages = [
            RenderedMessage(
                payload=payload,
                allowed=discord.AllowedMentions.none(),
            )
        ]
    for message in messages:
        await destination.send(
            **message.payload.message_kwargs(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

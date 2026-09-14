"""Action buttons for customized cards (beyond plain links).

A row/accessory button may carry ``action`` instead of ``url``. Only the
closed registry below is allowed -- arbitrary callbacks can never come from
user-edited JSON. Persistent views are rebuilt from stored documents at boot
and after every editor save, so action buttons survive restarts.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

#: Closed action catalog. ``open_ticket`` needs a ticket type param.
ACTIONS: tuple[str, ...] = ("open_ticket", "dismiss")

INTERACTIVE_STYLES = ("primary", "secondary", "success", "danger")

_STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def button_style(name: Any) -> discord.ButtonStyle:
    """Map a stored style name, defaulting to primary."""
    return _STYLE_MAP.get(name, discord.ButtonStyle.primary)


def custom_id_for(card_key: str, button_id: str) -> str:
    """Stable persistent custom_id (Discord caps at 100 chars)."""
    return f"cardact:{card_key[:40]}:{button_id}"[:100]


def parse_custom_id(custom_id: str) -> tuple[str, str, str] | None:
    """Split ``cardact:<key>:<button>``; ``None`` when foreign."""
    try:
        prefix, key, button_id = custom_id.split(":", 2)
    except ValueError:
        return None
    if prefix != "cardact" or not key or not button_id:
        return None
    return ("cardact", key, button_id)


async def iter_action_buttons(bot, guild_id: int):
    """Yield ``(card_key, button)`` for every action button in themed docs.

    Covers both card forms: V2 docs (rows + section accessories) and embed
    docs (classic ``buttons:`` list beside the embed)."""
    from rosemary.core.themes import theme_store

    store = theme_store(bot)
    # Scan every theme visible to the guild (its own files first, then global).
    seen: set[str] = set()
    for name in store.list_all(guild_id):
        try:
            theme = store.load(name, guild_id=guild_id)
        except Exception:
            continue
        for key, doc in getattr(theme, "cards", {}).items():
            if key in seen or not isinstance(doc, dict):
                continue
            seen.add(key)
            if doc.get("kind") == "embed":
                for button in doc.get("buttons", []) or []:
                    if isinstance(button, dict) and button.get("action") in ACTIONS:
                        yield key, button
                continue
            for block in _walk_blocks(doc.get("blocks")):
                for button in block.get("buttons", []) or []:
                    if isinstance(button, dict) and button.get("action") in ACTIONS:
                        yield key, button
                accessory = block.get("accessory") or {}
                if isinstance(accessory, dict) and accessory.get("action") in ACTIONS:
                    yield key, accessory


def _walk_blocks(blocks: Any):
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        yield block
        yield from _walk_blocks(block.get("children"))


async def dispatch(bot, interaction: discord.Interaction) -> None:
    """Route one action-button click (ACKs everything)."""
    parsed = parse_custom_id(interaction.custom_id or "")
    if parsed is None:
        return
    _, key, button_id = parsed
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    button = None
    async for found_key, found in iter_action_buttons(bot, interaction.guild_id):
        if found_key == key and str(found.get("id")) == button_id:
            button = found
            break
    if button is None:
        return
    action = button.get("action")
    if action == "dismiss":
        message = getattr(interaction, "message", None)
        if message is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
    elif action == "open_ticket":
        await _open_ticket(bot, interaction, button)


async def _open_ticket(bot, interaction: discord.Interaction, button: dict) -> None:
    """Create a ticket from an action button (same rules as the panel)."""
    t = bot.translator.t
    cog = bot.get_cog("TicketsCog")
    guild = interaction.guild
    member = guild.get_member(interaction.user.id) if guild else None
    ticket_type = button.get("ticket_type") or "report"
    if cog is None or guild is None or member is None:
        await interaction.followup.send(
            await t(interaction.guild_id, "actions.unavailable"), ephemeral=True
        )
        return
    ok, detail = await cog.create_ticket(guild, member, ticket_type)
    if ok:
        channel = guild.get_channel(int(detail))
        message = await t(
            interaction.guild_id,
            "actions.ticket_created",
            channel=channel.mention if channel else detail,
        )
    else:
        message = await t(interaction.guild_id, detail)
    await interaction.followup.send(message, ephemeral=True)


async def sync_guild(bot, guild_id: int) -> None:
    """(Re)register this guild's action-button view; call after every save."""
    from rosemary.ui.menu import MenuView

    old = getattr(bot, "_card_action_views", {}).pop(guild_id, None)
    if old is not None:
        old.stop()
    view = MenuView(author_id=None, timeout=None)
    view.bot = bot
    view.guild_id = guild_id
    count = 0
    async for key, button in iter_action_buttons(bot, guild_id):
        item = discord.ui.Button(
            style=button_style(button.get("style")),
            label=str(button.get("label") or "")[:80] or "•",
            emoji=button.get("emoji") or None,
            custom_id=custom_id_for(key, str(button.get("id", ""))),
        )
        item.callback = dispatch_action
        if count == 0:
            view.add_item(discord.ui.ActionRow(item))
            count += 1
        else:
            # Spread across rows (5 buttons per row max).
            last = view.children[-1]
            if len(last.children) >= 5:
                view.add_item(discord.ui.ActionRow(item))
            else:
                last.add_item(item)
    if count == 0:
        return
    bot.add_view(view)
    if not hasattr(bot, "_card_action_views"):
        bot._card_action_views = {}
    bot._card_action_views[guild_id] = view


def bind_action_callbacks(items) -> None:
    """Attach dispatch to rendered action buttons (editor previews/tests).

    Sent message views rely on the boot-rebuilt persistent views instead;
    this only makes clicks work inside live editor previews.
    """
    for item in _walk_items(items):
        if (
            isinstance(item, discord.ui.Button)
            and str(getattr(item, "custom_id", "") or "").startswith("cardact:")
            and getattr(item, "callback", None) is None
        ):
            item.callback = dispatch_action


def _walk_items(items):
    for item in items or []:
        yield item
        for child in list(getattr(item, "items", []) or []) + list(
            getattr(item, "children", []) or []
        ):
            yield from _walk_items([child])


async def dispatch_action(interaction: discord.Interaction) -> None:
    """Callback bound to rebuilt action buttons; resolves bot via view."""
    view = getattr(interaction, "view", None)
    bot = getattr(view, "bot", None) or getattr(interaction, "client", None)
    if bot is None:
        return
    await dispatch(bot, interaction)


async def sync_all_guilds(bot) -> None:
    """Rebuild action views for every guild (boot path)."""
    for guild in list(getattr(bot, "guilds", [])):
        try:
            await sync_guild(bot, guild.id)
        except Exception as exc:
            log.warning("card action sync failed in %s: %s", guild.id, exc)

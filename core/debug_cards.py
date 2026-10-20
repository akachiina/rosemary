"""Default card for the ``/debug`` diagnostics panel.

The panel is a first-class card (``debug.panel``) so a guild can reskin it in
/customize like every other message. The interactive buttons stay in
:mod:`rosemary.cogs.debug`; this builder owns only the document body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from rosemary.core.cards import set_default_builder


async def _line(bot, guild_id: int, key: str, fallback: str, **variables: Any) -> str:
    text = await bot.translator.t(guild_id, key, **variables)
    return text if isinstance(text, str) and text.strip() and text != key else fallback


async def default_debug_panel(
    bot, guild_id: int, **variables: Any
) -> dict[str, Any]:
    """Build the default diagnostics document (state lines + footer)."""
    title = await _line(bot, guild_id, "debug.panel.title", "Debug panel")
    lines = [
        await _line(
            bot, guild_id, "debug.guilds", "Guilds: {count}",
            count=variables.get("guilds", "-"),
        ),
        await _line(
            bot, guild_id, "debug.members", "Members: {count}",
            count=variables.get("members", "-"),
        ),
        await _line(
            bot, guild_id, "debug.latency", "Latency: {ms} ms",
            ms=variables.get("latency", "-"),
        ),
        await _line(
            bot, guild_id, "debug.uptime", "Uptime: {uptime}",
            uptime=variables.get("uptime", "-"),
        ),
        await _line(
            bot, guild_id, "debug.panel.version", "Version: {version}",
            version=variables.get("version", "-"),
        ),
        await _line(
            bot, guild_id, "debug.panel.theme", "Theme: {theme}",
            theme=variables.get("theme", "-"),
        ),
        await _line(
            bot, guild_id, "debug.panel.logging", "Logging: {logging}",
            logging=variables.get("logging", "-"),
        ),
        await _line(
            bot, guild_id, "debug.panel.audit", "Audit log: {audit}",
            audit=variables.get("audit", "-"),
        ),
    ]
    children: list[dict[str, Any]] = [
        {"type": "text", "body": f"# {title}"},
        {"type": "text", "body": "\n".join(lines)},
    ]
    flash = str(variables.get("flash") or "").strip()
    if flash:
        children.extend([{"type": "divider"}, {"type": "text", "body": flash}])
    timestamp = variables.get("timestamp") or discord.utils.format_dt(
        datetime.now(UTC), "R"
    )
    children.extend(
        [
            {"type": "divider"},
            {"type": "text", "body": f"-# {timestamp}"},
        ]
    )
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": variables.get("flash_color") or "info",
                "children": children,
            }
        ],
    }


set_default_builder("debug.panel", default_debug_panel)

__all__ = ["default_debug_panel"]

"""One-line notices: the plain-text layer of the bot.

A notice is one thing per line. The first line is ``{emoji} message`` (or
``{emoji} **Title**`` when the card has a title); the prefix belongs to the
theme (the ``feedback_*`` style family), so a guild restyles every notice at
once; the message itself is the usual catalog copy.

Two entry points:

* :func:`line` wraps a message already in hand (the theme prefix plus the
  text): the send site keeps its own wording;
* :func:`respond` answers an interaction, a command context or a channel
  with one notice line.

Notices never ping: an acknowledgement that mentions the member it is about
to ban must not notify that member. The few surfaces that *do* want a ping
are cards, and they carry their own mention policy.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

#: The tones of the prefix family (the theme defines the matching styles).
STYLES: tuple[str, ...] = ("error", "warning", "success", "info")

#: The style used when a caller does not pick one.
DEFAULT_STYLE = "info"

#: Cache of the built-in theme (the fallback when the bot has no theme).
_BUILTIN_THEME = None

#: Theme color tokens mapped to the notice tones. The callers already speak
#: in colors (``danger`` for errors), the theme speaks in tones.
TONES: dict[str, str] = {
    "danger": "error",
    "error": "error",
    "warning": "warning",
    "success": "success",
    "info": "info",
}


def tone_for(color: str) -> str:
    """The notice tone of a theme color token (``danger`` -> ``error``)."""
    return TONES.get((color or "").strip().lower(), DEFAULT_STYLE)


def _builtin_theme():
    """The built-in theme (cached): the notice layer never fails on a bot
    whose theme snapshot is missing (a fake bot in a test, a half-built
    client at boot)."""
    global _BUILTIN_THEME
    if _BUILTIN_THEME is None:
        from rosemary.ui.theme import load_theme

        _BUILTIN_THEME = load_theme()
    return _BUILTIN_THEME


def prefix_for(bot, guild_id: int | None, style: str = DEFAULT_STYLE) -> str:
    """The theme prefix of a notice ("{emoji} ").

    The guild's theme wins; the built-in theme is the fallback when the
    guild's theme does not define the style.
    """
    from rosemary.core.themes import theme_for

    tone = tone_for(style)
    try:
        theme = theme_for(bot, guild_id)
    except AttributeError:
        theme = None
    prefix = theme.style(f"feedback_{tone}").prefix if theme is not None else ""
    if not prefix:
        fallback = getattr(bot, "theme", None) or _builtin_theme()
        prefix = fallback.style(f"feedback_{tone}").prefix
    return prefix


def line(bot, guild_id: int | None, body: str, *, style: str = DEFAULT_STYLE) -> str:
    """One notice line: the theme prefix plus the message."""
    body = (body or "").strip()
    if not body:
        return ""
    return f"{prefix_for(bot, guild_id, style)}{body}"


async def respond(
    target: Any,
    bot,
    guild_id: int | None,
    body: str,
    *,
    style: str = DEFAULT_STYLE,
    ephemeral: bool = True,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> Any:
    """Send one notice line to an interaction, a context or a channel.

    ``target`` may be an :class:`discord.Interaction`, an application
    command context (or the :class:`rosemary.ui.responses.PrefixContext`
    facade over a prefix context) or a channel. Prefix contexts have no
    ephemeral messages: the facade drops the flag and replies publicly.

    The notice never notifies: the copy may carry the mention of the member
    it is about to ban, and a plain-text message would ping them (the
    Components V2 container it replaced never did). The policy rides every
    path, so an acknowledgement stays silent.
    """
    content = line(bot, guild_id, body, style=style)
    if not content:
        return None
    if allowed_mentions is None:
        allowed_mentions = discord.AllowedMentions.none()

    responder = getattr(target, "respond", None)
    response = getattr(target, "response", None)
    done = response is not None and response.is_done()
    if callable(responder) and not done:
        # An application command context (or the prefix facade): it answers
        # the interaction itself.
        return await responder(
            content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
        )
    if done and hasattr(target, "followup"):
        # The interaction was already ACKed (deferred): the follow-up is
        # the only way to answer it.
        return await target.followup.send(
            content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
        )
    if response is not None:
        # A raw interaction: answer it directly.
        return await response.send_message(
            content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
        )
    return await target.send(content, allowed_mentions=allowed_mentions)


__all__ = [
    "DEFAULT_STYLE",
    "STYLES",
    "TONES",
    "line",
    "prefix_for",
    "respond",
    "tone_for",
]

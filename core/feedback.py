"""One-line notices: the plain-text layer of the bot.

A notice is one thing per line. The first line is ``{emoji} message`` (or
``{emoji} **Title**`` when the card has a title); the icon belongs to the
notice, never to the copy. The decorator (:func:`notice_line`) strips any mark
the translated copy still carries and writes the one icon the notice shows:
the verb of the action (``ban``, ``mute``...) when the send site names one, the
theme tone (the ``feedback_*`` style family) otherwise. A guild restyles every
notice at once.

Two entry points:

* :func:`line` wraps a message already in hand (the decorated line): the send
  site keeps its own wording;
* :func:`respond` answers an interaction, a command context or a channel
  with one notice line.

Notices never ping: an acknowledgement that mentions the member it is about
to ban must not notify that member. The few surfaces that *do* want a ping
are cards, and they carry their own mention policy.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

log = logging.getLogger(__name__)

#: The leading mark a copy may still bring: one Unicode emoji (with its
#: modifiers and joiners) or one custom emoji mention. The decorator owns the
#: icon, so the body never ships a mark of its own.
_LEADING_MARK = re.compile(
    r"^(?:"
    r"<a?:[A-Za-z0-9_]+:\d+>"
    r"|[\U0001F000-\U0001FAFF\u2139\u2190-\u21FF\u2300-\u23FF"
    r"\u2600-\u27BF\u2B00-\u2BFF]"
    r"[\uFE0F\u200D\U0001F3FB-\U0001F3FF]*"
    r")"
)

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


def strip_mark(text: str) -> str:
    """The text without the leading marks the copy may still carry.

    A catalog copy often opened with the tone token (``{success} {member}...``)
    before the notice layer owned the icon. The rendered text then started with
    the same emoji the prefix added, and the member saw two checks. The
    decorator strips every leading mark (Unicode or custom emoji) so the line
    carries exactly one icon.
    """
    text = (text or "").lstrip()
    while text:
        match = _LEADING_MARK.match(text)
        if match is None:
            break
        text = text[match.end() :].lstrip()
    return text


def _theme(bot, guild_id: int | None):
    """The guild's theme, the bot's built-in theme, or the packaged one."""
    from rosemary.core.themes import theme_for

    try:
        theme = theme_for(bot, guild_id)
    except AttributeError:
        theme = None
    return theme or getattr(bot, "theme", None) or _builtin_theme()


def icon_for(bot, guild_id: int | None, token: str | None) -> str:
    """Resolve one icon token (``mute``, ``ban``...) to what Discord renders."""
    if not token:
        return ""
    return _theme(bot, guild_id).icon(token) or ""


def _prefix_with_icon(prefix: str, glyph: str) -> str:
    """The theme prefix with its leading mark swapped for ``glyph``.

    The default prefix is only the tone icon, but a guild may decorate it
    (``"⭐ >> "``). The verb icon replaces the mark and the decoration stays.
    """
    rest = strip_mark(prefix)
    if rest != prefix:
        if not rest:
            return f"{glyph} "
        return f"{glyph} {rest}"
    if not prefix:
        return f"{glyph} "
    return f"{glyph} {prefix}"


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
    tone = tone_for(style)
    prefix = _theme(bot, guild_id).style(f"feedback_{tone}").prefix
    if not prefix:
        prefix = _builtin_theme().style(f"feedback_{tone}").prefix
    return prefix


def notice_line(
    bot,
    guild_id: int | None,
    body: str,
    *,
    style: str = DEFAULT_STYLE,
    icon: str | None = None,
) -> str:
    """One notice line: the copy without its marks, the one icon, the body.

    ``style`` picks the theme tone (``error``/``warning``/``success``/``info``)
    and ``icon`` names the verb of the action. When the verb resolves, it
    replaces the tone icon in the theme prefix; otherwise the tone icon stays.
    """
    body = strip_mark(body)
    if not body:
        return ""
    tone = tone_for(style)
    prefix = prefix_for(bot, guild_id, tone)
    glyph = icon_for(bot, guild_id, icon)
    if glyph:
        prefix = _prefix_with_icon(prefix, glyph)
    return f"{prefix}{body}"


def line(
    bot,
    guild_id: int | None,
    body: str,
    *,
    style: str = DEFAULT_STYLE,
    icon: str | None = None,
) -> str:
    """One notice line: the theme prefix plus the message."""
    return notice_line(bot, guild_id, body, style=style, icon=icon)


async def respond(
    target: Any,
    bot,
    guild_id: int | None,
    body: str,
    *,
    style: str = DEFAULT_STYLE,
    icon: str | None = None,
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
    content = notice_line(bot, guild_id, body, style=style, icon=icon)
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
    "icon_for",
    "line",
    "notice_line",
    "prefix_for",
    "respond",
    "strip_mark",
    "tone_for",
]

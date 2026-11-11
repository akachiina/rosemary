"""One-line notices: the plain-text layer of the bot.

A notice is the copy, exactly as the catalog writes it. The copy opens with
the icon of the action (``{ban}``, ``{mute}``, ``{success}``...): the token
is a theme emoji, so a guild restyles the art of every notice at once without
touching a line of code. The notice layer adds nothing to the text: it only
sends it, and it never pings.

An acknowledgement that names the member the bot is about to ban must not
notify that member, so the policy rides every path: a notice stays silent.
The few surfaces that *do* want a ping are cards, and they carry their own
mention policy.

One entry point: :func:`respond` answers an interaction, a command context or
a channel with the copy.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger(__name__)


async def respond(
    target: Any,
    body: str,
    *,
    ephemeral: bool = True,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> Any:
    """Send one notice to an interaction, a context or a channel.

    ``target`` may be an :class:`discord.Interaction`, an application
    command context (or the :class:`rosemary.ui.responses.PrefixContext`
    facade over a prefix context) or a channel. Prefix contexts have no
    ephemeral messages: the facade drops the flag and replies publicly.

    The notice never notifies: the copy may carry the mention of the member
    it is about to ban, and a plain-text message would ping them (the
    Components V2 container it replaced never did). The policy rides every
    path, so an acknowledgement stays silent.
    """
    content = (body or "").strip()
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
        # The interaction was already ACKed (deferred): the follow-up is the
        # only way to answer it.
        return await target.followup.send(
            content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
        )
    if response is not None:
        # A raw interaction: answer it directly.
        return await response.send_message(
            content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
        )
    return await target.send(content, allowed_mentions=allowed_mentions)


__all__ = ["respond"]

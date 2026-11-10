"""The runtime of an interactive surface: open, update, close.

Every panel answers the same way to a click, to a timeout, to a dead token
and to a failure. This module owns that lifecycle:

* :func:`ack`: the single ACK, before any slow work (Discord's window is
  3 seconds);
* :func:`open`: build the panel, then send it;
* :func:`update`: rebuild the items and edit the message in place;
* :func:`close`: disable every item and stop the view;
* :func:`measured`: the metric of every click (panel, action, ok/fail and
  the duration).

A dead token (an expired interaction, or one Discord forgot after a gateway
RESUME replayed an old click) is an expected condition, not a crash: the
ACK reports it and the handler stops before doing anything.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from rosemary.core.interactions import ack as _ack_interaction

log = logging.getLogger(__name__)

Handler = Callable[[discord.Interaction], Awaitable[None]]


async def ack(
    panel: Any,
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
    suppress_errors: bool = False,
) -> bool:
    """ACK the interaction once; ``False`` when the token is dead.

    ``suppress_errors=True`` is for the modal submits, where a failed
    defer must not mask the follow-up re-render (which handles the HTTP
    failures itself).
    """
    try:
        return await _ack_interaction(interaction, ephemeral=ephemeral)
    except discord.HTTPException:
        if not suppress_errors:
            raise
        return False


async def open(panel: Any, responder: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Open a panel: prepare it, then answer.

    ``responder`` is the awaitable that sends the message: ``ctx.respond``
    for a slash command, ``interaction.response.send_message`` for a
    component. The panel is prepared first, so Discord never receives a
    half-built payload.
    """
    await panel.prepare()
    payload = await panel.message_kwargs()
    payload.update(kwargs)
    return await responder(**payload)


async def update(panel: Any, interaction: discord.Interaction) -> bool:
    """Rebuild the panel and edit its message in place.

    Returns ``False`` when the panel could not be drawn (a broken payload):
    the failure is logged, never shown as a dead click.
    """
    from rosemary.ui.menu import MenuView  # noqa: F401  (the type of the panel)

    await panel.prepare()
    extra = await panel.rerender_payload()
    try:
        await interaction.edit(view=panel, **extra)
        return True
    except discord.NotFound:
        # The interaction token expired (past 3s without an ACK, error
        # 10062): refresh the underlying message directly, so the panel
        # still updates.
        message = getattr(interaction, "message", None)
        if message is None:
            return False
        with contextlib.suppress(
            discord.NotFound, discord.Forbidden, discord.HTTPException
        ):
            await message.edit(view=panel, **extra)
        return True
    except (discord.Forbidden, discord.HTTPException):
        # A 400 (invalid emoji, bad label, ...) must not look like a dead
        # click: log the full error so the payload problem is diagnosable
        # from bot.log (AGENTS.md gotcha).
        log.warning(
            "panel edit failed for %s",
            type(panel).__name__,
            exc_info=True,
        )
        return False


async def close(panel: Any, interaction: discord.Interaction) -> None:
    """Close a panel: disable every item, edit the message and stop."""
    panel.disable_all_items()
    with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
        await interaction.edit(view=panel)
    panel.stop()


def measured(panel: Any, custom_id: str, handler: Handler) -> Handler:
    """Wrap a handler so every click logs the panel, the action and the time.

    The wrapper keeps the handler's identity for the registry (the tests
    bind the buttons to ``view._handlers[custom_id]``), so the metric never
    changes how a click is dispatched.
    """

    async def wrapper(interaction: discord.Interaction) -> None:
        started = time.monotonic()
        ok = True
        try:
            await handler(interaction)
        except Exception:
            ok = False
            raise
        finally:
            log.info(
                "panel=%s action=%s ok=%s duration_ms=%d",
                type(panel).__name__,
                custom_id,
                "ok" if ok else "fail",
                int((time.monotonic() - started) * 1000),
            )

    return wrapper


__all__ = ["ack", "close", "measured", "open", "update"]

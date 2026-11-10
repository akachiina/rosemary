"""The sendable bundle of a card: the three shapes, one payload.

A card may ship as Components V2, as a classic embed or as plain text. The
bundle (:class:`CardPayload`) carries all three, so the send sites never
branch on the form: they hand ``payload.message_kwargs()`` to Discord and the
right keyword goes with it.

The Components V2 flag is Discord's own (``IS_COMPONENTS_V2``) and it is
immutable: a message sent as V2 stays V2, and a classic message never gains
it. The two helpers here read that flag, so a repaint knows the shape it has
to keep.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import discord

#: Discord's ``IS_COMPONENTS_V2`` message flag (1 << 15). The flag is
#: immutable: a message sent as Components V2 stays V2 and a classic message
#: never gains it, so a repaint has to follow the shape the message has.
COMPONENTS_V2_FLAG = 1 << 15


@dataclasses.dataclass(frozen=True)
class CardPayload:
    """One rendered card, ready to send: V2 view, classic embed or plain text.

    A theme may write any card as Components V2 (default) or as an embed
    (``cards.<key>.embed``). This bundle makes the shapes interchangeable
    at every send site: ``embed`` is set only for embed cards, ``view``
    carries the V2 items (or the classic ActionRow holding an embed card's
    buttons) and ``content`` carries a plain-text message (or the line that
    holds an embed card's essential ping). Send through :func:`send_card`
    instead of touching the fields.
    """

    view: discord.ui.DesignerView | discord.ui.View | None = None
    embed: discord.Embed | None = None
    #: Files uploaded with the message; galleries/thumbnails in the card
    #: reference them via ``attachment://<filename>`` (color panel image).
    files: list[discord.File] = dataclasses.field(default_factory=list)
    #: Plain text of the message (text form, or the ping line of an embed).
    content: str | None = None

    def message_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``send``/``respond``/``edit`` with this card."""
        kwargs: dict[str, Any] = {}
        if self.content is not None:
            kwargs["content"] = self.content
        if self.embed is not None:
            kwargs["embed"] = self.embed
            if self.view is not None:
                kwargs["view"] = self.view
        elif self.view is not None:
            kwargs["view"] = self.view
        if self.files:
            kwargs["files"] = self.files
        return kwargs


def is_v2_message(message: Any) -> bool:
    """Whether a live message carries Discord's ``IS_COMPONENTS_V2`` flag.

    A V2 message cannot carry content or embeds and a classic message can
    never become V2: a repaint that ignores this ends in a 400 (error 50035).
    """
    flags = getattr(message, "flags", None)
    if flags is None:
        return False
    value = getattr(flags, "value", None)
    if value is None:
        return False
    try:
        return bool(int(value) & COMPONENTS_V2_FLAG)
    except (TypeError, ValueError):
        return False


def is_v2_payload(payload: CardPayload | None) -> bool:
    """Whether a rendered payload ships Components V2 (a V2 view)."""
    if payload is None:
        return False
    view = getattr(payload, "view", None)
    if view is None:
        return False
    checker = getattr(view, "is_components_v2", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # pragma: no cover - defensive
            return False
    return isinstance(view, discord.ui.DesignerView)


async def send_card(
    target,
    payload: CardPayload | None,
    allowed: discord.AllowedMentions | None = None,
    **kwargs: Any,
):
    """Send a rendered card payload through ``target.send``.

    Dispatches the right keyword for the payload shape: ``view=`` for V2,
    ``embed=`` (+ classic button view) for embed cards: so call sites never
    branch on the card form. Interaction responses use
    ``ctx.respond(**payload.message_kwargs(), ...)`` instead.
    """
    if payload is None:
        return None
    if allowed is None:
        allowed = discord.AllowedMentions.none()
    return await target.send(**payload.message_kwargs(), allowed_mentions=allowed, **kwargs)


__all__ = [
    "COMPONENTS_V2_FLAG",
    "CardPayload",
    "is_v2_message",
    "is_v2_payload",
    "send_card",
]

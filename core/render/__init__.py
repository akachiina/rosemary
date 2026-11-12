"""The pictures the bot draws for a card.

A card may carry a **drawn element**: a picture the bot rasterizes for it
(the author strip of a card, for instance). The element rides the card as a
media gallery and the PNG rides the message as an attachment.

The technique is generic: a kind (``author``), the HTML template the theme
writes and the values the template consumes. Nothing here breaks a send:
without the renderer, without the picture or with a broken template the
element does not exist and the card keeps its native shape.

* :mod:`rosemary.core.render.elements` draws the picture;
* :mod:`rosemary.core.render.identity` resolves who the card is about.
"""

from __future__ import annotations

__all__ = ["elements", "identity"]

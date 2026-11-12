"""The Card IR: one semantic model behind every card, whatever its form.

A card says *what* it is (a title, an author, fields, a footer, a body), not
how Discord writes it. The three forms are serializations of this model:

* ``card``: Components V2 (the interactive/rich layout);
* ``embed``: the classic embed (visual info, staff logs);
* ``text``: one plain line.

Because the model is semantic, the markdown dialect of a form belongs to its
serializer: the small-text marker (``-#``) and the heading marker (``#``) are
added by the Components V2 serializer and never by the content.

A card is *native* in one of the three shapes:

* **blocks**: a Components V2 body (the theme's own layout);
* **slots**: the semantic fields (title, description, fields, footer...);
* **content**: a single plain text.

The native shape decides how the card is written; the other forms are
conversions (see :mod:`rosemary.core.design.serialize`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Form = Literal["card", "embed", "text"]

#: The three shapes a card can be native in.
NATIVE_BLOCKS = "blocks"
NATIVE_SLOTS = "slots"
NATIVE_CONTENT = "content"


@dataclass(frozen=True)
class Field:
    """One label/value row (an embed field, or a ``**Label:** value`` line).

    ``code`` marks a machine value (a snowflake id): it renders as inline
    code in every form. ``optional`` marks a row that disappears when its
    value is empty (the compiler drops it). ``gate`` names the variable that
    decides whether the row exists at all: a row whose value is a composition
    (the two timestamps of a creation date) needs a gate, because the value
    itself never arrives empty.
    """

    name: str
    value: str
    inline: bool = False
    code: bool = False
    optional: bool = False
    gate: str = ""


@dataclass(frozen=True)
class Author:
    """The author line of an embed (the member or the actor)."""

    name: str
    icon_url: str = ""


@dataclass(frozen=True)
class Element:
    """A drawn element of a card: a picture the bot renders for it.

    The picture rides the message as an attachment and the card points at
    it (``attachment://<filename>``). The kind names the element (the
    ``author`` strip), so the serializer knows what to draw where.
    """

    kind: str
    attachment: str = ""
    description: str = ""


@dataclass(frozen=True)
class Action:
    """One button: a link, a registered action or a code-owned picker."""

    label: str
    url: str = ""
    action: str = ""
    emoji: str = ""
    style: str = ""
    id: str = ""


@dataclass(frozen=True)
class Text:
    """A text block (Components V2)."""

    body: str
    id: str = ""


@dataclass(frozen=True)
class Divider:
    """A separator (Components V2)."""

    spacing: str = "small"
    visible: bool = True
    id: str = ""


@dataclass(frozen=True)
class Gallery:
    """A media gallery (Components V2)."""

    urls: tuple[str, ...]
    id: str = ""


@dataclass(frozen=True)
class Thumbnail:
    """The accessory of a section (Components V2)."""

    url: str


@dataclass(frozen=True)
class Row:
    """A row of buttons (Components V2)."""

    buttons: tuple[Action, ...]
    id: str = ""


@dataclass(frozen=True)
class Section:
    """A section: a column of blocks with one accessory (Components V2)."""

    children: tuple[Any, ...]
    accessory: Thumbnail | Action | None = None
    id: str = ""


@dataclass(frozen=True)
class Container:
    """The colored frame that wraps a card (Components V2)."""

    children: tuple[Any, ...]
    color: str = ""
    id: str = ""


Block = Text | Divider | Gallery | Row | Section | Container

#: Discord's limit: a message carries at most ten embeds.
MAX_EMBEDS = 10


@dataclass(frozen=True)
class Card:
    """One card, in one semantic model.

    ``blocks`` holds a Components V2 body; when it is empty the semantic slots
    describe the card instead. ``content`` holds a plain-text card. The
    serializers pick the native shape and convert the others.
    """

    key: str = ""
    form: Form = "embed"
    color: str = ""
    title: str = ""
    url: str = ""
    #: The body of the card, in paragraphs. The shape of the form owns the
    #: way they meet: the embed joins them with a line break, the Components
    #: V2 renders one block per paragraph.
    paragraphs: tuple[str, ...] = ()
    content: str = ""
    author: Author | None = None
    #: The actor of the event (the member who did it). It resolves first and
    #: answers the ``{actor}`` placeholder everywhere in the card.
    actor: str = ""
    thumbnail: str = ""
    image: str = ""
    footer: str = ""
    footer_icon: str = ""
    timestamp: int | None = None
    fields: tuple[Field, ...] = ()
    blocks: tuple[Block, ...] = ()
    actions: tuple[Action, ...] = ()
    pings: tuple[int, ...] = ()
    role_pings: tuple[int, ...] = ()
    style: str = "info"
    #: The message content line of an embed card (the ping carrier). An
    #: embed never notifies: the mention tokens travel here, not in the
    #: embed body.
    ping_line: str = ""
    #: The line that always rides the message content of an embed card (the
    #: event sentence). Unlike ``ping_line``, it is not gated by the theme's
    #: pings toggle: a DM line is content, not a notification, so it ships
    #: whatever the toggle says.
    content_line: str = ""
    #: The embeds of the message, when the card declares more than one. Each
    #: one is a card of its own (its own title, body, fields and accent), so
    #: a theme can stack several boxes in a single message. An empty tuple
    #: means the card's own slots describe its single embed.
    embeds: tuple[Card, ...] = ()
    #: The pictures the bot draws for the card (the author strip). They ride
    #: the message as attachments and replace the slot they stand for (the
    #: strip replaces the author line).
    elements: tuple[Element, ...] = ()
    #: The names the card receives (its contract). A name of the contract is
    #: reserved: when the send site does not provide it, the placeholder
    #: stays literal instead of falling back to a theme emoji with the same
    #: name.
    variables: tuple[str, ...] = ()

    @property
    def native(self) -> str:
        """Which shape the card is native in."""
        if self.blocks:
            return NATIVE_BLOCKS
        if self.content:
            return NATIVE_CONTENT
        return NATIVE_SLOTS

    @property
    def copy_only(self) -> bool:
        """Whether the card carries copy but no layout.

        A body made only of text blocks is a copy, not a layout: it can
        travel in any shape without losing anything. A card that carries a
        container, a section, a gallery or a row asked for a rich layout, so
        it ships as Components V2.
        """
        return bool(self.blocks) and all(isinstance(block, Text) for block in self.blocks)


def _with_id(node: dict[str, Any], block: Any) -> dict[str, Any]:
    """Keep the editor's block id on the document node."""
    block_id = getattr(block, "id", "")
    if block_id:
        node["id"] = block_id
    return node


def block_to_document(block: Block) -> dict[str, Any]:
    """One IR block as the internal Components V2 document block."""
    if isinstance(block, Text):
        return _with_id({"type": "text", "body": block.body}, block)
    if isinstance(block, Divider):
        node: dict[str, Any] = {"type": "divider"}
        if block.spacing != "small":
            node["spacing"] = block.spacing
        if not block.visible:
            node["visible"] = False
        return _with_id(node, block)
    if isinstance(block, Gallery):
        return _with_id({"type": "gallery", "urls": list(block.urls)}, block)
    if isinstance(block, Row):
        return _with_id(
            {
                "type": "row",
                "buttons": [
                    {
                        key: value
                        for key, value in (
                            ("label", button.label),
                            ("url", button.url),
                            ("action", button.action),
                            ("emoji", button.emoji),
                            ("style", button.style),
                            ("id", button.id),
                        )
                        if value
                    }
                    for button in block.buttons
                ],
            },
            block,
        )
    if isinstance(block, Section):
        accessory: dict[str, Any] | None = None
        if isinstance(block.accessory, Thumbnail):
            accessory = {"type": "thumbnail", "url": block.accessory.url}
        elif isinstance(block.accessory, Action):
            accessory = {
                key: value
                for key, value in (
                    ("type", "button"),
                    ("label", block.accessory.label),
                    ("url", block.accessory.url),
                    ("action", block.accessory.action),
                    ("emoji", block.accessory.emoji),
                    ("style", block.accessory.style),
                    ("id", block.accessory.id),
                )
                if value
            }
        node = {
            "type": "section",
            "children": [block_to_document(child) for child in block.children],
        }
        if accessory is not None:
            node["accessory"] = accessory
        return _with_id(node, block)
    if isinstance(block, Container):
        node = {
            "type": "container",
            "children": [block_to_document(child) for child in block.children],
        }
        if block.color:
            node["color"] = block.color
        return _with_id(node, block)
    raise TypeError(f"unknown block: {block!r}")


def document_to_block(node: Any) -> Block | None:
    """One internal Components V2 document block as an IR block."""
    if not isinstance(node, dict):
        return None
    kind = node.get("type")
    block_id = str(node.get("id") or "")
    if kind == "text":
        return Text(body=str(node.get("body") or ""), id=block_id)
    if kind == "divider":
        return Divider(
            spacing=str(node.get("spacing") or "small"),
            visible=node.get("visible", True) is not False,
            id=block_id,
        )
    if kind == "gallery":
        urls = node.get("urls")
        return Gallery(
            urls=tuple(str(url) for url in urls or []),
            id=block_id,
        )
    if kind == "row":
        buttons = node.get("buttons") or []
        return Row(
            buttons=tuple(
                Action(
                    label=str(button.get("label") or ""),
                    url=str(button.get("url") or ""),
                    action=str(button.get("action") or ""),
                    emoji=str(button.get("emoji") or ""),
                    style=str(button.get("style") or ""),
                    id=str(button.get("id") or ""),
                )
                for button in buttons
                if isinstance(button, dict)
            ),
            id=block_id,
        )
    if kind == "section":
        accessory: Thumbnail | Action | None = None
        raw = node.get("accessory")
        if isinstance(raw, dict):
            if raw.get("type") == "button":
                accessory = Action(
                    label=str(raw.get("label") or ""),
                    url=str(raw.get("url") or ""),
                    action=str(raw.get("action") or ""),
                    emoji=str(raw.get("emoji") or ""),
                    style=str(raw.get("style") or ""),
                    id=str(raw.get("id") or ""),
                )
            else:
                accessory = Thumbnail(url=str(raw.get("url") or ""))
        children = tuple(
            child
            for child in (
                document_to_block(item) for item in node.get("children") or []
            )
            if child is not None
        )
        return Section(children=children, accessory=accessory, id=block_id)
    if kind == "container":
        children = tuple(
            child
            for child in (
                document_to_block(item) for item in node.get("children") or []
            )
            if child is not None
        )
        return Container(
            children=children,
            color=str(node.get("color") or ""),
            id=block_id,
        )
    return None


def blocks_to_document(blocks: tuple[Block, ...]) -> dict[str, Any]:
    """IR blocks as the internal Components V2 document."""
    return {"v": 1, "blocks": [block_to_document(block) for block in blocks]}


__all__ = [
    "Action",
    "Author",
    "Block",
    "Card",
    "Container",
    "Divider",
    "Field",
    "Form",
    "Gallery",
    "NATIVE_BLOCKS",
    "NATIVE_CONTENT",
    "NATIVE_SLOTS",
    "Row",
    "Section",
    "Text",
    "Thumbnail",
    "block_to_document",
    "blocks_to_document",
    "document_to_block",
]

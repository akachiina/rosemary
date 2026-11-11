"""Compile a card: resolve the placeholders once, collect the mentions.

The legacy pipeline resolved the same text twice (once for the mention
fields, once for the renderer) and could leave a raw ``{name}`` behind when a
variable held braces. The IR resolves **once**, here: every text slot of the
card is substituted with the contract variables (theme emojis as defaults) and
the mention fields (``{@name}``) are collected on the way.

The result is a card whose texts are final: the renderers draw it as-is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from rosemary.core.cards import safe_format_mentions
from rosemary.core.design.ir import (
    Action,
    Author,
    Card,
    Container,
    Divider,
    Field,
    Gallery,
    Row,
    Section,
    Text,
    Thumbnail,
)


@dataclass(frozen=True)
class Compiled:
    """A compiled card: every placeholder resolved, mentions collected.

    ``users`` and ``roles`` are the ids the card's mention fields resolve to
    (the ``{@name}`` ones). They are what may ping when the card ships in the
    Components V2 form; the classic forms ping through their content line.
    """

    card: Card
    users: tuple[int, ...] = ()
    roles: tuple[int, ...] = ()


class _Mentions:
    """The mention ids collected while the card is compiled."""

    def __init__(self) -> None:
        self.users: list[int] = []
        self.roles: list[int] = []

    def collect(self, found: Mapping[str, tuple[str, int]]) -> None:
        """Record the mentions one field resolved to."""
        for kind, found_id in found.values():
            (self.roles if kind == "role" else self.users).append(found_id)

    def result(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """``(users, roles)``, order-preserving and without duplicates."""
        return (
            tuple(dict.fromkeys(self.users)),
            tuple(dict.fromkeys(self.roles)),
        )


def compile_card(
    card: Card,
    mapping: Mapping[str, Any],
    mentions: _Mentions | None = None,
) -> Compiled:
    """Resolve every placeholder of ``card`` and collect its mentions.

    The card's native shape decides what is resolved: a blocks card resolves
    its body, a slots card its fields, a content card its text. The color
    resolves too: it names a theme token, but the state may choose it (the
    document writes the alternatives).

    The ``actor`` slot resolves first: it answers the ``{actor}``
    placeholder everywhere in the card (the log footers use it).

    ``mentions`` is shared by the embeds of a message: every box of the
    message collects into the same policy, because Discord applies it per
    message, not per embed.
    """
    mentions = _Mentions() if mentions is None else mentions
    mapping = dict(mapping)
    if card.actor:
        actor, found = _resolve(card.actor, mapping)
        mentions.collect(found)
        if "{" not in actor:
            # The actor slot names the variable that holds the actor. When it
            # did not arrive, the value the send site already sent (the
            # ``actor`` variable) stays in place.
            mapping["actor"] = actor
    if card.blocks:
        blocks = tuple(_compile_block(block, mapping, mentions) for block in card.blocks)
        compiled = replace(card, blocks=blocks)
    elif card.content:
        text, found = safe_format_mentions(card.content, mapping)
        mentions.collect(found)
        compiled = replace(card, content=text)
    else:
        compiled = _compile_slots(card, mapping, mentions)
    users, roles = mentions.result()
    return Compiled(
        card=replace(compiled, pings=users, role_pings=roles),
        users=users,
        roles=roles,
    )


def _resolve(text: str, mapping: Mapping[str, Any]) -> tuple[str, dict[str, tuple[str, int]]]:
    """One text slot resolved (the mention fields included)."""
    return safe_format_mentions(text, mapping)


def _url(text: str) -> str:
    """A URL slot: an unresolved placeholder means there is no image.

    A thumbnail whose variable never arrived is not a broken link: it is no
    thumbnail at all, so the slot drops itself.
    """
    return "" if "{" in text else text


def _resolve_color(text: str, mapping: Mapping[str, Any]) -> str:
    """The color token of the card, resolved once like every other slot.

    The color names a theme token: the theme owns its meaning. The *state*
    may choose the token (the document writes the alternatives), so the slot
    resolves here. A token that never arrived is dropped: the card ships
    without an accent bar rather than with a broken one.
    """
    value, _found = _resolve(text, mapping)
    return "" if "{" in value else value


def _present(name: str, mapping: Mapping[str, Any]) -> bool:
    """Whether a variable carries content (the gate of a field row).

    A variable that never arrived and a variable that arrived empty are the
    same thing for the gate: there is no data, so there is no row.
    """
    value = mapping.get(name)
    if value is None:
        return False
    return bool(str(value).strip())


def _compile_block(block: Any, mapping: Mapping[str, Any], mentions: _Mentions) -> Any:
    """One Components V2 block, resolved."""
    if isinstance(block, Text):
        body, found = _resolve(block.body, mapping)
        mentions.collect(found)
        return replace(block, body=body)
    if isinstance(block, Divider):
        return block
    if isinstance(block, Gallery):
        urls = tuple(_resolve(url, mapping)[0] for url in block.urls)
        return replace(block, urls=urls)
    if isinstance(block, Row):
        buttons = []
        for button in block.buttons:
            compiled = _compile_action(button, mapping, mentions, collect=True)
            if _keeps_its_purpose(button, compiled.url):
                buttons.append(compiled)
        return replace(block, buttons=tuple(buttons))
    if isinstance(block, Section):
        children = tuple(
            _compile_block(child, mapping, mentions) for child in block.children
        )
        accessory = block.accessory
        if isinstance(accessory, Thumbnail):
            accessory = Thumbnail(url=_resolve(accessory.url, mapping)[0])
        elif isinstance(accessory, Action):
            compiled = _compile_action(accessory, mapping, mentions, collect=True)
            # A link button that lost its URL is not an accessory: the
            # section keeps its text.
            accessory = compiled if _keeps_its_purpose(accessory, compiled.url) else None
        return replace(block, children=children, accessory=accessory)
    if isinstance(block, Container):
        children = tuple(
            _compile_block(child, mapping, mentions) for child in block.children
        )
        return replace(block, children=children)
    return block


def _compile_action(
    action: Action,
    mapping: Mapping[str, Any],
    mentions: _Mentions,
    *,
    collect: bool,
) -> Action:
    """One button, resolved. The label is the mention field (when collected)."""
    label, found = _resolve(action.label, mapping)
    if collect:
        mentions.collect(found)
    url = _resolve(action.url, mapping)[0]
    emoji = _resolve(action.emoji, mapping)[0]
    return replace(action, label=label, url=url, emoji=emoji)


def _keeps_its_purpose(action: Action, url: str) -> bool:
    """Whether a button still has what it needs to ship.

    A link button whose URL never arrived is not a button: Discord would
    reject the whole payload (a link with no URL). A button that never
    declared a URL stays: it is a code-owned picker (the color panel) or a
    registered action, and the code owns its behavior.
    """
    if action.action or not action.url:
        return True
    return bool(url) and "{" not in url


def _compile_slots(card: Card, mapping: Mapping[str, Any], mentions: _Mentions) -> Card:
    """The semantic slots of an embed card, resolved."""
    title, found = _resolve(card.title, mapping)
    mentions.collect(found)
    paragraphs = []
    for paragraph in card.paragraphs:
        text, found = _resolve(paragraph, mapping)
        mentions.collect(found)
        if not text.strip():
            # An empty paragraph is not a paragraph: the line whose data
            # never arrived simply does not exist.
            continue
        paragraphs.append(text)
    footer, found = _resolve(card.footer, mapping)
    mentions.collect(found)
    author = card.author
    if author is not None:
        name, found = _resolve(author.name, mapping)
        mentions.collect(found)
        if "{" in name:
            # The actor never arrived: there is no author line.
            author = None
        else:
            author = Author(
                name=name,
                icon_url=_url(_resolve(author.icon_url, mapping)[0]),
            )
    fields = []
    for field in card.fields:
        if field.gate and not _present(field.gate, mapping):
            # The row's data never arrived: the row does not exist (the gate
            # is how a composed value says "this line is only about that").
            continue
        name, found = _resolve(field.name, mapping)
        mentions.collect(found)
        value, found = _resolve(field.value, mapping)
        mentions.collect(found)
        if not value.strip():
            # An empty value: an optional row disappears, the others show the
            # dash (the convention of the field rows).
            if field.optional:
                continue
            value = "-"
        fields.append(
            Field(
                name=name,
                value=value,
                inline=field.inline,
                code=field.code,
                optional=field.optional,
                gate=field.gate,
            )
        )
    actions: list[Action] = []
    for action in card.actions:
        compiled = _compile_action(action, mapping, mentions, collect=False)
        if _keeps_its_purpose(action, compiled.url):
            actions.append(compiled)
    # The boxes of the message: each one is a card of its own, resolved with
    # the same mapping and the same mention policy.
    embeds = tuple(compile_card(sub, mapping, mentions).card for sub in card.embeds)
    return replace(
        card,
        title=title,
        url=_url(_resolve(card.url, mapping)[0]),
        color=_resolve_color(card.color, mapping),
        paragraphs=tuple(paragraphs),
        author=author,
        thumbnail=_url(_resolve(card.thumbnail, mapping)[0]),
        image=_url(_resolve(card.image, mapping)[0]),
        footer=footer,
        footer_icon=_url(_resolve(card.footer_icon, mapping)[0]),
        fields=tuple(fields),
        actions=tuple(actions),
        embeds=embeds,
        ping_line=_resolve(card.ping_line, mapping)[0],
        content_line=_resolve(card.content_line, mapping)[0],
    )


__all__ = ["Compiled", "compile_card"]

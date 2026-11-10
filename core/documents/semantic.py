"""The card document: YAML slots into the Card IR.

A document is the semantic description of a card: what it says (title, body,
fields, footer, author, thumbnail), not how Discord writes it. This module
reads that description into the model (:mod:`rosemary.core.design.ir`).

The reading is strict: an unknown key, a wrong type or a broken block is an
error with a stable code (``card_unknown_key``...), so the lint and the
``/temas`` panel can point at the offending line. A broken document never
reaches a send: the caller falls back to the default message.

The catalog references (``{log.field.target.label}``) are resolved here, once:
the variables (``{member}``) stay literal, because the compiler
(:mod:`rosemary.core.design.compile`) resolves them later, in one pass, with
the mention fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from rosemary.core.design.ir import (
    Action,
    Author,
    Card,
    Divider,
    Field,
    Gallery,
    Row,
    Section,
    Text,
    Thumbnail,
)
from rosemary.core.documents.merge import DocumentError
from rosemary.core.documents.templates import TOKEN_RE, Resolver

#: The keys a document may carry. Anything else is an error.
SLOT_KEYS = frozenset(
    {
        "actions",
        "actor",
        "author",
        "author_icon",
        "blocks",
        "category",
        "color",
        "description",
        "fields",
        "footer",
        "form",
        "form_override",
        "image",
        "legacy_body",
        "mention",
        "paragraphs",
        "rich",
        "style",
        "thumbnail",
        "timestamp",
        "title",
        "variables",
    }
)

#: The keys that steer the resolution (never content).
DIRECTIVE_KEYS = frozenset({"archetype", "use"})

#: The forms a document may declare.
FORMS = ("card", "embed", "text")

#: The field names whose value is a machine value: it renders as inline code.
MACHINE_NAMES = frozenset(
    {
        "member_id",
        "user_id",
        "role_id",
        "channel_id",
        "server_id",
        "emoji_id",
        "sticker_id",
    }
)

#: The label of a field comes from the catalog, under this prefix.
FIELD_LABEL_PREFIX = "log.field."


def validate_keys(doc: Mapping[str, Any]) -> None:
    """Raise :class:`DocumentError` when the document carries an unknown key."""
    for key in doc:
        if key in SLOT_KEYS or key in DIRECTIVE_KEYS:
            continue
        raise DocumentError("card_unknown_key", str(key))


def is_semantic_document(entry: Any) -> bool:
    """Whether a theme's card entry is a semantic document (a delta).

    A semantic document is a mapping that is not one of the legacy shapes
    (the internal document, the embed entry). Everything else is a **legacy
    full card**: the raw Components V2 list or the plain string. The legacy
    cards replace the whole card; the semantic ones are deltas over the
    packaged document (the fields they do not declare are inherited).
    """
    if not isinstance(entry, dict) or not entry:
        return False
    # The legacy documents carry their own envelope.
    return not ("kind" in entry or "v" in entry or "embed" in entry)


async def build_card(
    doc: Mapping[str, Any],
    key: str,
    resolver: Resolver,
) -> Card:
    """Read a document into the Card IR.

    The catalog references are resolved here; the variables stay literal for
    the compiler. The document's ``form`` is the card's preferred shape.
    """
    validate_keys(doc)
    form = str(doc.get("form") or "embed")
    if form not in FORMS:
        raise DocumentError("card_bad_form", form)
    actor = await _text(doc.get("actor"), resolver)
    _alias_actor(actor, resolver)
    fields = await _fields(doc.get("fields"), resolver)
    blocks = await _blocks(doc.get("blocks"), resolver)
    actions = await _actions(doc.get("actions"), resolver)
    author_name = await _text(doc.get("author"), resolver)
    author_icon = await _text(doc.get("author_icon"), resolver)
    author = Author(name=author_name, icon_url=author_icon) if author_name else None
    timestamp: int | None = None
    if doc.get("timestamp"):
        timestamp = int(datetime.now(UTC).timestamp())
    return Card(
        key=key,
        form=form,
        # The color names a theme token (never a hex): the theme owns what it
        # means. It resolves like any other slot, so the state may choose the
        # token (the alternatives: ``color: ['{state_color}', 'warning']``).
        color=await _text(doc.get("color"), resolver),
        title=await _copy(doc.get("title"), resolver),
        paragraphs=await _body(doc, resolver),
        style=str(doc.get("style") or "info"),
        footer=await _text(doc.get("footer"), resolver),
        footer_icon=await _text(doc.get("footer_icon"), resolver),
        thumbnail=await _text(doc.get("thumbnail"), resolver),
        image=await _text(doc.get("image"), resolver),
        author=author,
        actor=actor,
        timestamp=timestamp,
        fields=tuple(fields),
        blocks=tuple(blocks),
        actions=tuple(actions),
        variables=tuple(str(name) for name in (doc.get("variables") or ())),
    )


async def _copy(value: Any, resolver: Resolver) -> str:
    """One text slot that may name a catalog copy.

    A value that is exactly one catalog reference (``{a.b.c}``) names the
    copy of that key: when the catalog has none, the slot is empty (the card
    has nothing to say there, and the send site keeps its own fallback)
    instead of shipping the raw key to the member. Any other value is a
    template and resolves as usual.
    """
    if not isinstance(value, str):
        return await _text(value, resolver)
    key = value.strip()
    match = TOKEN_RE.fullmatch(key)
    if match is not None and match.group("dotted"):
        found = await resolver.catalog_copy(match.group("dotted"))
        return found if found is not None else ""
    return await resolver.copy(value)


def _alias_actor(actor: str, resolver: Resolver) -> None:
    """Make the actor answer the ``{actor}`` placeholder right away.

    The ``actor`` slot names the variable that holds the actor (the
    moderator, the author). The slot is resolved before every other one, so
    the alternatives that ask for an actor (``the actor footer``) can tell
    whether there is one.
    """
    name = actor.strip().removeprefix("{").removesuffix("}")
    if not name or name == actor.strip() or "." in name:
        return
    value = resolver.variables.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return
    resolver.variables.setdefault("actor", value)


async def _body(doc: Mapping[str, Any], resolver: Resolver) -> tuple[str, ...]:
    """The body of the card, in paragraphs.

    The ``description`` slot holds one paragraph; the ``paragraphs`` slot
    holds several (the body of a card that says more than one thing). Each
    paragraph is a text slot: a template, or a reference to the copy of a
    catalog key.
    """
    variant = resolver.variables.get("body_key")
    if isinstance(variant, str) and variant.strip():
        return (await _copy("{" + variant.strip() + "}", resolver),)
    raw = doc.get("paragraphs")
    if raw is not None:
        if not isinstance(raw, list):
            raise DocumentError("card_bad_paragraphs")
        return tuple([await _copy(paragraph, resolver) for paragraph in raw])
    text = await _copy(doc.get("description"), resolver)
    return (text,) if text else ()


async def _text(value: Any, resolver: Resolver) -> str:
    """One text slot: the catalog references resolved, the variables literal.

    A slot may hold **alternatives** (a list): the first one whose
    placeholders are all known wins. It is how the document says "the actor
    footer when there is an actor, the system footer otherwise", without a
    conditional language. When no alternative is complete, the one with the
    fewest unknown placeholders wins (the list is written from the most
    specific to the most generic).
    """
    if value is None:
        return ""
    if isinstance(value, list):
        resolved = [await resolver.copy(str(item)) for item in value]
        if not resolved:
            return ""
        best = resolved[0]
        best_missing = _missing(resolved[0], resolver)
        if best_missing == 0:
            return best
        for item in resolved[1:]:
            missing = _missing(item, resolver)
            if missing == 0:
                return item
            if missing < best_missing:
                best = item
                best_missing = missing
        return best
    if not isinstance(value, str):
        raise DocumentError("card_bad_text")
    return await resolver.copy(value)


def _missing(template: str, resolver: Resolver) -> int:
    """How many placeholders of a template the send site did not provide."""
    missing = 0
    for match in TOKEN_RE.finditer(template):
        name = match.group("plain") or match.group("mention")
        if name is None:
            continue
        value = resolver.variables.get(name.lstrip("@"))
        if value is None:
            value = resolver.emojis.get(name.lstrip("@"))
        if value is None or isinstance(value, str) and not value.strip():
            missing += 1
    return missing


async def _fields(raw: Any, resolver: Resolver) -> list[Field]:
    """The ``fields`` slot: the short form (``- name: "{value}"``) or the long."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocumentError("card_bad_fields")
    fields: list[Field] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry:
            raise DocumentError("card_bad_field")
        if "label" in entry or "value" in entry:
            fields.append(await _long_field(entry, resolver))
        else:
            fields.append(await _short_field(entry, resolver))
    return fields


async def _short_field(entry: Mapping[str, Any], resolver: Resolver) -> Field:
    """``- target: "{member}"``: the label comes from the catalog."""
    if len(entry) != 1:
        raise DocumentError("card_bad_field")
    name, template = next(iter(entry.items()))
    if not isinstance(name, str) or not isinstance(template, str):
        raise DocumentError("card_bad_field")
    label = await resolver.copy("{" + FIELD_LABEL_PREFIX + name + ".label}")
    return Field(
        name=label,
        value=template,
        inline=False,
        code=name in MACHINE_NAMES,
    )


async def _long_field(entry: Mapping[str, Any], resolver: Resolver) -> Field:
    """``- {label: "...", value: "...", inline: true, code: true}``."""
    label = entry.get("label")
    value = entry.get("value")
    if not isinstance(label, str) or not isinstance(value, str):
        raise DocumentError("card_bad_field")
    code = entry.get("code")
    if code is None:
        code = _machine_value(value)
    gate = entry.get("gate")
    if gate is not None and not isinstance(gate, str):
        raise DocumentError("card_bad_field")
    return Field(
        name=await resolver.copy(label),
        # The value may name the catalog too (a composed value: the labels of
        # the parts it joins), so the copy phase resolves it here.
        value=await resolver.copy(value),
        inline=bool(entry.get("inline")),
        code=bool(code),
        optional=bool(entry.get("optional")),
        gate=(gate or "").strip(),
    )


def _machine_value(template: str) -> bool:
    """Whether a value template names a machine value (an id)."""
    name = template.strip()
    if not (name.startswith("{") and name.endswith("}")):
        return False
    return name[1:-1] in MACHINE_NAMES


async def _actions(raw: Any, resolver: Resolver) -> list[Action]:
    """The ``actions`` slot: the buttons of the card."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocumentError("card_bad_actions")
    actions: list[Action] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise DocumentError("card_bad_action")
        label = entry.get("label")
        if not isinstance(label, str):
            raise DocumentError("card_bad_action")
        actions.append(
            Action(
                label=await resolver.copy(label),
                url=str(entry.get("url") or ""),
                action=str(entry.get("action") or ""),
                emoji=await resolver.copy(str(entry.get("emoji") or "")),
                style=str(entry.get("style") or ""),
                id=str(entry.get("id") or ""),
            )
        )
    return actions


async def _blocks(raw: Any, resolver: Resolver) -> list[Any]:
    """The ``blocks`` slot: the Components V2 escape hatch of the document."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocumentError("card_bad_blocks")
    blocks: list[Any] = []
    for entry in raw:
        block = await _block(entry, resolver)
        if block is not None:
            blocks.append(block)
    return blocks


async def _block(entry: Any, resolver: Resolver) -> Any | None:
    """One block of the ``blocks`` slot."""
    if isinstance(entry, str):
        if entry == "divider":
            return Divider()
        raise DocumentError("card_bad_block", entry)
    if not isinstance(entry, dict):
        raise DocumentError("card_bad_block")
    if "text" in entry:
        body = entry.get("text")
        if not isinstance(body, str):
            raise DocumentError("card_bad_block")
        return Text(body=await resolver.copy(body))
    if "divider" in entry:
        raw = entry.get("divider") or {}
        spacing = str(raw.get("spacing") or "small") if isinstance(raw, dict) else "small"
        visible = raw.get("visible", True) is not False if isinstance(raw, dict) else True
        return Divider(spacing=spacing, visible=visible)
    if "gallery" in entry:
        urls = entry.get("gallery")
        if not isinstance(urls, list):
            raise DocumentError("card_bad_block")
        return Gallery(urls=tuple(await resolver.copy(str(url)) for url in urls))
    if "section" in entry:
        raw = entry.get("section")
        if not isinstance(raw, dict):
            raise DocumentError("card_bad_block")
        children = await _blocks(raw.get("children"), resolver)
        accessory = raw.get("accessory")
        item: Thumbnail | Action | None = None
        if isinstance(accessory, dict):
            if accessory.get("type") == "button":
                item = Action(
                    label=await resolver.copy(str(accessory.get("label") or "")),
                    url=str(accessory.get("url") or ""),
                    action=str(accessory.get("action") or ""),
                    emoji=await resolver.copy(str(accessory.get("emoji") or "")),
                    style=str(accessory.get("style") or ""),
                    id=str(accessory.get("id") or ""),
                )
            else:
                item = Thumbnail(url=await resolver.copy(str(accessory.get("url") or "")))
        return Section(children=tuple(children), accessory=item)
    if "row" in entry:
        raw = entry.get("row")
        if not isinstance(raw, list):
            raise DocumentError("card_bad_block")
        return Row(buttons=tuple(await _actions(raw, resolver)))
    if "each" in entry:
        return await _each(entry.get("each"), resolver)
    raise DocumentError("card_bad_block")


async def _each(raw: Any, resolver: Resolver) -> Any | None:
    """The ``each`` block: one block repeated per item of a list variable.

    The items are data, so their paths (``{entry.reason}``) are resolved
    here, with the item as the scope. The plain variables of the card stay
    literal: the compiler resolves them later.
    """
    if not isinstance(raw, dict):
        raise DocumentError("card_bad_block")
    name = raw.get("each")
    alias = str(raw.get("as") or "entry")
    template = raw.get("block")
    if not isinstance(name, str) or not isinstance(template, list):
        raise DocumentError("card_bad_block")
    items = resolver.variables.get(name)
    if not isinstance(items, list):
        return None
    blocks: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        child = resolver.child({alias: item})
        for entry in template:
            block = await _block(entry, child)
            if block is not None:
                blocks.append(block)
    return _as_container(blocks)


def _as_container(blocks: list[Any]) -> Any | None:
    """The repeated blocks as one block (a container when there are many)."""
    if not blocks:
        return None
    if len(blocks) == 1:
        return blocks[0]
    from rosemary.core.design.ir import Container

    return Container(children=tuple(blocks))


__all__ = [
    "DIRECTIVE_KEYS",
    "FIELD_LABEL_PREFIX",
    "FORMS",
    "MACHINE_NAMES",
    "SLOT_KEYS",
    "build_card",
    "is_semantic_document",
    "validate_keys",
]

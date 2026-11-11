"""The card pipeline: theme override -> packaged document -> serializer.

One pipeline, one model:

1. the guild's theme may override the card: a semantic **delta** over the
   packaged document (the fields it does not declare are inherited) or a
   legacy full card (the raw Components V2 of the old themes);
2. the packaged document is the source of truth (the default package, see
   :mod:`rosemary.core.theme_package`);
3. the card is compiled (every placeholder resolved **once**) and written in
   the shape it ships in (Components V2, classic embed or plain text).

The result is a :class:`rosemary.core.card_payload.CardPayload`: the send
sites never branch on the form, they hand ``payload.message_kwargs()`` to
Discord.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from rosemary.core.card_payload import (
    CardPayload,
    RenderedMessage,
    is_v2_message,
    is_v2_payload,
    send_card,
)
from rosemary.core.card_resolution import (
    get_effective_card,
    get_effective_cards,
    reserve_contract,
)
from rosemary.core.cards import (
    ECHO_VARIABLES,
    FORM_CARD,
    FORM_EMBED,
    FORM_TEXT,
    CardsError,
    build_embed_view,
    default_form,
    render_document,
    resolve_embeds,
    validate_document,
)
from rosemary.core.design.compile import compile_card
from rosemary.core.design.ir import Action, Author, Card, Field
from rosemary.core.design.serialize import card_to_embeds, card_to_text, card_to_v2
from rosemary.core.identity import event_time, inject_identities
from rosemary.core.themes import theme_for

log = logging.getLogger(__name__)


#: the fallback ---------------------------------------------------------------


def _resolve_color(theme, color: str | discord.Colour | None) -> discord.Colour:
    """The accent of a fallback card (a theme token or a ready colour).

    A theme without the requested token (or without the usual ones) still
    gets an accent: a fallback never raises.
    """
    if isinstance(color, discord.Colour):
        return color
    for token in (color, "info", "brand"):
        if not token:
            continue
        try:
            return theme.color(token)
        except KeyError:
            continue
    return discord.Colour(0x5865F2)


def simple_payload(
    bot,
    guild_id: int | None,
    key: str,
    *,
    title: str = "",
    body: str = "",
    content_line: str = "",
    color: str | discord.Colour | None = None,
    style: str | None = None,
    author: str = "",
    author_icon: str = "",
    thumbnail: str | None = None,
    image: str | None = None,
    footer: str | None = None,
    fields: tuple[tuple[str, str, bool], ...] = (),
    buttons: tuple[tuple[str, str], ...] = (),
) -> CardPayload:
    """The last-resort card, in the shape the card ships in.

    The card pipeline resolves every customizable message; this is the
    fallback for the rare case where it resolves nothing (a missing catalog
    copy). It follows the card's own form, so the surface never changes
    under the member: a text card answers with one notice line, an embed
    card with a classic embed, a Components V2 card with a container.

    ``content_line`` is the line that opens an embed card (the event
    sentence): it rides the message content, never the embed body.

    The fallback is a card of the same model as every other one: the
    serializer writes it in the shape the card ships in.
    """
    theme = theme_for(bot, guild_id)
    accent = _resolve_color(theme, color)
    card = Card(
        key=key,
        form=default_form(key),
        color=f"#{accent.value:06x}",
        title=str(title or "").strip(),
        paragraphs=(str(body or "").strip(),) if str(body or "").strip() else (),
        content_line=str(content_line or "").strip(),
        author=Author(name=str(author), icon_url=str(author_icon))
        if str(author or "").strip()
        else None,
        thumbnail=str(thumbnail or ""),
        image=str(image or ""),
        footer=str(footer or ""),
        fields=tuple(
            Field(name=str(name), value=str(value), inline=bool(inline))
            for name, value, inline in fields
        ),
        actions=tuple(
            Action(label=str(label), url=str(url)) for label, url in buttons if url
        ),
        style=style or "info",
    )
    compiled = compile_card(card, {})
    payload, _content = _payload_for_card(bot, guild_id, key, compiled.card, theme, {})
    return payload


#: the pipeline ---------------------------------------------------------------


def _is_instant(value: Any) -> bool:
    """Whether a ``timestamp`` value is a time token or an epoch."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and value.strip().startswith("<t:")


async def _canonical_variables(
    bot,
    guild_id: int,
    variables: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve the identity twins and the readable event date before a send.

    A card reads ``user_name``/``user_avatar`` (and the same twins of every
    other user variable), never the raw mention: an embed author/footer does
    not parse ``<@id>``. The ``timestamp`` variable follows the same rule: a
    legacy relative token becomes the readable date in the guild's timezone,
    while ``epoch`` still feeds the native embed timestamp.
    """
    if variables is None:
        return None
    values = dict(variables)
    guild = None
    get_guild = getattr(bot, "get_guild", None)
    if callable(get_guild):
        guild = get_guild(guild_id)
    inject_identities(bot, guild, values)
    if _is_instant(values.get("timestamp")):
        values.update(await event_time(bot, guild_id, values["timestamp"]))
    return values


async def render_card_messages(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
    *,
    silent: bool = False,
    form: str | None = None,
) -> list[RenderedMessage]:
    """Render a card as the messages it ships in (one, or a themed sequence).

    Each message carries its own :class:`CardPayload` and its own
    ``allowed_mentions``: Discord applies the mention policy per message, so
    a two-message card pings only where its document puts the mention.

    A message whose copy the catalog does not have is dropped (the card
    never ships an empty frame), and an invalid message is logged and
    dropped too. An empty list means the card says nothing at all, so the
    call site keeps its own fallback.
    """
    from rosemary.core.debug import trace_card_path

    variables = await _canonical_variables(bot, guild_id, variables)
    cards = await get_effective_cards(bot, guild_id, key, variables, form=form)
    if not cards:
        return []
    theme = theme_for(bot, guild_id)
    base_mapping = {
        **(getattr(theme, "emojis", {}) or {}),
        **(dict(ECHO_VARIABLES) if variables is None else variables),
    }
    rendered: list[RenderedMessage] = []
    for card in cards:
        mapping = reserve_contract(card, base_mapping, variables)
        try:
            if card.blocks:
                # A blocks-native card (the theme's own layout): the legacy
                # validator still guards the document it draws, so an invalid
                # one falls back to the default message.
                issues = validate_document(card_to_v2(card), theme=theme, variables=mapping)
                if issues:
                    raise CardsError(issues)
            compiled = compile_card(card, mapping)
        except CardsError as exc:
            log.warning(
                "card %s for guild %s is invalid, using default: %s", key, guild_id, exc
            )
            continue
        if not _says_something(compiled.card):
            # The card has no copy (the catalog has none for it): there is
            # nothing to say, so the message does not exist.
            continue
        try:
            payload, content = _payload_for_card(
                bot, guild_id, key, compiled.card, theme, mapping
            )
        except CardsError as exc:
            log.warning(
                "card %s for guild %s is invalid, using default: %s", key, guild_id, exc
            )
            continue
        allowed = await _allowed_for_card(bot, guild_id, key, compiled, content, silent)
        rendered.append(RenderedMessage(payload=payload, allowed=allowed))
    if rendered:
        await trace_card_path(bot, guild_id, key)
    return rendered


async def render_card_message(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
    *,
    silent: bool = False,
    form: str | None = None,
) -> tuple[CardPayload | None, discord.AllowedMentions]:
    """Render the first message of a card: ``(payload, allowed_mentions)``.

    The single-message call sites keep this signature. A card that ships as
    a sequence is sent through :func:`render_card_messages`, which returns
    every message in order.

    ``payload`` is a :class:`rosemary.core.card_payload.CardPayload`: a
    Components V2 ``DesignerView``, an ``embed=``-ready bundle when the card
    ships in the embed form, or a plain ``content=`` line when it ships as
    text. ``None`` only when the card resolves no document at all, in which
    case the caller falls back to its own default view. Otherwise
    ``allowed_mentions`` derives from the document itself: only mention
    tokens the resolved text actually contains may ping, and only when the
    theme's pings toggle for the card is on (or the caller forces
    ``silent``). An invalid document renders ``None`` so the caller's default
    path takes over: sends never break on themed content.

    ``form`` forces the shape (a repaint keeps the shape of the message it
    edits: a V2 message gets the V2 form, a classic message the classic
    one).
    """
    messages = await render_card_messages(
        bot, guild_id, key, variables, silent=silent, form=form
    )
    if not messages:
        return None, discord.AllowedMentions.none()
    first = messages[0]
    return first.payload, first.allowed


def _payload_for_card(
    bot,
    guild_id: int,
    key: str,
    card: Card,
    theme: Any,
    mapping: dict[str, Any],
) -> tuple[CardPayload, str]:
    """The payload of a compiled card, in the shape it ships in.

    Returns the payload and the line that carries the mentions: the classic
    forms ping through the content (an embed never notifies, so its ping
    travels in the message content), the Components V2 form pings through
    the ids the compiler collected.
    """
    from rosemary.core.mentions import theme_pings

    if card.form == FORM_TEXT:
        text = card_to_text(card)
        if not text:
            return CardPayload(), ""
        # The copy carries its own icon (a theme emoji): the text form is the
        # copy, verbatim.
        return CardPayload(content=text), text
    if card.form == FORM_EMBED:
        # A box that says nothing does not ship: an empty frame is not a
        # card (and Discord would reject it).
        boxes = tuple(box for box in card.embeds if _says_something(box))
        documents = card_to_embeds(card, boxes)
        embeds, buttons = resolve_embeds(documents, theme, mapping, resolved=True)
        view = build_embed_view(buttons, mapping, key, resolved=True)
        # The content of an embed is the phrase its document declares: the
        # content line (the event sentence, which always ships) or the ping
        # line (the notification phrase, which only ships when the theme's
        # pings toggle is on). When pings are off for the card, the
        # notification line does not ship at all (the embed stays as it is).
        content = card.content_line
        if not content and theme_pings(bot, guild_id, key):
            content = card.ping_line
        return (
            CardPayload(embeds=tuple(embeds), view=view, content=content or None),
            content,
        )
    document = card_to_v2(card, theme)
    view = render_document(
        bot, document, mapping, guild_id=guild_id, card_key=key, resolved=True
    )
    return CardPayload(view=view), ""


async def _allowed_for_card(
    bot,
    guild_id: int,
    key: str,
    compiled: Any,
    content: str,
    silent: bool,
) -> discord.AllowedMentions:
    """The mention policy of a rendered card.

    The Components V2 form pings the ids its mention fields resolved to; the
    classic forms ping through their content line.
    """
    from rosemary.core.mentions import allowed_for_ids, allowed_for_text

    if compiled.card.form == FORM_CARD:
        return await allowed_for_ids(
            bot,
            guild_id,
            key,
            user_ids=compiled.users,
            role_ids=compiled.roles,
            silent=silent,
        )
    return await allowed_for_text(bot, guild_id, key, content, silent=silent)


def _says_something(card: Card) -> bool:
    """Whether a card carries any copy at all.

    The copy is what the member reads: the title, the body, the fields, the
    blocks, the line of a text card or the content line that opens an embed
    card. The frame (the footer, the thumbnail, the image) is not copy: a
    card whose copy the catalog does not have (a missing key, a translator
    without the catalog) says nothing, so the call site keeps its own
    fallback instead of shipping an empty frame.
    """
    if (
        card.content
        or card.content_line
        or card.blocks
        or card.title
        or card.paragraphs
        or card.fields
    ):
        return True
    # A stack of embeds: the copy may live in the boxes alone.
    if any(_says_something(embed) for embed in card.embeds):
        return True
    return card.author is not None and bool(card.author.name)


#: the repaint ----------------------------------------------------------------


async def align_to_message(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None,
    payload: CardPayload | None,
    allowed: discord.AllowedMentions,
    message: Any,
    *,
    silent: bool = False,
) -> tuple[CardPayload | None, discord.AllowedMentions]:
    """Fit a rendered card to the shape of the message it will edit.

    Discord's ``IS_COMPONENTS_V2`` flag is immutable: a V2 message can
    never carry an embed or content, and a classic message never becomes
    V2. A repaint that ignores this ends in a 400 (error 50035), so the
    card follows the message:

    * V2 message, classic card: the card is rendered again in the V2 form;
    * classic message, V2 card: the repaint is skipped (the old content
      stays: the flag cannot be added).

    Returns ``(payload, allowed)``; a ``None`` payload means "skip this
    repaint" (the caller keeps the message as it is).
    """
    if payload is None:
        return None, allowed
    message_v2 = is_v2_message(message)
    payload_v2 = is_v2_payload(payload)
    if message_v2 == payload_v2:
        return payload, allowed
    if message_v2:
        # The message is Components V2: it cannot carry an embed or content.
        rendered, rendered_allowed = await render_card_message(
            bot, guild_id, key, variables, form=FORM_CARD, silent=silent
        )
        if rendered is None or not is_v2_payload(rendered):
            log.warning(
                "card %s has no Components V2 form for guild %s: repaint skipped",
                key,
                guild_id,
            )
            return None, rendered_allowed
        return rendered, rendered_allowed
    # A classic message cannot become Components V2: the flag is immutable.
    log.warning(
        "card %s is Components V2 but message %s is classic: repaint skipped",
        key,
        getattr(message, "id", "?"),
    )
    return None, allowed


__all__ = [
    "CardPayload",
    "RenderedMessage",
    "align_to_message",
    "get_effective_card",
    "get_effective_cards",
    "is_v2_message",
    "is_v2_payload",
    "render_card_message",
    "render_card_messages",
    "render_document",
    "send_card",
    "simple_payload",
]

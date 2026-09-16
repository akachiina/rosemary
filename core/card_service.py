"""Card resolution and rendering: theme override -> catalog default.

This is the single pipeline every card send goes through:

1. the guild's active theme file may override the card (raw Discord Components
   V2 in the theme, converted at load -- see :mod:`rosemary.core.v2_convert`);
2. otherwise the feature's own default applies: a registered builder, the seed
   map (:data:`SEED_PARTS_BY_KEY`) or plain ``card.<key>``/``<key>`` scalars.

Rendering validates first and raises :class:`CardsError` on invalid content --
send sites treat that as "use the default message", so a bad theme can never
break a send.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import Any

import discord

from rosemary.core.cards import (
    DOCUMENT_VERSION,
    ECHO_VARIABLES,
    CardsError,
    build_embed_view,
    build_items,
    get_default_builder,
    is_embed_document,
    resolve_embed,
    safe_format,
)
from rosemary.core.themes import card_document, card_origin, theme_for

log = logging.getLogger(__name__)

#: Where each card's default copy lives outside ``card.<key>``. Most features
#: render catalog text from their own section (``about.title``,
#: ``bump.messages.thank_you_description``, ...), while ``card.<key>`` only
#: carries labels -- so the map points at the keys the send site really
#: resolves. Entry: ``(container_color | None, heading_key | None, parts)``.
#: A part is a catalog key, a literal template, or a ``(label_key, value_key)``
#: tuple. Used when a theme overrides a card but the feature has no builder:
#: the themed document replaces the whole message, while this map only powers
#: test-sends and previews of *default* content (see :func:`default_document`).
SEED_PARTS_BY_KEY: dict[str, tuple[str | None, str | None, tuple[Any, ...]]] = {
    "about.card": ("brand", "about.title", ("about.text", "about.version")),
    "bump.reminder": (
        "brand",
        "bump.messages.reminder_title",
        ("bump.messages.reminder_description",),
    ),
    "bump.thank_you": (
        "brand",
        "bump.messages.thank_you_title",
        ("bump.messages.thank_you_description",),
    ),
    "bump.leaderboard": (
        "warning",
        "bump.messages.leaderboard_title",
        ("bump.messages.leaderboard_description",),
    ),
    "bump.no_bumps": (
        "info",
        "bump.messages.no_bumps_title",
        ("bump.messages.no_bumps_description",),
    ),
    "bump.anti_camping": (None, None, ("bump.messages.anti_camping",)),
    "bump.schedule.open": (None, None, ("bump.schedule.open_message_default",)),
    "bump.schedule.close": (None, None, ("bump.schedule.close_message_default",)),
    # List cards: the send site builds the body and passes it as {body}.
    "invites.leaderboard": ("info", "invites.leaderboard.title", ("{body}",)),
    "invites.stats": ("info", "invites.stats.title", ("{body}",)),
    "invites.personal": ("info", "invites.personal.title", ("{body}",)),
    "invites.invited_list": ("info", "invites.invited.title", ("{body}",)),
    "invites.server_stats": ("info", "invites.server.title", ("{body}",)),
    "invites.invited_by": ("info", "invites.inviter.title", ("invites.inviter.body",)),
    "tickets.panel": ("brand", "tickets.panel.title", ("tickets.panel.text",)),
    "tickets.created": ("brand", "tickets.created.title", ("tickets.created.text",)),
    "partnerships.invite": (None, None, ("partnerships.invite_text",)),
    "cleaner.confirm": (None, None, ("cleaner.confirm_text",)),
    "boost.preview": (
        "success",
        None,
        (
            "# @{role_name}",
            ("boost.emoji.owner", "{owner}"),
            ("boost.emoji.members", "boost.descriptions.members_count"),
        ),
    ),
    # Code-built cards promoted to CardSpecs: the send site passes the rendered
    # title/body plus card-specific variables; {title}/{body} keep the seed
    # identical to what the code fallback shows.
    # starboard.card is intentionally absent: a registered default builder
    # owns its full rich layout (avatar, gallery, author footer). A seed here
    # would shadow the builder -- the exact bug that made live posts bare.
    "utility.ping": ("info", "ping.title", ("ping.latency",)),
    "utility.serverinfo": ("brand", "serverinfo.title", ("{body}",)),
    "birthdays.list": ("info", "birthdays.list_title", ("{body}",)),
    "partnerships.list": ("info", "partnerships.list_title", ("{body}",)),
    "partnerships.audit": ("warning", "partnerships.audit_title", ("{body}",)),
    "moderation.warnings": ("brand", None, ("{title}", "{body}")),
    "bump.stats": (None, None, ("{title}", "{body}")),
    # Interactive menus: only the heading block is theme-customizable --
    # selects/buttons are code (Discord needs registered callbacks).
    "settings.title": ("brand", "settings.title", ("{body}",)),
    "themes.title": ("brand", "themes.title", ("{body}",)),
    "debug.title": ("info", "debug.title", ("{body}",)),
    "boost.home": ("brand", "boost.titles.home", ("boost.descriptions.user_panel",)),
}


@dataclasses.dataclass(frozen=True)
class CardPayload:
    """One rendered card, ready to send: V2 view **or** classic embed.

    A theme may write any card as Components V2 (default) or as an embed
    (``cards.<key>.embed``). This bundle makes the two shapes interchangeable
    at every send site: ``embed`` is set only for embed cards, and ``view``
    carries the V2 items -- or the classic ActionRow holding an embed card's
    buttons. Send through :func:`send_card` instead of touching the fields.
    """

    view: discord.ui.DesignerView | discord.ui.View | None = None
    embed: discord.Embed | None = None

    def message_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``send``/``respond``/``edit`` with this card."""
        if self.embed is not None:
            kwargs: dict[str, Any] = {"embed": self.embed}
            if self.view is not None:
                kwargs["view"] = self.view
            return kwargs
        return {"view": self.view}


async def send_card(
    target,
    payload: CardPayload | None,
    allowed: discord.AllowedMentions | None = None,
    **kwargs: Any,
):
    """Send a rendered card payload through ``target.send``.

    Dispatches the right keyword for the payload shape -- ``view=`` for V2,
    ``embed=`` (+ classic button view) for embed cards -- so call sites never
    branch on the card form. Interaction responses use
    ``ctx.respond(**payload.message_kwargs(), ...)`` instead.
    """
    if payload is None:
        return None
    if allowed is None:
        allowed = discord.AllowedMentions.none()
    return await target.send(**payload.message_kwargs(), allowed_mentions=allowed, **kwargs)


async def _resolve_parts(raw, guild_id: int, parts: tuple[Any, ...]):
    """Yield each seed part resolved (see :func:`_resolve_seed_part`)."""
    for part in parts:
        yield await _resolve_seed_part(raw, guild_id, part)


async def _resolve_seed_part(raw, guild_id: int, part: Any) -> str:
    """One seed part: catalog key, ``(label_key, value)`` entry, or literal."""
    if isinstance(part, tuple):
        label_key, value_key = part
        label = await raw(guild_id, label_key)
        if not isinstance(label, str) or label == label_key or not label.strip():
            label = label_key.rsplit(".", 1)[-1].replace("_", " ")
        value = await raw(guild_id, value_key)
        if not isinstance(value, str) or value == value_key:
            value = value_key
        return f"**{label}:**\n{value}"
    found = await raw(guild_id, part)
    if isinstance(found, str) and found != part:
        return found
    return part


def _seed_document(bot, guild_id: int | None, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate-free seed doc: theme emojis resolve, placeholders stay literal."""
    from rosemary.core.themes import theme_for

    mapping = {
        **(getattr(theme_for(bot, guild_id), "emojis", {}) or {}),
        **ECHO_VARIABLES,
    }

    def walk(items: list[dict[str, Any]]) -> None:
        for block in items:
            if block.get("type") == "text":
                block["body"] = safe_format(str(block["body"]), mapping)
            walk(block.get("children") or [])

    walk(blocks)
    return {"v": DOCUMENT_VERSION, "blocks": blocks}


async def default_document(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The feature's default document for ``key`` (no theme override).

    Resolution order:
    1. a feature-registered default builder (rich cards with custom layouts);
    2. the declared seed map (:data:`SEED_PARTS_BY_KEY`);
    3. plain ``card.<key>`` / ``<key>`` scalars (DM and log cards);
    4. ``None`` when the feature resolves no catalog copy at all.

    ``variables`` (the send-site contract values) pass through to feature
    builders, so defaults can react to real content (e.g. the starboard card
    skips its gallery when the starred message has no image). Builders must
    accept them as ``**kwargs`` -- seeded previews (no variables) still work.
    """
    builder = get_default_builder(key)
    if builder is not None:
        try:
            doc = await builder(bot, guild_id, **(variables or {}))
        except Exception as exc:
            log.warning("default builder failed for card %s: %s", key, exc)
        else:
            if isinstance(doc, dict) and isinstance(doc.get("blocks"), list):
                return copy.deepcopy(doc)
    raw = getattr(bot.translator, "raw", None)
    if not callable(raw):
        return None
    plan = SEED_PARTS_BY_KEY.get(key)
    if plan is not None:
        color, heading_key, parts = plan
        bodies = [body async for body in _resolve_parts(raw, guild_id, parts) if body.strip()]
        heading = None
        if heading_key is not None:
            found = await raw(guild_id, heading_key)
            if isinstance(found, str) and found.strip() and found != heading_key:
                heading = f"# {found}"
        if heading is None and not bodies:
            return None
        blocks: list[dict[str, Any]] = []
        if heading is not None:
            blocks.append({"type": "text", "body": heading})
        blocks.extend({"type": "text", "body": body} for body in bodies)
        if color is not None:
            blocks = [{"type": "container", "color": color, "children": blocks}]
        return _seed_document(bot, guild_id, blocks)
    lines: list[str] = []
    for candidate_key in (f"card.{key}", key):
        candidate = await raw(guild_id, candidate_key)
        if isinstance(candidate, str) and candidate.strip() and candidate != candidate_key:
            lines.append(candidate)
    if not lines:
        return None
    blocks = [{"type": "text", "body": line} for line in lines]
    return _seed_document(bot, guild_id, blocks)


async def get_effective_document(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Themed override, else the feature's default document (or ``None``)."""
    themed = await card_document(bot, guild_id, key)
    if themed is not None:
        return copy.deepcopy(themed)
    return await default_document(bot, guild_id, key, variables)


async def render_document(
    bot,
    doc: dict[str, Any],
    variables: dict[str, Any] | None = None,
    *,
    guild_id: int | None = None,
    card_key: str | None = None,
    draft: bool = False,
) -> discord.ui.DesignerView:
    """Render one validated document into a Components V2 view.

    ``guild_id`` selects the theme used for emoji tokens and color names --
    the guild's active theme when known, the built-in one otherwise.
    ``CardsError`` is intentionally allowed to propagate so callers can show
    the structured issue instead of silently substituting another message.
    """
    from rosemary.core.card_actions import bind_action_callbacks
    from rosemary.core.themes import theme_for

    items = build_items(
        theme_for(bot, guild_id),
        doc,
        variables or {},
        draft=draft,
        card_key=card_key,
    )
    bind_action_callbacks(items)
    view = discord.ui.DesignerView(store=False)
    for item in items:
        view.add_item(item)
    return view


async def render_card_message(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
    *,
    silent: bool = False,
) -> tuple[CardPayload | None, discord.AllowedMentions]:
    """Render a card for a real send: ``(payload, allowed_mentions)``.

    ``payload`` is a :class:`CardPayload` -- a Components V2 ``DesignerView``
    **or** an ``embed=``-ready bundle when the active theme writes the card
    in embed form. ``None`` only when the card resolves no document at all,
    in which case the caller falls back to its own default view. Otherwise
    ``allowed_mentions`` derives from the document itself: only mention
    tokens the resolved text actually contains may ping, and only when the
    theme's pings toggle for the card is on (or the caller forces
    ``silent``). An invalid document renders ``None`` so the caller's default
    path takes over -- sends never break on themed content.
    """
    from rosemary.core.mentions import allowed_for_document
    from rosemary.core.themes import theme_for

    doc = await get_effective_document(bot, guild_id, key, variables)
    if doc is None:
        return None, discord.AllowedMentions.none()
    mapping = {
        **(getattr(theme_for(bot, guild_id), "emojis", {}) or {}),
        **(dict(ECHO_VARIABLES) if variables is None else variables),
    }
    if is_embed_document(doc):
        try:
            embed, buttons = resolve_embed(doc, theme_for(bot, guild_id), mapping)
            view = build_embed_view(buttons, mapping, key)
        except CardsError as exc:
            log.warning(
                "embed card %s for guild %s is invalid, using default: %s", key, guild_id, exc
            )
            return None, discord.AllowedMentions.none()
        allowed = await allowed_for_document(bot, guild_id, key, doc, mapping, silent=silent)
        await trace_card_path(bot, guild_id, key)
        return CardPayload(embed=embed, view=view), allowed
    try:
        view = await render_document(bot, doc, mapping, guild_id=guild_id, card_key=key)
    except CardsError as exc:
        log.warning("card %s for guild %s is invalid, using default: %s", key, guild_id, exc)
        return None, discord.AllowedMentions.none()
    allowed = await allowed_for_document(bot, guild_id, key, doc, mapping, silent=silent)
    await trace_card_path(bot, guild_id, key)
    return CardPayload(view=view), allowed


async def trace_card_path(bot, guild_id: int, key: str) -> None:
    """Post ``card.<key>`` + origin to the log channel when ``debug.card_paths``
    is on. Never pings (``card_key=None``) and never traces itself.

    Public so call sites that build themed views outside this module
    (ephemeral confirm cards) can trace their own sends."""
    from rosemary.core.debug import send_channel_log
    from rosemary.core.settings import get_setting
    from rosemary.core.themes import card_origin

    try:
        enabled = await get_setting(bot.storage, guild_id, "debug.card_paths")
    except (KeyError, AttributeError):
        return
    if not enabled:
        return
    theme_name, _ = card_origin(bot, guild_id, key)
    origin = (
        await bot.translator.t(guild_id, "debug.trace.theme", theme=theme_name)
        if theme_name
        else await bot.translator.t(guild_id, "debug.trace.default")
    )
    description = await bot.translator.t(
        guild_id, "debug.trace.card_path", path=f"card.{key}", origin=origin
    )
    await send_channel_log(
        bot,
        guild_id,
        await bot.translator.t(guild_id, "debug.trace.title"),
        description,
        color="info",
    )


def log_default_failure(key: str, exc: Exception) -> None:
    """Warn that a card's default builder crashed (seed falls through)."""
    log.warning("default builder failed for card %s: %s", key, exc)


async def menu_heading_items(
    bot, guild_id: int, key: str
) -> list[discord.ui.ViewItem]:
    """Themed heading items for an interactive menu (empty = code default).

    Menus are interactive: their selects and buttons live in code -- Discord
    needs registered callbacks, so a theme file cannot generate them. What a
    theme *can* restyle is the heading card (``settings.title``,
    ``themes.title``, ``debug.title``, ``boost.home``/``boost.admin``). The
    themed document is rendered and spliced in place of the code heading; a
    container in the theme document is rejected (the menu already provides
    one, and nesting containers 400s the payload) so the menu falls back to
    its built-in heading instead of failing the send.
    """
    from rosemary.core.cards import is_embed_document, maybe_view
    from rosemary.core.themes import card_document
    from rosemary.ui.containers import Container

    doc = await card_document(bot, guild_id, key)
    if doc is not None and is_embed_document(doc):
        log.warning(
            "theme heading %s is an embed; menu headings must be V2 text", key
        )
        return []
    view = await maybe_view(bot, guild_id, key)
    if view is None:
        return []
    items = list(view.children)
    if any(isinstance(item, Container) for item in items):
        log.warning(
            "theme heading %s must not contain a container; using default", key
        )
        return []
    return items


__all__ = [
    "SEED_PARTS_BY_KEY",
    "CardPayload",
    "card_origin",
    "default_document",
    "get_effective_document",
    "log_default_failure",
    "menu_heading_items",
    "render_card_message",
    "render_document",
    "send_card",
    "theme_for",
    "trace_card_path",
]

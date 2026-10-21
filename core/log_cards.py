"""Default builders for staff log events.

Logs use the same document/render pipeline as every other card: a
:class:`~rosemary.core.log_specs.LogSpec` declares metadata, this module turns
it into a Components V2 document, and ``render_card_message`` resolves theme,
mentions and the per-event ``cards.<event>`` override.

The old body card (``<event>.description``) remains a *body* override: when a
guild theme has it, the builder uses that text and keeps the new title, color
and footer. A full ``cards.<event>`` override wins before the builder runs.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.cards import set_default_builder
from rosemary.core.log_specs import LOG_SPECS, LogSpec
from rosemary.core.themes import theme_for

_ACTOR_FOOTER_FALLBACK = "-# by {actor} · {timestamp}"
_SYSTEM_FOOTER_FALLBACK = "-# {timestamp}"


async def _raw(bot, guild_id: int, key: str) -> str | None:
    raw = getattr(bot.translator, "raw", None)
    if callable(raw):
        found = await raw(guild_id, key)
        if isinstance(found, str) and found.strip() and found != key:
            return found
    return None


async def _text(bot, guild_id: int, key: str, fallback: str = "") -> str:
    found = await _raw(bot, guild_id, key)
    if found is not None:
        return found
    translate = getattr(bot.translator, "t", None)
    if callable(translate):
        found = await translate(guild_id, key)
        if isinstance(found, str) and found.strip() and found != key:
            return found
    return fallback or key


def _title_markdown(theme, title: str) -> str:
    """Apply the theme's title preset; keep ``# title`` for bare themes."""
    md = getattr(theme, "md", None)
    if callable(md):
        try:
            return md("title", title=title)
        except (KeyError, ValueError):
            pass
    return f"# {title}"


async def _legacy_body(bot, guild_id: int, spec: LogSpec, variables: dict[str, Any]) -> str | None:
    """Return the old body-card override, only when the theme really has it."""
    key = spec.legacy_key
    if not key:
        return None
    theme = theme_for(bot, guild_id)
    cards = getattr(theme, "cards", None)
    if not isinstance(cards, dict):
        return None

    from rosemary.core.cards import maybe_flat_text

    return await maybe_flat_text(bot, guild_id, key, **variables)


async def _field_lines(
    bot, guild_id: int, spec: LogSpec, variables: dict[str, Any]
) -> list[str]:
    lines: list[str] = []
    for field in spec.fields:
        value = variables.get(field.variable)
        if value is None or (isinstance(value, str) and not value.strip()):
            if field.optional:
                continue
            value = "-"
        label = await _text(bot, guild_id, field.label, fallback=field.label)
        lines.append(f"**{label}:** {value}")
    return lines


async def default_log_document(
    bot,
    guild_id: int,
    spec: LogSpec,
    **variables: Any,
) -> dict[str, Any]:
    """Assemble the default document for one log event.

    Events that declare ``avatar_var`` use the audit-style frame: a section
    with the target avatar on the right, title and content on the left. A
    missing avatar falls back to plain text so a theme preview or a partial
    send never renders an empty accessory.
    """
    title = await _text(bot, guild_id, spec.title_key, fallback=spec.title_key)
    heading = _title_markdown(theme_for(bot, guild_id), title)
    content: list[dict[str, Any]] = [{"type": "text", "body": heading}]
    gallery: dict[str, Any] | None = None

    body = await _legacy_body(bot, guild_id, spec, variables)
    if body:
        content.append({"type": "text", "body": body})
    elif spec.layout == "fields":
        lines = await _field_lines(bot, guild_id, spec, variables)
        if lines:
            content.append({"type": "text", "body": "\n".join(lines)})
    else:
        body_key = variables.get("body_key") or spec.body_key
        body = await _text(bot, guild_id, str(body_key), fallback=str(body_key))
        content.append({"type": "text", "body": body})
        if spec.layout == "media":
            image_url = variables.get("image_url")
            if image_url:
                gallery = {"type": "gallery", "urls": [str(image_url)]}

    avatar_url = variables.get(spec.avatar_var) if spec.avatar_var else None
    if isinstance(avatar_url, str) and avatar_url.strip():
        children: list[dict[str, Any]] = [
            {
                "type": "section",
                "accessory": {"type": "thumbnail", "url": avatar_url.strip()},
                "children": content,
            }
        ]
    else:
        children = content
    if gallery is not None:
        children.append(gallery)

    if spec.footer != "none":
        actor = variables.get("actor")
        if spec.footer == "actor" and actor:
            footer_key = "log.actor_footer"
            footer = await _raw(bot, guild_id, footer_key) or _ACTOR_FOOTER_FALLBACK
        else:
            footer_key = "log.system_footer"
            footer = await _raw(bot, guild_id, footer_key) or _SYSTEM_FOOTER_FALLBACK
        children.extend(
            [
                {"type": "divider"},
                {"type": "text", "body": footer},
            ]
        )

    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": spec.severity,
                "children": children,
            }
        ],
    }


def _make_builder(spec: LogSpec):
    async def builder(bot, guild_id: int, **variables: Any) -> dict[str, Any]:
        return await default_log_document(bot, guild_id, spec, **variables)

    builder.__name__ = f"log_{spec.event.replace('.', '_')}"
    return builder


def register_log_builders() -> None:
    """Register one default builder per log event (idempotent)."""
    for spec in LOG_SPECS.values():
        set_default_builder(spec.event, _make_builder(spec))


# Importing this module is enough for every log card to resolve its default.
register_log_builders()

__all__ = ["default_log_document", "register_log_builders"]

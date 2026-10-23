"""Default builders for the moderation warnings card.

``/warnings`` reacts to real content (the inspected member's avatar and
whether they have any warnings), so its rich layout lives in code instead of
a catalog seed (the same rule as ``core/bump_cards.py`` and
``starboard.card``: a seed here would shadow the builder and strip the
thumbnail from live cards).

The container color is state-driven: a list of warnings renders on the
``moderation_warnings`` style, the empty state on ``success`` (the member is
clean, good news). Both resolve through the theme styles, so restyling a
theme file changes the live card; an unknown style degrades to ``base``.
"""

from __future__ import annotations

from rosemary.core.themes import theme_for


def _heading(theme, title: str) -> str:
    """Apply the theme's title preset; keep ``# title`` for bare themes."""
    md = getattr(theme, "md", None)
    if callable(md):
        try:
            return md("title", title=title)
        except (KeyError, ValueError):
            pass
    return f"# {title}"


def _container(color: str, children: list[dict]) -> dict:
    blocks = [child for child in children if child]
    return {"v": 1, "blocks": [{"type": "container", "color": color, "children": blocks}]}


async def default_moderation_warnings_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``moderation.warnings``: avatar + state-driven color."""
    from rosemary.core.cards import author_footer_blocks

    theme = theme_for(bot, guild_id)
    avatar = str(variables.get("user_avatar") or "")
    title = str(variables.get("title") or "")
    body = str(variables.get("body") or "")
    section = {
        "type": "section",
        **({"accessory": {"type": "thumbnail", "url": avatar}} if avatar else {}),
        "children": [{"type": "text", "body": _heading(theme, title)}],
    }
    children: list[dict] = [section]
    if body:
        children.append({"type": "text", "body": body})
    children.extend(await author_footer_blocks(bot, guild_id))
    state_style = "success" if not variables.get("has_warnings", True) else (
        "moderation_warnings"
    )
    return _container(theme.style(state_style).color, children)


def register_moderation_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("moderation.warnings", default_moderation_warnings_document)


register_moderation_builders()

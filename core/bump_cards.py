"""Default builders for the bump leaderboard/thank-you cards.

These two cards react to real content (the winner's avatar thumbnail), so
their rich layout lives in code instead of a catalog seed (the same rule as
``starboard.card`` and ``utility.serverinfo``: a seed here would shadow the
builder and strip the avatar from live posts).

Container colors resolve through the theme styles (``bump_reminder`` /
``bump_leaderboard``), the same indirection the code fallbacks use: restyling
a theme file changes the live cards, and an unknown style token degrades to
``base`` instead of breaking the send.
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


async def _body_template(bot, guild_id: int, key: str) -> str:
    """Raw description template: its ``{variables}`` resolve at render."""
    template = await bot.translator.raw(guild_id, key)
    if not isinstance(template, str) or template == key:
        return ""
    return template


def _container(color: str, children: list[dict]) -> dict:
    blocks = [child for child in children if child]
    return {"v": 1, "blocks": [{"type": "container", "color": color, "children": blocks}]}


def _style_color(bot, guild_id: int, name: str) -> str:
    """The container color token behind a theme style (unknown style -> base)."""
    theme = theme_for(bot, guild_id)
    return theme.style(name).color


async def default_bump_thank_you_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``bump.thank_you``: avatar thumbnail when available."""
    from rosemary.core.cards import author_footer_blocks

    theme = theme_for(bot, guild_id)
    avatar = str(variables.get("user_avatar") or "")
    title = await bot.translator.t(guild_id, "bump.messages.thank_you_title")
    section = {
        "type": "section",
        **({"accessory": {"type": "thumbnail", "url": avatar}} if avatar else {}),
        "children": [{"type": "text", "body": _heading(theme, title)}],
    }
    body = await _body_template(bot, guild_id, "bump.messages.thank_you_description")
    children: list[dict] = [section]
    if body:
        children.append({"type": "text", "body": body})
    children.extend(await author_footer_blocks(bot, guild_id))
    return _container(_style_color(bot, guild_id, "bump_reminder"), children)


async def default_bump_leaderboard_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``bump.leaderboard``: winner avatar + list + footer."""
    from rosemary.core.cards import author_footer_blocks

    theme = theme_for(bot, guild_id)
    avatar = str(variables.get("winner_avatar") or "")
    title = await bot.translator.t(guild_id, "bump.messages.leaderboard_title")
    section = {
        "type": "section",
        **({"accessory": {"type": "thumbnail", "url": avatar}} if avatar else {}),
        "children": [{"type": "text", "body": _heading(theme, title)}],
    }
    body = await _body_template(bot, guild_id, "bump.messages.leaderboard_description")
    children: list[dict] = [section]
    if body:
        children.append({"type": "text", "body": body})
    children.extend(await author_footer_blocks(bot, guild_id))
    return _container(_style_color(bot, guild_id, "bump_leaderboard"), children)


def register_bump_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("bump.thank_you", default_bump_thank_you_document)
    set_default_builder("bump.leaderboard", default_bump_leaderboard_document)


register_bump_builders()

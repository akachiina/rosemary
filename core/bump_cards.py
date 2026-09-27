"""Default builders for the bump leaderboard/thank-you cards.

These two cards react to real content (the winner's avatar thumbnail), so
their rich layout lives in code instead of a catalog seed (the same rule as
``starboard.card`` and ``utility.serverinfo``: a seed here would shadow the
builder and strip the avatar from live posts).
"""

from __future__ import annotations


def _avatar_url(bot, member) -> str:
    return getattr(getattr(member, "display_avatar", None), "url", "") or ""


async def _heading(bot, guild_id: int, key: str) -> str:
    template = await bot.translator.t(guild_id, key)
    return bot.theme.md("title", title=template)


async def _body_template(bot, guild_id: int, key: str) -> str:
    """Raw description template: its ``{variables}`` resolve at render."""
    template = await bot.translator.raw(guild_id, key)
    if not isinstance(template, str) or template == key:
        return ""
    return template


def _container(color: str, children: list[dict]) -> dict:
    blocks = [child for child in children if child]
    return {"v": 1, "blocks": [{"type": "container", "color": color, "children": blocks}]}


async def default_bump_thank_you_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``bump.thank_you``: avatar thumbnail when available."""
    from rosemary.core.cards import author_footer_blocks

    avatar = str(variables.get("user_avatar") or "")
    section = {
        "type": "section",
        **({"accessory": {"type": "thumbnail", "url": avatar}} if avatar else {}),
        "children": [{"type": "text", "body": await _heading(
            bot, guild_id, "bump.messages.thank_you_title"
        )}],
    }
    body = await _body_template(bot, guild_id, "bump.messages.thank_you_description")
    children: list[dict] = [section]
    if body:
        children.append({"type": "text", "body": body})
    children.extend(await author_footer_blocks(bot, guild_id))
    return _container("brand", children)


async def default_bump_leaderboard_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``bump.leaderboard``: winner avatar + list + footer."""
    from rosemary.core.cards import author_footer_blocks

    avatar = str(variables.get("winner_avatar") or "")
    section = {
        "type": "section",
        **({"accessory": {"type": "thumbnail", "url": avatar}} if avatar else {}),
        "children": [{"type": "text", "body": await _heading(
            bot, guild_id, "bump.messages.leaderboard_title"
        )}],
    }
    body = await _body_template(bot, guild_id, "bump.messages.leaderboard_description")
    children: list[dict] = [section]
    if body:
        children.append({"type": "text", "body": body})
    children.extend(await author_footer_blocks(bot, guild_id))
    return _container("warning", children)


def register_bump_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("bump.thank_you", default_bump_thank_you_document)
    set_default_builder("bump.leaderboard", default_bump_leaderboard_document)


register_bump_builders()

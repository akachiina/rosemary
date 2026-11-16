"""Theme tools: what a theme does, before the guild picks it.

Three questions the ``/themes`` panel answers about a theme:

* **validate**: does the theme load, and does its content make sense? The
  cards it names exist, the copy it points at exists in the catalog, the
  colors it uses exist, and the variables it fills are the ones the send
  site provides.
* **preview**: how does a card of the theme look in each shape (the plain
  text, the classic embed, the Components V2)?
* **diff**: what does the theme change against the built-in one?

The module is pure: it reads and compares, it never sends. The panel
formats the result (and the copy lives in the catalogs).
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import Any

import discord

from rosemary.core.documents.merge import DocumentError, resolve_layers
from rosemary.core.documents.templates import TOKEN_RE
from rosemary.core.theme_package import default_package

#: The shapes a card may ship in (the preview offers each one).
FORMS = ("text", "embed", "card")


@dataclasses.dataclass(frozen=True)
class ThemeIssue:
    """One problem of a theme: the code, where it is and what it says."""

    code: str
    where: str
    detail: str


@dataclasses.dataclass(frozen=True)
class ThemeDiff:
    """What a theme changes against the built-in one."""

    extends: str | None = None
    colors: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    emojis: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    styles: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    cards: tuple[str, ...] = ()
    deltas: tuple[str, ...] = ()
    pings: dict[str, bool] = dataclasses.field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Whether the theme changes nothing (it is the built-in one)."""
        return not (
            self.colors
            or self.emojis
            or self.styles
            or self.cards
            or self.deltas
            or self.pings
        )


def diff_theme(theme: Any, base: Any) -> ThemeDiff:
    """What ``theme`` changes against ``base`` (the built-in one).

    The tokens are compared leaf by leaf (the styles too), so the diff says
    exactly which values moved. The cards are listed by key: a **legacy
    full card** replaces the card, a **delta** customizes it.
    """
    colors = _changed(base.colors, theme.colors)
    emojis = _changed(base.emojis, theme.emojis)
    styles = _changed_styles(base.styles, theme.styles)
    cards = tuple(sorted(getattr(theme, "cards", {}) or {}))
    deltas = tuple(sorted(getattr(theme, "deltas", {}) or {}))
    pings = {
        key: bool(value)
        for key, value in (getattr(theme, "pings", {}) or {}).items()
        if bool(value) != bool((getattr(base, "pings", {}) or {}).get(key))
    }
    return ThemeDiff(
        extends=getattr(theme, "extends", None),
        colors=colors,
        emojis=emojis,
        styles=styles,
        cards=cards,
        deltas=deltas,
        pings=pings,
    )


def _changed(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """The tokens whose value differs (the base value, the new one)."""
    out: dict[str, tuple[str, str]] = {}
    for key, value in (overlay or {}).items():
        before = (base or {}).get(key)
        if before != value:
            out[key] = (str(before or ""), str(value))
    return out


def _changed_styles(
    base: dict[str, dict[str, str]], overlay: dict[str, dict[str, str]]
) -> dict[str, tuple[str, str]]:
    """The style fields that differ (``style.field`` -> the two values)."""
    out: dict[str, tuple[str, str]] = {}
    for name, fields in (overlay or {}).items():
        before = (base or {}).get(name) or {}
        for field, value in (fields or {}).items():
            if before.get(field) != value:
                out[f"{name}.{field}"] = (str(before.get(field, "")), str(value))
    return out


async def validate_theme(bot, guild_id: int, theme: Any) -> list[ThemeIssue]:
    """The problems of a theme (an empty list means the theme is sound).

    The loader already refuses a theme that does not parse; this checks the
    *meaning*: a card that does not exist, a copy that the catalog does not
    have, a color token nobody defines and a variable the send site never
    provides are all silent holes in the UI, and this is where they surface.
    """
    issues: list[ThemeIssue] = []
    deltas = getattr(theme, "deltas", {}) or {}
    cards = getattr(theme, "cards", {}) or {}
    for key in sorted(set(deltas) | set(cards)):
        if not _known_card(key):
            issues.append(
                ThemeIssue("theme_unknown_card", key, f"{key}: no card with this key")
            )
    for key, doc in sorted(deltas.items()):
        issues.extend(await _validate_delta(bot, guild_id, theme, key, doc))
    for key in sorted(getattr(theme, "pings", {}) or {}):
        if not _known_card(key):
            issues.append(
                ThemeIssue("theme_unknown_card", key, f"{key}: no card with this key")
            )
    return issues


async def _validate_delta(
    bot, guild_id: int, theme: Any, key: str, doc: dict[str, Any]
) -> list[ThemeIssue]:
    """One delta checked: the frames, the color, the copy and the variables."""
    issues: list[ThemeIssue] = []
    archetypes = getattr(theme, "archetypes", {}) or {}
    fragments = getattr(theme, "fragments", {}) or {}
    try:
        resolved = resolve_layers(doc, archetypes, fragments)
    except DocumentError as exc:
        return [ThemeIssue(exc.code, key, str(exc))]
    issues.extend(await _validate_document(bot, guild_id, theme, key, resolved))
    color = resolved.get("color")
    for token in color if isinstance(color, list) else [color]:
        if not isinstance(token, str):
            continue
        value = token.strip()
        if not value or "{" in value:
            continue
        if value.startswith("#") or value in (getattr(theme, "colors", {}) or {}):
            continue
        issues.append(
            ThemeIssue("theme_unknown_color", key, f"{value}: no color token")
        )
    contract = _contract(key)
    emojis = getattr(theme, "emojis", {}) or {}
    for text in _texts(resolved):
        for match in TOKEN_RE.finditer(text):
            dotted = match.group("dotted")
            if dotted is not None:
                found = await _catalog_copy(bot, guild_id, dotted)
                if not found:
                    issues.append(
                        ThemeIssue(
                            "theme_unknown_copy",
                            key,
                            f"{dotted}: the catalog has no copy for it",
                        )
                    )
                continue
            name = (match.group("plain") or match.group("mention") or "").lstrip("@")
            if not name or name in contract or name in emojis:
                continue
            issues.append(
                ThemeIssue(
                    "theme_unknown_variable",
                    key,
                    f"{name}: the send site does not provide it",
                )
            )
    return issues


async def _validate_document(
    bot, guild_id: int, theme: Any, key: str, resolved: dict[str, Any]
) -> list[ThemeIssue]:
    """The delta read into the model: a broken slot is reported here.

    The document is merged over the packaged one (the same merge the send
    path does) and read into the card model: a slot the reader refuses (a
    message slot inside a box of the ``embeds`` list, a stack of eleven
    boxes, a stack in a shape that has no place for it) is a hole in the
    theme, and the panel says so instead of the send silently falling back.
    """
    from rosemary.core.card_resolution import _merge_delta
    from rosemary.core.documents.semantic import build_cards
    from rosemary.core.documents.templates import Resolver

    base = default_package().document(key) or {}
    try:
        merged = _merge_delta(base, resolved)
    except DocumentError as exc:
        return [ThemeIssue(exc.code, key, str(exc))]
    resolver = Resolver(
        bot, guild_id, {}, emojis=getattr(theme, "emojis", {}) or {}
    )
    try:
        await build_cards(merged, key, resolver)
    except DocumentError as exc:
        return [ThemeIssue(exc.code, key, str(exc))]
    return []


async def _catalog_copy(bot, guild_id: int, key: str) -> str:
    """The copy of a catalog key (``""`` when the catalog has none)."""
    raw = getattr(getattr(bot, "translator", None), "raw", None)
    if not callable(raw):
        return ""
    found = await raw(guild_id, key)
    if not isinstance(found, str) or not found.strip() or found == key:
        return ""
    return found


def _known_card(key: str) -> bool:
    """Whether a card key exists (the package or the code-built registry)."""
    from rosemary.core.cards import get_card

    if key in default_package().card_keys():
        return True
    return get_card(key) is not None


def _contract(key: str) -> tuple[str, ...]:
    """The variables the send site provides for a card (its contract)."""
    from rosemary.core.cards import get_card

    metadata = default_package().metadata(key)
    if metadata.get("variables"):
        return tuple(metadata["variables"])
    spec = get_card(key)
    if spec is not None:
        return tuple(spec.variables)
    return ()


def _texts(doc: Any) -> list[str]:
    """Every string of a document (wherever it sits)."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            out.append(node)

    walk(doc)
    return out


async def preview_card(
    bot, guild_id: int, key: str, form: str, theme: Any = None, user_id: int | None = None
) -> tuple[Any, Any]:
    """Render a card of a theme in one shape.

    The variables are the samples of the contract, but the ones the preview
    can know for real (the bot's version, the guild, the invoker, the bot's
    own avatar) are filled with real values, so the admin sees a preview
    close to what a member would receive instead of a wall of samples.

    ``theme`` previews a theme that is not the active one: the guild's
    snapshot is swapped for the render and restored right after, so the
    admin sees the theme they picked, not the one in use.
    """
    from rosemary.core.card_service import render_card_message
    from rosemary.core.cards import preview_variables

    contract = _contract(key)
    variables = preview_variables(*contract)
    real = await _real_variables(bot, guild_id, getattr(theme, "name", None), user_id)
    for name in contract:
        if name in real:
            variables[name] = real[name]
    with _using_theme(bot, guild_id, theme):
        return await render_card_message(bot, guild_id, key, variables, form=form)


async def _real_variables(
    bot, guild_id: int, theme_name: str | None, user_id: int | None
) -> dict[str, Any]:
    """Real values for the variables the preview can know (the rest stays sample).

    The preview is generic, but a few variables are the same for every send:
    the bot's version, the host line, the guild name/counts, the bot's own
    avatar and the invoker (the admin clicking preview). Filling them makes
    the preview look like the real card instead of a wall of samples.
    """
    from rosemary import __version__

    values: dict[str, Any] = {
        "version": __version__,
        "theme": theme_name or "default",
        "origin": theme_name or "default",
        "timestamp": discord.utils.format_dt(discord.utils.utcnow(), style="R"),
    }

    host = getattr(bot, "host", None)
    if host is not None:
        values["channel"] = getattr(host, "update_channel", "stable")
        identity = getattr(host, "identity_line", None)
        if callable(identity):
            values["host_line"] = identity()

    guild = None
    get_guild = getattr(bot, "get_guild", None)
    if callable(get_guild) and guild_id is not None:
        guild = get_guild(guild_id)
    if guild is not None:
        name = getattr(guild, "name", "") or ""
        values.update(
            {
                "server": name,
                "guild": name,
                "server_name": name,
                "members": getattr(guild, "member_count", 0) or 0,
            }
        )
        icon = getattr(guild, "icon", None)
        if icon is not None:
            values["server_icon"] = icon.url
        banner = getattr(guild, "banner", None)
        if banner is not None:
            values["banner_url"] = banner.url
        bot_user = getattr(bot, "user", None)
        get_member = getattr(guild, "get_member", None)
        if bot_user is not None and callable(get_member):
            me = get_member(bot_user.id)
            if me is not None:
                values["bot_avatar"] = me.display_avatar.url
                me_banner = getattr(me, "guild_banner", None) or getattr(me, "banner", None)
                if me_banner is not None:
                    values["bot_banner"] = me_banner.url

    user = None
    if guild is not None and user_id is not None and callable(getattr(guild, "get_member", None)):
        user = guild.get_member(user_id)
    if user is None and user_id is not None:
        get_user = getattr(bot, "get_user", None)
        if callable(get_user):
            user = get_user(user_id)
    if user is not None:
        mention = getattr(user, "mention", "")
        display = getattr(user, "display_name", "") or getattr(user, "name", "")
        avatar = getattr(user, "display_avatar", None)
        avatar_url = avatar.url if avatar is not None else ""
        values.update(
            {
                "user": mention,
                "user_mention": mention,
                "user_name": display,
                "user_avatar": avatar_url,
                "user_id": str(getattr(user, "id", "")),
                "member": mention,
                "member_mention": mention,
                "member_name": display,
                "member_avatar": avatar_url,
                "member_id": str(getattr(user, "id", "")),
                "author": mention,
                "author_name": display,
                "author_avatar": avatar_url,
            }
        )

    if guild_id is not None:
        try:
            from rosemary.core.settings import get_setting

            prefix = await get_setting(bot.storage, guild_id, "general.prefix")
            if prefix:
                values["prefix"] = prefix
        except Exception:
            pass
    return values


@contextlib.contextmanager
def _using_theme(bot, guild_id: int, theme: Any):
    """Render as if ``theme`` were the guild's active one (then restore)."""
    if theme is None:
        yield
        return
    cache = getattr(bot, "_theme_cache", None)
    if not isinstance(cache, dict):
        yield
        return
    before = cache.get(guild_id)
    cache[guild_id] = theme
    try:
        yield
    finally:
        if before is None:
            cache.pop(guild_id, None)
        else:
            cache[guild_id] = before


__all__ = [
    "FORMS",
    "ThemeDiff",
    "ThemeIssue",
    "diff_theme",
    "preview_card",
    "validate_theme",
]

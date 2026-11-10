"""The canonical identity block of a card.

A classic embed does not parse a mention nor a timestamp in its author and
footer lines: a ``<@id>`` there renders as raw text. The send sites therefore
hand the document a display name and an avatar URL instead of the token. This
module resolves them once, from a member object, a mention string or a user
id, so every card speaks the same identity vocabulary.

The event clock lives here too: ``timestamp`` is the readable date in the
guild's timezone and language format, while ``epoch`` is the integer instant
the embed renders natively (``<t:...>``) in the reader's own timezone.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rosemary.core.settings import get_setting
from rosemary.core.timezone import resolve_timezone

if TYPE_CHECKING:
    from rosemary.bot import RosemaryBot

log = logging.getLogger(__name__)

#: A user mention (never a role: a role is not an identity).
_MENTION_RE = re.compile(r"^<@[!]?([0-9]+)>$")
#: A Discord time token (``<t:1767225600:R>``).
_TIME_RE = re.compile(r"^<t:([0-9]+)(?::[tTdDfFR])?>$")
#: The fallback date format when a catalog has no ``general.date_format``.
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M"


def _user_id(value: Any) -> int | None:
    """The id of a user object, a mention string or an integer id."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    found = getattr(value, "id", None)
    if isinstance(found, int) and not isinstance(found, bool):
        return found
    if isinstance(value, str):
        text = value.strip()
        match = _MENTION_RE.fullmatch(text)
        if match is not None:
            return int(match.group(1))
        if text.isdigit():
            return int(text)
    return None


def _fields(user: Any) -> dict[str, str]:
    """The identity block of a user-like object (empty when it is not one)."""
    user_id = _user_id(user)
    if user_id is None:
        return {}
    mention = getattr(user, "mention", "")
    if not isinstance(mention, str) or "<@" not in mention:
        mention = f"<@{user_id}>"
    name = ""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(user, attribute, "")
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    avatar = ""
    display_avatar = getattr(user, "display_avatar", None)
    url = getattr(display_avatar, "url", "") if display_avatar is not None else ""
    if not isinstance(url, str) or not url:
        avatar_object = getattr(user, "avatar", None)
        url = getattr(avatar_object, "url", "") if avatar_object is not None else ""
    if isinstance(url, str):
        avatar = url
    return {
        "user": mention,
        "user_name": name,
        "user_avatar": avatar,
        "user_id": str(user_id),
    }


def _lookup(bot: RosemaryBot | None, guild: Any, user_id: int) -> dict[str, str]:
    """The identity block resolved from the guild cache, then the bot cache."""
    if guild is not None:
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            found = _fields(get_member(user_id))
            if found:
                return found
    if bot is not None:
        get_user = getattr(bot, "get_user", None)
        if callable(get_user):
            found = _fields(get_user(user_id))
            if found:
                return found
    return {}


def identity(
    bot: RosemaryBot | None,
    guild: Any,
    user: Any,
) -> dict[str, str]:
    """The canonical ``user`` block of one card.

    ``user`` may be a member/user object, a ``<@id>`` mention, an integer id
    or a plain display name. A mention the cache does not know still yields
    the id and the mention, but never the raw token as a name: the author
    line falls back to no name instead of ``<@123>``.
    """
    if user is None:
        return {}
    # A string or an integer is an id, not a user object: resolve it through
    # the caches before falling back to the bare mention.
    if not isinstance(user, (str, int)) or isinstance(user, bool):
        found = _fields(user)
        if found:
            return found
    user_id = _user_id(user)
    if user_id is not None:
        found = _lookup(bot, guild, user_id)
        if found:
            return found
        return {
            "user": f"<@{user_id}>",
            "user_name": "",
            "user_avatar": "",
            "user_id": str(user_id),
        }
    if isinstance(user, str) and user.strip():
        text = user.strip()
        return {"user": text, "user_name": text, "user_avatar": "", "user_id": ""}
    return {}


def actor_identity(
    bot: RosemaryBot | None,
    guild: Any,
    actor: Any,
) -> dict[str, str]:
    """The identity block of the actor (the moderator of an event)."""
    block = identity(bot, guild, actor)
    if not block:
        return {}
    return {
        "actor": block.get("user", ""),
        "actor_name": block.get("user_name", ""),
        "actor_avatar": block.get("user_avatar", ""),
        "actor_id": block.get("user_id", ""),
    }


#: The placeholders that may carry a user. A send site may pass the object,
#: the mention or the id; the injection normalizes all three.
IDENTITY_VARIABLES: tuple[str, ...] = (
    "user",
    "actor",
    "member",
    "moderator",
    "inviter",
    "author",
    "owner",
    "invitee",
    "winner",
    "old_owner",
    "new_owner",
)


def inject_identities(
    bot: RosemaryBot | None,
    guild: Any,
    values: dict[str, Any],
    names: tuple[str, ...] = IDENTITY_VARIABLES,
) -> None:
    """Fill the ``<name>_name``/``<name>_avatar`` twins of every user variable.

    A card reads the display name and the avatar from the twins, never from
    the mention: an embed author/footer does not parse ``<@id>``. The mention
    itself is normalized too, so a call site may hand the member object.
    Values that are not users (an empty field, a dash, a role) are left alone.
    """
    for name in names:
        value = values.get(name)
        if value is None:
            continue
        if isinstance(value, str) and (not value.strip() or value.strip() == "-"):
            continue
        block = identity(bot, guild, value)
        if not block:
            continue
        mention = block.get("user", "")
        if mention:
            values[name] = mention
        for suffix in ("name", "avatar", "id"):
            found = block.get(f"user_{suffix}", "")
            if found:
                values.setdefault(f"{name}_{suffix}", found)


def _moment(value: Any) -> datetime:
    """The instant of an event, from an epoch, a time token or a datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        match = _TIME_RE.fullmatch(text)
        if match is not None:
            return datetime.fromtimestamp(int(match.group(1)), tz=UTC)
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=UTC)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


async def event_time(
    bot: RosemaryBot,
    guild_id: int,
    moment: Any = None,
) -> dict[str, Any]:
    """The card clock: the readable date and the integer instant.

    The readable date follows the guild's timezone (``general.timezone``) and
    the language format (``general.date_format``). The epoch feeds the native
    embed timestamp, which Discord renders in the reader's own timezone.
    """
    instant = _moment(moment)
    timezone_value = ""
    try:
        timezone_value = await get_setting(bot.storage, guild_id, "general.timezone")
    except (AttributeError, KeyError):
        timezone_value = ""
    tz = resolve_timezone(timezone_value)

    date_format = DEFAULT_DATE_FORMAT
    raw = getattr(bot.translator, "raw", None)
    if callable(raw):
        try:
            found = await raw(guild_id, "general.date_format")
        except (AttributeError, KeyError, TypeError, ValueError):
            found = None
        if isinstance(found, str) and found.strip() and found != "general.date_format":
            date_format = found
    try:
        readable = instant.astimezone(tz).strftime(date_format)
    except (TypeError, ValueError):
        readable = instant.astimezone(tz).strftime(DEFAULT_DATE_FORMAT)
    return {"timestamp": readable, "epoch": int(instant.timestamp())}


__all__ = [
    "DEFAULT_DATE_FORMAT",
    "IDENTITY_VARIABLES",
    "actor_identity",
    "event_time",
    "identity",
    "inject_identities",
]

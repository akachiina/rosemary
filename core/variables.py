"""Canonical placeholder variables for customizable cards.

Each entry is the single source of truth for one ``{name}`` placeholder:
a sample value for editor previews/tests and i18n keys for the label and
description shown in /personalizar. ``CardSpec.variables`` (in
:mod:`rosemary.core.cards`) declares which of these a card actually receives;
send sites must pass exactly that set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VariableSpec:
    """Describes one placeholder variable."""

    name: str
    sample: Any
    label_key: str
    description_key: str
    kind: str = "text"


def _v(name: str, sample: Any, kind: str = "text") -> VariableSpec:
    return VariableSpec(
        name=name,
        sample=sample,
        label_key=f"variables.{name}.label",
        description_key=f"variables.{name}.description",
        kind=kind,
    )


#: Canonical variables. Samples render in editor previews and card tests.
VARIABLES: dict[str, VariableSpec] = {
    spec.name: spec
    for spec in [
        _v("user", "<@0>", "mention"),
        _v("user_name", "@you"),
        _v("user_avatar", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("user_mention", "<@0>", "mention"),
        _v("server", "…"),
        _v("rep", "<@0>", "mention"),
        _v("count", 1, "number"),
        _v("matched", 1, "number"),
        _v("criterion", "com a palavra “spam”"),
        _v("inviter", "<@0>", "mention"),
        _v("inviter_mention", "<@0>", "mention"),
        _v("member", "<@0>", "mention"),
        _v("member_mention", "<@0>", "mention"),
        _v("mention", "<@0>", "mention"),
        _v("moderator", "<@0>", "mention"),
        _v("target", "<@0>", "mention"),
        _v("author", "<@0>", "mention"),
        _v("owner", "<@0>", "mention"),
        _v("actor", "<@0>", "mention"),
        _v("winner", "<@0>", "mention"),
        _v("winner_mention", "<@0>", "mention"),
        _v("invitee", "<@0>", "mention"),
        _v("reason", "…"),
        _v("duration", "…"),
        _v("channel", "#…"),
        _v("version", "v1.0.0"),
        _v("role", "…"),
        _v("role_name", "…"),
        _v("guild", "…"),
        _v("body", "…"),
        _v("title", "…"),
        _v("ping", "…"),
        _v("ping_role", "<@&0>", "mention"),
        _v("tag", "🏷️"),
        _v("link", "https://example.com/invite"),
        _v("url", "https://example.com", "url"),
        _v("cooldown", "…"),
        _v("days", 1, "number"),
        _v("total", 1, "number"),
        _v("bumps", 1, "number"),
        _v("bump_count", 1, "number"),
        _v("winner_count", 1, "number"),
        _v("total_wins", 1, "number"),
        _v("regular", 1, "number"),
        _v("bonus", 1, "number"),
        _v("fake", 1, "number"),
        _v("left", 1, "number"),
        _v("rank", 1, "number"),
        _v("joins", 1, "number"),
        _v("sent", 1, "number"),
        _v("failed", 1, "number"),
        _v("relayed", 1, "number"),
        _v("codes", 1, "number"),
        _v("amount", 1, "number"),
        _v("max_members", 10, "number"),
        _v("members", 1, "number"),
        _v("code", "abc123"),
        _v("flags", ""),
        _v("label", "…"),
        _v("expires", "…"),
        _v("name", "…"),
        _v("type", "…"),
        _v("word", "…"),
        _v("previous", "…"),
        _v("current", "…"),
        _v("old", "…"),
        _v("new", "…"),
        _v("deleted", "…"),
        _v("enabled", True),
        _v("locked", False),
        _v("next", "…"),
        _v("when", "…"),
        _v("status", "…"),
        _v("server_name", "…"),
        _v("leaderboard_list", "…"),
        _v("start_timestamp", 1111111111, "number"),
        _v("end_timestamp", 1111111111, "number"),
        _v("next_reset_timestamp", 1111111111, "number"),
        _v("next_bump_timestamp", 1111111111, "number"),
        _v("position", 1, "number"),
        _v("medal", "🥇"),
        _v("warned", "…"),
        _v("member_label", "…"),
        _v("moderator_label", "…"),
        _v("reason_label", "…"),
        _v("duration_label", "…"),
        _v("old_owner", "<@0>", "mention"),
        _v("new_owner", "<@0>", "mention"),
        _v("old_name", "…"),
        _v("new_name", "…"),
        _v("new_color", "…"),
        _v("emoji", "😀"),
        _v("orphans", 1, "number"),
        _v("expired", 1, "number"),
        _v("stars", 1, "number"),
        _v("ms", 42, "number"),
        _v("image_url", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("roles", 1, "number"),
        _v("channels", 1, "number"),
        _v("created_at", "<t:1111111111:D>"),
        _v("created_rel", "<t:1111111111:R>"),
        _v("server_icon", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("banner_url", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("description", "…"),
        _v("text_channels", 1, "number"),
        _v("voice_channels", 1, "number"),
        _v("boosts", 1, "number"),
        _v("emojis", 1, "number"),
        _v("stickers", 1, "number"),
        _v("verification", "Média"),
        _v("server_id", "1388731936914280540"),
        _v("timestamp", "<t:1111111111:R>"),
        _v("message_author", "<@0>", "mention"),
        _v("message_author_name", "@you"),
        _v("message", "…"),
        _v("new_message", "…"),
        _v("message_link", "https://example.com/message", "url"),
        _v("file_url", "https://example.com/purge.txt", "url"),
        _v("old_avatar", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("new_avatar", "https://cdn.discordapp.com/embed/avatars/1.png", "image"),
        _v("nickname", "…"),
        _v("joined_at", "<t:1111111111:D>"),
        _v("joined_rel", "<t:1111111111:R>"),
        _v("top_role", "…"),
        _v("boosting_since", "…"),
        _v("timeout", "…"),
        _v("is_bot", "Não"),
        _v("user_id", "123456789012345678"),
        _v("emoji_name", "…"),
        _v("emoji_id", "123456789012345678"),
        _v("animated", "Não"),
        _v("emoji_url", "https://cdn.discordapp.com/emojis/1.png", "image"),
        _v("source", "…"),
        _v("sticker_name", "…"),
        _v("sticker_id", "123456789012345678"),
        _v("sticker_url", "https://cdn.discordapp.com/stickers/1.png", "image"),
        _v("format", "…"),
        _v("tags", "…"),
        _v("uploader", "…"),
        _v("role_color", "…"),
        _v("role_icon", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("role_id", "123456789012345678"),
        _v("mentionable", "Não"),
        _v("hoist", "Não"),
        _v("channel_name", "…"),
        _v("channel_type", "…"),
        _v("topic", "…"),
        _v("category", "…"),
        _v("nsfw", "Não"),
        _v("slowmode", "…"),
        _v("bitrate", "…"),
        _v("user_limit", "…"),
        _v("channel_id", "123456789012345678"),
    ]
}

#: Legacy aliases (old name -> canonical twin). Empty: every live name is
#: canonical; the mechanism stays for future renames without breaking docs.
ALIASES: dict[str, str] = {}


def resolve_alias(name: str) -> str:
    """Map a legacy variable name to its canonical twin."""
    return ALIASES.get(name, name)


def samples_for(names) -> dict[str, Any]:
    """Sample mapping for the given variable names (unknowns skipped)."""
    return {
        name: VARIABLES[resolve_alias(name)].sample
        for name in names
        if resolve_alias(name) in VARIABLES
    }


def lint_placeholders(doc: dict, allowed: set[str]) -> list[tuple[str, str | None]]:
    """Unknown ``{placeholders}`` in a document with close-match suggestions.

    Never blocks saving: unknown names render literally, so this is a hint
    for typos (e.g. ``{usre}`` → ``{user}``), not a validation error.
    """
    import difflib
    import re

    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("body", "url", "label") and isinstance(value, str):
                    found.extend(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", value))
                elif key == "urls" and isinstance(value, list):
                    for url in value:
                        if isinstance(url, str):
                            found.extend(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", url))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc.get("blocks", []))
    issues = []
    for name in dict.fromkeys(found):
        if name not in allowed:
            matches = difflib.get_close_matches(name, sorted(allowed), n=1, cutoff=0.6)
            issues.append((name, matches[0] if matches else None))
    return issues

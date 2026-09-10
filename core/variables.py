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
        _v("user_name", "@voce"),
        _v("user_avatar", "https://cdn.discordapp.com/embed/avatars/0.png", "image"),
        _v("user_mention", "<@0>", "mention"),
        _v("server", "…"),
        _v("count", 1, "number"),
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
        _v("link", "https://exemplo.com/convite"),
        _v("url", "https://exemplo.com", "url"),
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

    Never blocks saving — unknown names render literally, so this is a hint
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

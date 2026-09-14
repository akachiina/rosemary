"""Embed card entries: the classic rich message as a theme alternative.

A theme ``cards:`` entry may be written as an **embed** instead of raw
Components V2 — same placeholders (``{user}``), same pings (``{@user}``), and
optionally classic ActionRow buttons (link or action — ``open_ticket`` /
``dismiss`` work identically). This module converts and validates the embed
shape once, at theme load/import, into an internal document::

    {"v": 1, "kind": "embed", "embed": {...}, "buttons": [...]}

V2 documents produced by :mod:`rosemary.core.v2_convert` are tagged
``"kind": "v2"``. Validation mirrors Discord's documented embed limits
(title 256, description 4096, 25 fields, footer 2048, author 256, 6000 total)
and raises :class:`ThemeError` with stable ``embed_*`` codes so import
rejections stay precise and translatable.
"""

from __future__ import annotations

import re
from typing import Any

from rosemary.core.cards import CardIssue

#: Discord hard limits (https://discord.com/developers/docs/resources/message).
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_COUNT_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_TEXT_MAX = 2048
AUTHOR_NAME_MAX = 256
TOTAL_MAX = 6000

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

#: String fields allowed on the embed itself (canonical Discord spelling).
_EMBED_STRING_FIELDS = {
    "title": TITLE_MAX,
    "description": DESCRIPTION_MAX,
}

_NESTED_STRING_FIELDS = {
    "footer.text": FOOTER_TEXT_MAX,
    "author.name": AUTHOR_NAME_MAX,
}


class ThemeError(ValueError):
    """Re-exported shape: raised with translatable issue codes."""

    def __init__(self, issues: list[CardIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.code for issue in issues) or "invalid theme")


def is_embed_entry(entry: Any) -> bool:
    """Whether a ``cards:`` entry is the embed form (``embed:`` key present)."""
    return isinstance(entry, dict) and isinstance(entry.get("embed"), dict)


def convert_embed_entry(key: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Convert + validate one embed card entry into the internal document.

    Raises :class:`ThemeError` carrying every problem found (never fails
    silently — a rejected import explains itself).
    """
    issues: list[CardIssue] = []
    raw = entry["embed"]
    embed: dict[str, Any] = {}

    for field, limit in _EMBED_STRING_FIELDS.items():
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            issues.append(CardIssue("embed_field_string", (("field", field),)))
            continue
        _check_length(issues, field, value, limit)
        embed[field] = value

    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            issues.append(CardIssue("embed_bad_url", (("field", "url"),)))
        else:
            embed["url"] = url

    color = _convert_color(issues, raw.get("color"))
    if color is not None:
        embed["color"] = color

    for path, limit in _NESTED_STRING_FIELDS.items():
        section_name, field = path.split(".")
        section = raw.get(section_name)
        if section is None:
            continue
        if isinstance(section, str):
            # Short form: `footer: "text"` / `author: "name"`.
            _check_length(issues, path, section, limit)
            embed[section_name] = {"text" if section_name == "footer" else "name": section}
            continue
        if not isinstance(section, dict):
            issues.append(CardIssue("embed_section_shape", (("field", section_name),)))
            continue
        converted: dict[str, Any] = {}
        text = section.get(field)
        if isinstance(text, str) and text.strip():
            _check_length(issues, path, text, limit)
            converted[field] = text
        icon = section.get("icon_url")
        if icon is not None:
            if isinstance(icon, str) and icon.startswith(("http://", "https://")):
                converted["icon_url"] = icon
            else:
                issues.append(CardIssue("embed_bad_url", (("field", f"{section_name}.icon_url"),)))
        if converted:
            embed[section_name] = converted

    timestamp = raw.get("timestamp")
    if timestamp is not None and isinstance(timestamp, (int, float)) and not isinstance(
        timestamp, bool
    ):
        embed["timestamp"] = int(timestamp)

    image = _convert_media(issues, "image", raw.get("image"))
    if image is not None:
        embed["image"] = image
    thumbnail = _convert_media(issues, "thumbnail", raw.get("thumbnail"))
    if thumbnail is not None:
        embed["thumbnail"] = thumbnail

    raw_fields = raw.get("fields")
    if raw_fields is not None:
        if not isinstance(raw_fields, list) or not raw_fields:
            issues.append(CardIssue("embed_fields_shape"))
        else:
            if len(raw_fields) > FIELD_COUNT_MAX:
                issues.append(
                    CardIssue(
                        "embed_field_count",
                        (("count", len(raw_fields)), ("max", FIELD_COUNT_MAX)),
                    )
                )
            fields: list[dict[str, Any]] = []
            for index, item in enumerate(raw_fields):
                field = _convert_field(issues, index, item)
                if field is not None:
                    fields.append(field)
            if fields:
                embed["fields"] = fields

    buttons: list[dict[str, Any]] = []
    raw_buttons = entry.get("buttons")
    if raw_buttons is not None:
        from rosemary.core.v2_convert import convert_button

        if not isinstance(raw_buttons, list) or not raw_buttons:
            issues.append(CardIssue("embed_buttons_shape"))
        else:
            for index, button in enumerate(raw_buttons):
                if not isinstance(button, dict):
                    issues.append(CardIssue("embed_buttons_shape"))
                    continue
                converted = convert_button(button, issues, f"buttons[{index}]")
                if converted is not None:
                    buttons.append(converted)

    if issues:
        raise ThemeError(issues)
    if buttons and len(buttons) > 5:
        raise ThemeError([CardIssue("embed_buttons_count", (("max", 5),))])

    total = _total_chars(embed)
    if total > TOTAL_MAX:
        raise ThemeError([CardIssue("embed_total", (("chars", total), ("max", TOTAL_MAX)))])
    if not embed:
        raise ThemeError([CardIssue("embed_empty")])
    _stable_button_ids(key, buttons)

    return {"v": 1, "kind": "embed", "embed": embed, "buttons": buttons}


def _stable_button_ids(card_key: str, buttons: list[dict[str, Any]]) -> None:
    """Content-derived ids for embed action buttons (same contract as V2's
    ``_stable_button_ids``): posted buttons and boot-registered dispatch views
    come from separate theme loads, so randomness would kill every click."""
    import hashlib

    for index, button in enumerate(buttons):
        raw = (
            f"{card_key}|e|{index}|{button.get('label', '')}"
            f"|{button.get('url', '')}|{button.get('action', '')}"
        )
        button["id"] = "b_" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def _check_length(issues: list[CardIssue], field: str, text: str, limit: int) -> None:
    if len(text) > limit:
        issues.append(
            CardIssue("embed_too_long", (("field", field), ("chars", len(text)), ("max", limit)))
        )


def _convert_color(issues: list[CardIssue], value: Any) -> str | None:
    """Color int / ``#rrggbb`` / theme token — stored as token-or-hex string."""
    if value is None:
        return None
    if isinstance(value, bool):
        issues.append(CardIssue("embed_bad_color", (("color", value),)))
        return None
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return f"#{value:06x}"
    if isinstance(value, str) and value.strip():
        return value.strip()  # hex or theme token; render resolves, load validates palette
    issues.append(CardIssue("embed_bad_color", (("color", value),)))
    return None


def _convert_media(issues: list[CardIssue], field: str, value: Any) -> dict[str, str] | None:
    """``image``/``thumbnail``: short string or Discord ``{url: ...}`` shape."""
    if value is None:
        return None
    if isinstance(value, str):
        url = value
    elif isinstance(value, dict) and isinstance(value.get("url"), str):
        url = value["url"]
    else:
        issues.append(CardIssue("embed_bad_url", (("field", field),)))
        return None
    if not url.startswith(("http://", "https://")) and "{" not in url:
        issues.append(CardIssue("embed_bad_url", (("field", field),)))
        return None
    return {"url": url}


def _convert_field(issues: list[CardIssue], index: int, item: Any) -> dict[str, Any] | None:
    """One embed field: ``{name, value, inline?}``."""
    where = f"fields[{index}]"
    if not isinstance(item, dict):
        issues.append(CardIssue("embed_field_shape", (("where", where),)))
        return None
    name, value = item.get("name"), item.get("value")
    if not isinstance(name, str) or not name.strip():
        issues.append(CardIssue("embed_field_shape", (("where", where),)))
        return None
    if not isinstance(value, str):
        issues.append(CardIssue("embed_field_shape", (("where", where),)))
        return None
    _check_length(issues, f"{where}.name", name, FIELD_NAME_MAX)
    _check_length(issues, f"{where}.value", value, FIELD_VALUE_MAX)
    inline = item.get("inline")
    field: dict[str, Any] = {"name": name, "value": value}
    if isinstance(inline, bool):
        field["inline"] = inline
    return field


def _total_chars(embed: dict[str, Any]) -> int:
    """Discord's global embed character budget (rendered placeholder length
    is checked again at send time by the same walk)."""
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    footer = embed.get("footer") or {}
    total += len(footer.get("text") or "")
    author = embed.get("author") or {}
    total += len(author.get("name") or "")
    for field in embed.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    return total

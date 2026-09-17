"""Convert raw Discord Components V2 JSON into Rosemary's internal schema.

Theme files speak *exactly* the JSON shape Discord documents (type numbers and
names both accepted), so admins can copy payloads straight from discord.dev.
This module converts them once: at theme load/import: into the internal block
schema (:mod:`rosemary.core.cards`); everything downstream (validation,
``build_items``, placeholders, mentions) stays untouched.

Accepted aliases per Discord component type:

=================  ============  ==========================================
Discord            Internal      Notes
=================  ============  ==========================================
1 ActionRow        row           ``components`` -> ``buttons``
2 Button           button        inline into the parent row; ``style`` int -> name
3 StringSelect     (unsupported) no runtime behavior for theme authors
9 Section          section       ``components`` -> ``children``
10 TextDisplay     text          ``content`` -> ``body``
11 Thumbnail       thumbnail     ``media.url`` -> ``url``
12 MediaGallery    gallery       ``items[].media.url`` -> ``urls``
14 Separator       divider       ``spacing`` 1/2 -> small/large
17 Container       container     ``accent_color`` int -> ``color`` hex token
=================  ============  ==========================================

Unknown/unsupported structures raise :class:`ThemeError` with a stable code so
import rejections are precise and translatable.
"""

from __future__ import annotations

import re
from typing import Any

from rosemary.core.cards import CardIssue

#: Discord component type numbers (the canonical spelling in theme files).
TYPE_ACTION_ROW = 1
TYPE_BUTTON = 2
TYPE_STRING_SELECT = 3
TYPE_SECTION = 9
TYPE_TEXT_DISPLAY = 10
TYPE_THUMBNAIL = 11
TYPE_MEDIA_GALLERY = 12
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

_NAME_TO_TYPE: dict[str, int] = {
    "action_row": TYPE_ACTION_ROW,
    "actionrow": TYPE_ACTION_ROW,
    "row": TYPE_ACTION_ROW,
    "button": TYPE_BUTTON,
    "string_select": TYPE_STRING_SELECT,
    "section": TYPE_SECTION,
    "text_display": TYPE_TEXT_DISPLAY,
    "textdisplay": TYPE_TEXT_DISPLAY,
    "text": TYPE_TEXT_DISPLAY,
    "thumbnail": TYPE_THUMBNAIL,
    "media_gallery": TYPE_MEDIA_GALLERY,
    "gallery": TYPE_MEDIA_GALLERY,
    "separator": TYPE_SEPARATOR,
    "divider": TYPE_SEPARATOR,
    "container": TYPE_CONTAINER,
}

_BUTTON_STYLE_TO_NAME = {
    1: "primary",
    2: "secondary",
    3: "success",
    4: "danger",
    5: "link",
}

_SEPARATOR_SPACING = {1: "small", 2: "large"}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class ThemeError(ValueError):
    """A theme file is invalid; ``issues`` carry stable, translatable codes."""

    def __init__(self, issues: list[CardIssue]) -> None:
        self.issues = issues
        parts = []
        for issue in issues:
            parts.append(issue.code)
        super().__init__("; ".join(parts) or "invalid theme")


def _stable_button_ids(card_key: str, blocks: list[dict[str, Any]]) -> None:
    """Give action buttons ids derived from content, not randomness.

    The posted message's buttons and the boot-registered persistent dispatch
    views come from *separate* theme loads: a random id would never match,
    silently killing every themed action button. Hashing
    ``(card_key, path, label, url)`` keeps ids stable across loads and
    restarts while still changing when the author edits the button.
    """
    import hashlib

    def visit(blocks: list[dict[str, Any]], path: str) -> None:
        for index, block in enumerate(blocks):
            here = f"{path}/{index}"
            for btn_index, button in enumerate(block.get("buttons", []) or []):
                if not isinstance(button, dict):
                    continue
                raw = (
                    f"{card_key}|{here}|{btn_index}|{button.get('label', '')}"
                    f"|{button.get('url', '')}|{button.get('action', '')}"
                )
                button["id"] = "b_" + hashlib.sha1(raw.encode()).hexdigest()[:8]
            visit(block.get("children", []) or [], here)

    visit(blocks, "")


def _component_type(node: dict[str, Any]) -> int | None:
    """Discord type number from ``type`` (int) or its string name."""
    raw = node.get("type")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        return _NAME_TO_TYPE.get(raw.strip().lower())
    return None


def _to_hex(value: Any) -> str | None:
    """Discord color int (or hex string) to ``#rrggbb``; ``None`` when invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return f"#{value:06x}"
    if isinstance(value, str):
        token = value.strip()
        if _HEX_RE.match(token):
            return token if token.startswith("#") else f"#{token}"
    return None


def _require(
    issues: list[CardIssue],
    code: str,
    condition: bool,
    **params: Any,
) -> None:
    if not condition:
        issues.append(CardIssue(code, tuple(params.items())))


def convert_button(
    node: dict[str, Any], issues: list[CardIssue], where: str
) -> dict[str, Any] | None:
    """One raw button -> internal button dict (label/url/action/style)."""
    button: dict[str, Any] = {}
    label = node.get("label")
    if isinstance(label, str):
        button["label"] = label
    url = node.get("url")
    if isinstance(url, str):
        button["url"] = url
    style = node.get("style")
    if isinstance(style, int) and not isinstance(style, bool):
        name = _BUTTON_STYLE_TO_NAME.get(style)
        _require(
            issues,
            "theme_button_style",
            name is not None,
            where=where,
            style=style,
        )
        if name in ("primary", "secondary", "success", "danger"):
            button["style"] = name
    custom_id = node.get("custom_id")
    if isinstance(custom_id, str) and custom_id.startswith("cardact:"):
        # Convention for bot-driven buttons (open_ticket:<type>, dismiss).
        parts = custom_id.split(":", 2)
        if len(parts) == 3 and parts[1] in ("open_ticket", "dismiss"):
            button["action"] = parts[1]
            if parts[1] == "open_ticket":
                button["ticket_type"] = parts[2]
            else:
                button.pop("url", None)
    return button


def _convert_row(
    node: dict[str, Any], issues: list[CardIssue], where: str
) -> dict[str, Any]:
    children = node.get("components")
    buttons: list[dict[str, Any]] = []
    for child in children or []:
        if not isinstance(child, dict) or _component_type(child) != TYPE_BUTTON:
            issues.append(CardIssue("theme_row_button", (("where", where),)))
            continue
        converted = convert_button(child, issues, where)
        if converted is not None:
            buttons.append(converted)
    return {"type": "row", "buttons": buttons}


def _convert_section_child(
    node: dict[str, Any], issues: list[CardIssue], where: str
) -> dict[str, Any] | None:
    kind = _component_type(node)
    if kind == TYPE_TEXT_DISPLAY:
        return {"type": "text", "body": str(node.get("content", ""))}
    issues.append(CardIssue("theme_section_text", (("where", where),)))
    return None


def _convert_accessory(
    node: Any, issues: list[CardIssue], where: str
) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        issues.append(CardIssue("theme_section_accessory", (("where", where),)))
        return None
    kind = _component_type(node)
    if kind == TYPE_THUMBNAIL:
        media = node.get("media")
        url = media.get("url") if isinstance(media, dict) else None
        return {"type": "thumbnail", "url": url if isinstance(url, str) else ""}
    if kind == TYPE_BUTTON:
        converted = convert_button(node, issues, where)
        if converted is None:
            return None
        url = converted.get("url")
        if not isinstance(url, str) or not url:
            issues.append(CardIssue("theme_section_accessory", (("where", where),)))
            return None
        return {"type": "button", "label": converted.get("label", ""), "url": url}
    issues.append(CardIssue("theme_section_accessory", (("where", where),)))
    return None


def _convert_component(
    node: dict[str, Any], issues: list[CardIssue], where: str, *, in_container: bool
) -> dict[str, Any] | None:
    """One raw Discord component -> one internal block (recursing children)."""
    kind = _component_type(node)
    if kind is None:
        issues.append(CardIssue("theme_unknown_type", (("where", where),)))
        return None

    if kind == TYPE_TEXT_DISPLAY:
        content = node.get("content")
        if not isinstance(content, str):
            issues.append(CardIssue("theme_text_content", (("where", where),)))
            return None
        return {"type": "text", "body": content}

    if kind == TYPE_SEPARATOR:
        block: dict[str, Any] = {
            "type": "divider",
            "spacing": _SEPARATOR_SPACING.get(node.get("spacing"), "small"),
        }
        if node.get("divider") is False:
            block["visible"] = False
        return block

    if kind == TYPE_MEDIA_GALLERY:
        items = node.get("items")
        urls: list[str] = []
        for item in items or []:
            media = item.get("media") if isinstance(item, dict) else None
            url = media.get("url") if isinstance(media, dict) else None
            if isinstance(url, str) and url:
                urls.append(url)
            else:
                issues.append(CardIssue("theme_gallery_url", (("where", where),)))
        return {"type": "gallery", "urls": urls}

    if kind == TYPE_ACTION_ROW:
        return _convert_row(node, issues, where)

    if kind == TYPE_SECTION:
        children = [
            converted
            for child in node.get("components") or []
            if isinstance(child, dict)
            for converted in [_convert_section_child(child, issues, where)]
            if converted is not None
        ]
        accessory = _convert_accessory(node.get("accessory"), issues, where)
        if accessory is None:
            return None
        return {"type": "section", "children": children, "accessory": accessory}

    if kind == TYPE_CONTAINER:
        _require(
            issues,
            "theme_container_top",
            not in_container,
            where=where,
        )
        color = None
        raw_color = node.get("accent_color")
        if raw_color is not None:
            color = _to_hex(raw_color)
            _require(
                issues,
                "theme_bad_color",
                color is not None,
                where=where,
                color=raw_color,
            )
        children: list[dict[str, Any]] = []
        for index, child in enumerate(node.get("components") or []):
            if isinstance(child, dict):
                converted = _convert_component(
                    child, issues, f"{where}.components[{index}]", in_container=True
                )
                if converted is not None:
                    children.append(converted)
        out: dict[str, Any] = {"type": "container", "children": children}
        if color is not None:
            out["color"] = color
        return out

    if kind == TYPE_STRING_SELECT:
        issues.append(CardIssue("theme_select_unsupported", (("where", where),)))
        return None

    issues.append(CardIssue("theme_unknown_type", (("where", where),)))
    return None


def convert_raw_document(payload: Any) -> list[dict[str, Any]]:
    """Convert a raw Discord Components V2 payload to internal blocks.

    ``payload`` may be the component list itself or ``{"components": [...]}``.
    Raises :class:`ThemeError` when the payload is not convertible; the caller
    then rejects the theme (at load or import) instead of sending broken cards.
    """
    issues: list[CardIssue] = []
    if isinstance(payload, dict) and isinstance(payload.get("components"), list):
        components = payload["components"]
    elif isinstance(payload, list):
        components = payload
    else:
        raise ThemeError([CardIssue("theme_document_shape")])
    blocks: list[dict[str, Any]] = []
    for index, node in enumerate(components):
        if not isinstance(node, dict):
            issues.append(CardIssue("theme_document_shape"))
            continue
        converted = _convert_component(node, issues, f"components[{index}]", in_container=False)
        if converted is not None:
            blocks.append(converted)
    if issues:
        raise ThemeError(issues)
    if not blocks:
        raise ThemeError([CardIssue("theme_document_shape")])
    return blocks


def convert_card_entry(key: str, entry: Any) -> dict[str, Any]:
    """Convert one theme ``cards:`` entry to a validated internal document.

    A plain string becomes a single text block (the plain-card shortcut);
    anything else goes through :func:`convert_raw_document`. Validation runs
    against the theme palette *after* conversion, so both shapes obey Discord's
    layout rules and raise :class:`ThemeError` with a per-card message.
    """
    from rosemary.core.cards import ensure_ids, validate_document

    blocks = (
        [{"type": "text", "body": entry}]
        if isinstance(entry, str)
        else convert_raw_document(entry)
    )
    _stable_button_ids(key, blocks)
    doc = ensure_ids({"v": 1, "blocks": blocks})
    errors = validate_document(doc, draft=True)
    if errors:
        raise ThemeError(
            [CardIssue("theme_card_invalid", (("key", key),))]
            + errors
        )
    return doc

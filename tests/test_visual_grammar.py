"""The visual grammar of P7: what every golden may and may not show.

The card and panel goldens freeze what a member receives. This module is the
lint over those fixtures: it does not render anything new, it asserts the
grammar the refactor established, so a future card cannot quietly put a raw
mention in the author line, repeat the image as the thumbnail, ship an empty
embed frame, ping with a bare token or disguise a field as a blockquote.

The last checks read the panels: the category picker marks the current
category and every action button of the migrated panels carries an icon.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from catalog import flatten, load_catalog
from golden_cards import GOLDEN_DIR as CARD_DIR
from golden_panels import GOLDEN_DIR as PANEL_DIR
from golden_panels import make_bot, render_all

#: A resolved user/role mention (the frame may name the member, never the id).
MENTION = re.compile(r"<@[!&]?\d+>")
#: A native timestamp token (the frame shows the readable date, not the token).
TIMESTAMP = re.compile(r"<t:\d+(?::[a-zA-Z])?>")
#: An inline bold segment (the title of a plain-text card).
BOLD = re.compile(r"\*\*[^*]+\*\*")
#: A field disguised as a blockquote (``> **Label:** value``).
FAKE_FIELD = re.compile(r"(?m)^>\s*\*\*[^*]+:\*\*")
#: A pictographic character (the command picker stays plain).
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def _cards() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(CARD_DIR.glob("*.json"))
    ]


def _frame_text(frame: dict[str, Any] | None) -> str:
    if not frame:
        return ""
    return str(frame.get("name") or frame.get("text") or "")


def test_the_frame_never_carries_a_token():
    """The author and the footer show names and dates, never the raw token."""
    broken: list[str] = []
    for key, card in _cards():
        embed = card.get("embed")
        if not embed:
            continue
        for field in ("author", "footer"):
            text = _frame_text(embed.get(field))
            if MENTION.search(text) or TIMESTAMP.search(text):
                broken.append(f"{key}: {field} carries {text!r}")
    assert broken == []


def test_the_image_is_never_the_thumbnail():
    """The thumbnail identifies the subject; the image shows the content."""
    broken = [
        key
        for key, card in _cards()
        if (embed := card.get("embed"))
        and embed.get("image")
        and embed.get("image") == embed.get("thumbnail")
    ]
    assert broken == []


def test_every_embed_has_title_or_description():
    """An embed with neither is an empty frame, not a card."""
    broken = [
        key
        for key, card in _cards()
        if (embed := card.get("embed"))
        and not (embed.get("title") or embed.get("description"))
    ]
    assert broken == []


def test_an_embed_with_a_ping_carries_a_sentence():
    """An embed never notifies: the ping travels as a sentence in the content."""
    broken: list[str] = []
    for key, card in _cards():
        if not card.get("pings") or not card.get("embed"):
            continue
        content = card.get("content") or ""
        sentence = MENTION.sub("", content).strip()
        if not sentence:
            broken.append(f"{key}: content is {content!r}, not a sentence")
    assert broken == []


def test_a_multiline_text_card_opens_with_a_bold_title():
    """One thing per line starts with the title, in bold."""
    broken: list[str] = []
    for key, card in _cards():
        if card.get("embed") is not None:
            continue
        content = card.get("content") or ""
        if "\n" not in content:
            continue
        first = content.splitlines()[0]
        if not BOLD.search(first):
            broken.append(f"{key}: {first!r}")
    assert broken == []


def test_a_text_card_never_disguises_a_field_as_a_blockquote():
    broken = [
        key
        for key, card in _cards()
        if card.get("embed") is None
        and FAKE_FIELD.search(card.get("content") or "")
    ]
    assert broken == []


async def test_the_category_picker_marks_the_current_category():
    """Every category carries an icon and a description; the current is default."""
    snapshots = await render_all(make_bot())
    select = next(
        select
        for select in snapshots["settings"]["selects"]
        if select["custom_id"] == "settings_category"
    )
    assert all(option["emoji"] for option in select["options"])
    assert all(option["description"] for option in select["options"])
    assert [option["value"] for option in select["options"] if option["default"]] == [
        "general"
    ]


async def test_every_action_button_has_an_icon():
    """The five migrated panels: a button without an icon is a bare label."""
    snapshots = await render_all(make_bot())
    missing = [
        f"{name}: {button['custom_id']}"
        for name, snapshot in snapshots.items()
        for button in snapshot["buttons"]
        if not button["emoji"]
    ]
    assert missing == []


def test_no_golden_leaks_an_asset_reference():
    """``{emojis/...}`` is an internal reference: it never reaches a member."""
    broken = [
        path.name
        for path in sorted(Path(CARD_DIR).glob("*.json"))
        + sorted(Path(PANEL_DIR).glob("*.json"))
        if "{emojis/" in path.read_text(encoding="utf-8")
    ]
    assert broken == []


def test_slash_command_descriptions_stay_plain():
    """The command picker carries no emoji: the hierarchy lives in the help.

    Descriptions may keep machine values in inline code, but a pictograph or a
    theme token would put the icon language in the picker, where the copy has no
    space for it. ``{{rep}}`` is the only escaped placeholder allowed.
    """
    broken: list[str] = []
    for code in ("pt-BR", "en-US"):
        for key, value in flatten(load_catalog(code)).items():
            if not isinstance(value, str) or not key.endswith("description"):
                continue
            if ".command." not in key:
                continue
            plain = value.replace("{{", "").replace("}}", "")
            if EMOJI.search(value) or "{" in plain or "}" in plain:
                broken.append(f"{code}: {key} = {value!r}")
    assert broken == []

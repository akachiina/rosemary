"""The icon system: one token vocabulary, one resolver.

Every setting category and value type names an icon token in the built-in
theme. A token is Unicode or an asset reference (``{emojis/shield}``): the
same token reaches a card, a button, a select and the chrome through
:meth:`rosemary.ui.theme.Theme.icon`. A broken reference degrades to no icon
and never leaks the raw ``{emojis/...}`` into a message.
"""

from __future__ import annotations

from rosemary.core.assets import REGISTRY
from rosemary.core.settings_spec import SettingCategory, SettingType
from rosemary.ui.theme import Theme, load_theme


def test_every_category_and_type_has_an_icon():
    theme = load_theme()
    missing = [
        f"category_{category.value}"
        for category in SettingCategory
        if not theme.icon(f"category_{category.value}")
    ]
    missing += [
        f"type_{value_type.value}"
        for value_type in SettingType
        if not theme.icon(f"type_{value_type.value}")
    ]
    assert missing == []


def test_icon_resolves_an_asset_reference(monkeypatch):
    monkeypatch.setitem(REGISTRY._mentions, "emojis/shield", "<:shield:123>")
    theme = Theme(
        colors={},
        emojis={
            "shield": "{emojis/shield}",
            "broken": "{emojis/never_uploaded}",
        },
    )

    assert theme.icon("shield") == "<:shield:123>"
    # An asset the application never uploaded degrades to no icon.
    assert theme.icon("broken") == ""
    assert "{emojis/" not in theme.icon("broken")
    # A token the theme never defined is empty too.
    assert theme.icon("missing") == ""

    # The same resolver backs the markdown templates (cards and chrome).
    theme.markdown["title"] = "{shield} {title}"
    assert theme.md("title", title="Ready") == "<:shield:123> Ready"

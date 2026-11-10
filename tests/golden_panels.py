"""Golden snapshots of the interactive panels: the copy and the controls.

The card goldens freeze what a member receives from the pipeline. These
fixtures freeze the other half of the UI: the panel screens assembled in
Python, with their themed chrome, the text they show and the buttons/selects
they offer. ``scripts/snapshot_panels.py`` writes them under
``tests/golden/panels/`` and ``tests/test_golden_panels.py`` compares the
current render against them, so a kit/runtime refactor cannot change what a
member sees or clicks unnoticed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import discord

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.colors import ColorEntry, ColorStore
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme

#: Where the fixtures live.
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "panels"
#: The guild the snapshots render for.
GUILD_ID = 1
#: The language the fixtures capture (the bot's primary audience).
LANGUAGE = "pt-BR"
#: The catalog directory (relative to the repo root, never to the CWD).
LANG_DIR = Path(__file__).resolve().parents[1] / "language"


class PanelBot:
    """The minimum bot the panels need: real theme, catalog and storage."""

    def __init__(self, root: Path) -> None:
        self.storage = GuildStorage(root)
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=lambda guild_id: LANGUAGE,
            default_placeholders=self.theme.emojis,
        )
        self.guilds: list[Any] = []
        self.views: list[Any] = []

    def get_guild(self, guild_id: int):
        return None

    def get_cog(self, name: str):
        return None

    def add_view(self, view: Any) -> None:
        self.views.append(view)


#: The representative panels the fixtures capture (one per kit consumer).
PANEL_NAMES = ("settings", "boost", "roleplay", "colors", "cleaner")


def make_bot() -> PanelBot:
    """A bot bound to a throwaway data directory."""
    return PanelBot(Path(tempfile.mkdtemp()))


def _walk(node: Any):
    """Depth-first walk; an ActionRow exposes ``children``/``items`` alike."""
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk(child)
        return
    yield node
    if isinstance(node, discord.ui.ActionRow):
        nested = list(getattr(node, "children", []) or [])
    else:
        nested = list(getattr(node, "items", []) or [])
    for child in nested:
        yield from _walk(child)
    accessory = getattr(node, "accessory", None)
    if accessory is not None:
        yield from _walk(accessory)


def _emoji(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def snapshot(view: Any) -> dict[str, Any]:
    """The visible copy and the controls of one prepared panel."""
    texts: list[str] = []
    buttons: list[dict[str, Any]] = []
    selects: list[dict[str, Any]] = []
    for node in _walk(list(view.children)):
        if isinstance(node, discord.ui.TextDisplay):
            texts.append(node.content)
        elif isinstance(node, discord.ui.Button):
            buttons.append(
                {
                    "custom_id": node.custom_id,
                    "label": node.label,
                    "style": node.style.name,
                    "disabled": bool(node.disabled),
                    "emoji": _emoji(node.emoji),
                }
            )
        elif isinstance(node, discord.ui.Select):
            selects.append(
                {
                    "custom_id": node.custom_id,
                    "placeholder": node.placeholder,
                    "disabled": bool(node.disabled),
                    "options": [
                        {
                            "label": option.label,
                            "value": option.value,
                            "default": bool(option.default),
                        }
                        for option in node.options
                    ],
                }
            )
    return {"texts": texts, "buttons": buttons, "selects": selects}


async def _build_panels(bot: PanelBot) -> dict[str, Any]:
    from rosemary.cogs.cleaner import CleanerPurgeView, PurgeRequest
    from rosemary.ui.boost_menu import BoostMenuView
    from rosemary.ui.colors_menu import ColorsManagerView
    from rosemary.ui.roleplay_menu import RoleplayManagerView
    from rosemary.ui.settings_menu import SettingsMenuView

    await ColorStore(bot.storage.data_dir).set_colors(
        GUILD_ID, [ColorEntry("c1", "Rosa", "#FFB7C5")]
    )
    panels = {
        "settings": SettingsMenuView(bot, GUILD_ID, owner_id=9),
        "boost": BoostMenuView(bot, GUILD_ID, owner_id=9),
        "roleplay": RoleplayManagerView(bot, GUILD_ID, 9),
        "colors": ColorsManagerView(bot, GUILD_ID, 9),
        "cleaner": CleanerPurgeView(
            bot,
            GUILD_ID,
            owner_id=9,
            request=PurgeRequest(mode="word", word="spam", criterion="spam"),
        ),
    }
    snapshots: dict[str, Any] = {}
    for name, view in panels.items():
        assert name in PANEL_NAMES, name
        await view.prepare()
        snapshots[name] = snapshot(view)
    assert set(snapshots) == set(PANEL_NAMES)
    return snapshots


async def render_all(bot: PanelBot | None = None) -> dict[str, Any]:
    """Render every representative panel (the kit's consumers)."""
    return await _build_panels(bot or make_bot())


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """The paths where two snapshots differ (at most a dozen)."""
    found: list[str] = []

    def walk(want: Any, got: Any, where: str) -> None:
        if len(found) >= 12:
            return
        if type(want) is not type(got):
            found.append(f"{where}: {want!r} != {got!r}")
            return
        if isinstance(want, dict):
            for key in sorted(set(want) | set(got)):
                if key not in want or key not in got:
                    found.append(f"{where}.{key}: missing on one side")
                else:
                    walk(want[key], got[key], f"{where}.{key}")
            return
        if isinstance(want, list):
            if len(want) != len(got):
                found.append(f"{where}: {len(want)} items != {len(got)} items")
                return
            for index, (one, other) in enumerate(zip(want, got, strict=True)):
                walk(one, other, f"{where}[{index}]")
            return
        if want != got:
            found.append(f"{where}: {want!r} != {got!r}")

    walk(expected, actual, path)
    return found

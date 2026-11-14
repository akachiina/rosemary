"""Golden snapshots of every card, in its default form.

The card pipeline is about to be refactored (the UI System v2 plan). These
fixtures freeze what a member receives today, so the refactor can prove it did
not change the visible output: ``scripts/snapshot_cards.py`` writes them under
``tests/golden/cards/`` and ``tests/test_golden_cards.py`` compares the current
render against them.

The snapshot is the rendered payload (plain text, classic embed or Components
V2 view) after normalization. The only volatile value is the send timestamp,
replaced by a constant, so two runs produce the same file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

# The cog modules register their commands and their send-site helpers when
# imported, so the snapshot must import them to see the same cards the bot
# renders (the documents live in the default package; the helpers compute the
# state the cards show).
import rosemary.cogs.about  # noqa: F401  (registers the help card)
import rosemary.cogs.audit  # noqa: F401  (registers the audit cards)
import rosemary.cogs.birthdays  # noqa: F401  (registers the birthday card)
import rosemary.cogs.confessions  # noqa: F401  (registers the confession cards)
import rosemary.cogs.info  # noqa: F401  (registers the info cards)
import rosemary.cogs.roleplay  # noqa: F401  (registers the roleplay card)
import rosemary.cogs.starboard  # noqa: F401  (registers the starboard card)
import rosemary.cogs.suggestions  # noqa: F401  (registers the suggestion cards)
import rosemary.cogs.utility  # noqa: F401  (registers the serverinfo card)
import rosemary.cogs.welcome  # noqa: F401  (registers the event cards)
from rosemary.cogs.starboard import _ramp_hex
from rosemary.cogs.suggestions import _score_hex, _side_color
from rosemary.core.card_service import (
    render_card_messages,
)
from rosemary.core.cards import all_cards, default_form, get_card
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.core.variables import samples_for
from rosemary.ui.theme import load_theme

#: Where the fixtures live.
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "cards"
#: The guild the snapshots render for (no guild state is involved).
GUILD_ID = 1
#: The language the fixtures capture (the bot's primary audience).
LANGUAGE = "pt-BR"
#: The frozen clock: nothing in a fixture may depend on "now".
FIXED_TIMESTAMP = "<t:1767225600:R>"
#: The catalog directory (relative to the repo root, never to the CWD).
LANG_DIR = Path(__file__).resolve().parents[1] / "language"

#: Lines some send sites compose before rendering the card. They are not part
#: of the declared contract (the card body only references them), but a
#: realistic send always fills them, so the snapshot fills them too. Each entry
#: is ``(catalog key, prefix, variable)``: the prefix mirrors the send site
#: (a space before the reason, a newline before the others).
EXTRA_LINES = {
    "reason_line": ("card.audit.reason_line", " ", "reason"),
    "moderator_line": ("card.audit.moderator_line", "\n", "moderator"),
    "file_line": ("card.audit.bulk_delete.file_line", "\n", "file_url"),
    "duration_line": ("card.audit.timeout.until_line", "\n", "duration"),
    "attachments_line": ("card.audit.attachments_line", "\n", "attachment_lines"),
}

#: A family whose copy differs composes its own line: the card key names it
#: and its entries replace the shared ones (the suggestion DMs write the
#: motive in their own words, not in the audit ones).
EXTRA_LINES_BY_KEY = {
    "suggestions.dm.approved": {"reason_line": ("suggest.dm.reason", "", "reason")},
    "suggestions.dm.rejected": {"reason_line": ("suggest.dm.reason", "", "reason")},
    "confessions.dm.rejected": {"reason_line": ("confess.dm.reason", "", "reason")},
}

#: The color a card takes when the *state* chooses it. The state is not part
#: of the contract (the theme owns what the token means), but the live send
#: site computes it, so the snapshot computes it too: the fixture must show
#: the color a member really receives, not the document's fallback.
STATE_BY_KEY = {
    "starboard.card": lambda theme, variables: {
        "state_color": _ramp_hex(theme, int(variables.get("stars") or 1))
    },
    "suggestions.card": lambda theme, variables: {
        "state_color": _score_hex(theme, int(variables.get("score") or 0))
    },
    "suggestions.dm.vote": lambda theme, variables: {
        "state_color": _side_color(variables.get("side"))
    },
    "bump.thank_you": lambda theme, variables: {
        "state_color": theme.style("bump_reminder").color
    },
    "bump.leaderboard": lambda theme, variables: {
        "state_color": theme.style("bump_leaderboard").color
    },
    # The key share shows the default quorum the settings deliver (3 of 5),
    # not the generic single-item sample of the contract.
    "confessions.dm.key_share": lambda theme, variables: {
        "threshold": 3,
        "total": 5,
    },
}


class GoldenBot:
    """The minimum bot the card pipeline needs.

    The real theme, the real catalog and an empty storage: the snapshot must
    show what a server with the default theme receives.
    """

    def __init__(self, root: Path) -> None:
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=lambda guild_id: LANGUAGE,
            default_placeholders=self.theme.emojis,
        )
        self.storage = GuildStorage(root)
        self._theme_store = ThemeStore(root, themes_dir=root / "themes")
        self._theme_cache: dict[int, Any] = {}
        self.guilds: list[Any] = []


def make_bot() -> GoldenBot:
    """A bot bound to a throwaway data directory."""
    return GoldenBot(Path(tempfile.mkdtemp()))


def sample_variables(spec) -> dict[str, Any]:
    """The sample values one card receives (its declared contract)."""
    values = samples_for(spec.variables)
    if "timestamp" in values:
        values["timestamp"] = FIXED_TIMESTAMP
    return values


async def extra_variables(
    bot: GoldenBot, variables: dict[str, Any], key: str = ""
) -> dict[str, Any]:
    """The composed lines some send sites add (resolved from the catalog)."""
    from rosemary.core.cards import safe_format

    values: dict[str, Any] = {}
    lines = {**EXTRA_LINES, **EXTRA_LINES_BY_KEY.get(key, {})}
    for name, (catalog_key, prefix, variable) in lines.items():
        template = await bot.translator.raw(GUILD_ID, catalog_key)
        if not isinstance(template, str):
            continue
        if not str(variables.get(variable) or "").strip():
            continue  # the send site leaves the line out when it has no value
        if template == catalog_key:
            # The catalog carries no template for this line: the send site
            # ships the raw value (the attachments, without a label).
            values[name] = prefix + str(variables[variable])
            continue
        values[name] = prefix + safe_format(
            template, {**bot.theme.emojis, **variables}
        )
    return values


def _item_snapshot(item: Any) -> dict[str, Any]:
    """One Components V2 item, as a stable JSON-friendly shape."""
    node: dict[str, Any] = {"type": type(item).__name__}
    for attr in ("content", "label", "url", "custom_id"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value:
            node[attr] = value
    style = getattr(item, "style", None)
    style_name = getattr(style, "name", None)
    if isinstance(style_name, str):
        node["style"] = style_name
    accent = getattr(item, "colour", None)
    if accent is not None:
        node["accent"] = str(accent)
    children = list(getattr(item, "children", []) or []) + [
        child
        for child in (getattr(item, "items", []) or [])
        # An ActionRow exposes the same list as ``children`` and ``items``:
        # counting both would double every button in the fixture.
        if not any(child is seen for seen in getattr(item, "children", []) or [])
    ]
    if children:
        node["children"] = [_item_snapshot(child) for child in children]
    accessory = getattr(item, "accessory", None)
    if accessory is not None:
        node["accessory"] = _item_snapshot(accessory)
    return node


def _view_snapshot(payload: Any) -> list[dict[str, Any]]:
    """The Components V2 tree of a rendered payload."""
    view = getattr(payload, "view", None)
    if view is None:
        return []
    return [_item_snapshot(item) for item in getattr(view, "children", []) or []]


def _embed_data(embed: Any) -> dict[str, Any]:
    """One embed of a rendered payload, with a frozen timestamp."""
    data = embed.to_dict()
    if data.get("timestamp"):
        data["timestamp"] = "<iso>"
    return data


def _embed_snapshot(payload: Any) -> dict[str, Any] | None:
    """The classic embed of a rendered payload, with a frozen timestamp."""
    embed = getattr(payload, "embed", None)
    if embed is None:
        return None
    return _embed_data(embed)


def _embeds_snapshot(payload: Any) -> list[dict[str, Any]] | None:
    """The stack of embeds of a rendered payload.

    A message carries one embed (the shape every card had) or a stack of
    them (the ``embeds`` slot of the document). The stack is only recorded
    when there is more than one box: the single embed stays in the
    ``embed`` field, so the fixture of a plain card does not change.
    """
    embeds = list(getattr(payload, "embeds", ()) or ())
    if len(embeds) <= 1:
        return None
    return [_embed_data(embed) for embed in embeds]


async def render_card(bot: GoldenBot, key: str) -> dict[str, Any]:
    """Render one card and normalize the payload for the fixture.

    The pipeline is the IR one: the document is read into the semantic
    model, compiled and drawn by the serializer of its form. A card that
    ships as a sequence keeps one entry per message, in order; a
    single-message card keeps the flat shape it always had.
    """
    spec = get_card(key)
    variables = sample_variables(spec)
    variables.update(await extra_variables(bot, variables, key))
    state = STATE_BY_KEY.get(key)
    if state is not None:
        variables.update(state(bot.theme, variables))
    messages = await render_card_messages(bot, GUILD_ID, key, variables)
    if len(messages) <= 1:
        message = messages[0] if messages else None
        return {
            "key": key,
            "form": default_form(key),
            **_message_snapshot(message),
        }
    return {
        "key": key,
        "form": "sequence",
        "messages": [_message_snapshot(message) for message in messages],
    }


def _message_snapshot(message: Any) -> dict[str, Any]:
    """One message of a card, normalized for the fixture."""
    payload = getattr(message, "payload", None)
    allowed = getattr(message, "allowed", None)
    pings: list[int] = []
    role_pings: list[int] = []
    if allowed is not None:
        pings = sorted(allowed.to_dict().get("users") or [])
        role_pings = sorted(allowed.to_dict().get("roles") or [])
    stack = _embeds_snapshot(payload)
    files = [
        str(getattr(item, "filename", ""))
        for item in getattr(payload, "files", []) or []
        if getattr(item, "filename", "")
    ]
    return {
        "content": getattr(payload, "content", None),
        "embed": _embed_snapshot(payload),
        **({"embeds": stack} if stack else {}),
        **({"files": files} if files else {}),
        "view": _view_snapshot(payload),
        "pings": pings,
        **({"role_pings": role_pings} if role_pings else {}),
    }


def differences(expected: object, actual: object, path: str = "") -> list[str]:
    """The paths where two snapshots differ (at most a dozen)."""
    found: list[str] = []

    def walk(want: object, got: object, where: str) -> None:
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


async def render_all(bot: GoldenBot | None = None) -> dict[str, dict[str, Any]]:
    """Render every registered card (the whole registry, in key order)."""
    bot = bot or make_bot()
    snapshots: dict[str, dict[str, Any]] = {}
    for spec in all_cards():
        snapshots[spec.key] = await render_card(bot, spec.key)
    return snapshots

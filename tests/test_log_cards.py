"""Default log cards: layout, footer, legacy body and full-card overrides."""

from __future__ import annotations

import rosemary.core.card_specs  # noqa: F401  (registers every card builder)
from rosemary.core.card_service import get_effective_document
from rosemary.core.debug_cards import default_debug_panel
from rosemary.core.log_cards import default_log_document
from rosemary.core.log_specs import LOG_SPECS
from rosemary.ui.theme import load_theme


class _Translator:
    def __init__(self, words=None) -> None:
        self.words = words or {}

    async def t(self, guild_id, key, **kwargs):
        found = self.words.get(key)
        if found is not None:
            return found
        return f"{key}:{kwargs}" if kwargs else key

    async def raw(self, guild_id, key):
        return self.words.get(key, key)


class _ThemeStore:
    def __init__(self, theme) -> None:
        self.theme = theme

    async def effective_theme(self, bot, guild_id):
        return self.theme


class _Bot:
    def __init__(self, theme=None) -> None:
        self.theme = theme or load_theme()
        self._theme_cache = {1: self.theme}
        self._theme_store = _ThemeStore(self.theme)
        self.translator = _Translator()


def _children(doc: dict) -> list[dict]:
    return doc["blocks"][0]["children"]


async def test_fields_log_uses_a_timestamp_only_footer():
    spec = LOG_SPECS["moderation.logs.ban"]
    doc = await default_log_document(
        _Bot(),
        1,
        spec,
        member="<@2>",
        member_id="2",
        moderator="<@3>",
        reason="spam",
    )
    children = _children(doc)
    assert children[0]["body"] == "# moderation.logs.ban.title"
    fields = children[1]["body"]
    assert "**log.field.target.label:** <@2>" in fields
    assert "**log.field.moderator.label:** <@3>" in fields
    assert children[-1]["body"] == "-# {timestamp}"


async def test_moderation_log_uses_the_avatar_section():
    doc = await default_log_document(
        _Bot(),
        1,
        LOG_SPECS["moderation.logs.warn"],
        member="<@2>",
        member_id="2",
        user_avatar="https://cdn.example/2.png",
        moderator="<@3>",
        reason="spam",
    )
    children = _children(doc)
    section = children[0]
    assert section["type"] == "section"
    assert section["accessory"] == {
        "type": "thumbnail",
        "url": "https://cdn.example/2.png",
    }
    assert section["children"][0]["body"] == "# moderation.logs.warn.title"
    assert "**log.field.target.label:** <@2>" in section["children"][1]["body"]
    assert children[-1]["body"] == "-# {timestamp}"


async def test_invites_leave_log_uses_the_avatar_section():
    bot = _Bot()
    bot.translator = _Translator(
        {
            "invites.logs.leave.title": "{door} Membro Saiu",
            "invites.logs.leave.description": "**Membro:** {user}\n**Convidador:** {inviter}",
        }
    )
    doc = await default_log_document(
        bot,
        1,
        LOG_SPECS["invites.logs.leave"],
        user="<@2>",
        user_avatar="https://cdn.example/left.png",
        inviter="<@3>",
    )
    children = _children(doc)
    section = children[0]
    assert section["type"] == "section"
    assert section["accessory"] == {
        "type": "thumbnail",
        "url": "https://cdn.example/left.png",
    }
    assert "Membro Saiu" in section["children"][0]["body"]
    assert "{user}" in section["children"][1]["body"]
    assert "{inviter}" in section["children"][1]["body"]
    assert children[-1]["body"] == "-# {timestamp}"


async def test_field_labels_resolve_catalog_keys():
    bot = _Bot()
    bot.translator = _Translator(
        {
            "log.field.target.label": "{people} Alvo",
            "log.field.member_id.label": "{numbers} ID do membro",
            "log.field.moderator.label": "{gear} Moderador(a)",
            "log.field.reason.label": "{scroll} Motivo",
        }
    )
    doc = await default_log_document(
        bot,
        1,
        LOG_SPECS["moderation.logs.ban"],
        member="<@2>",
        member_id="2",
        moderator="<@3>",
        reason="spam",
    )
    fields = _children(doc)[1]["body"]
    assert "**{people} Alvo:** <@2>" in fields
    assert "**{gear} Moderador(a):** <@3>" in fields
    assert "log.field." not in fields


async def test_log_title_uses_the_theme_title_preset():
    theme = load_theme()
    theme.markdown["title"] = "## {title}"
    doc = await default_log_document(
        _Bot(theme), 1, LOG_SPECS["debug.logs.test"]
    )
    assert _children(doc)[0]["body"] == "## debug.logs.test.title"


async def test_actor_footer_when_the_body_does_not_name_the_actor():
    spec = LOG_SPECS["bump.logs.no_role"]
    doc = await default_log_document(
        _Bot(),
        1,
        spec,
        actor="<@9>",
        body_key="bump.error_role_not_found",
    )
    assert _children(doc)[-1]["body"] == "-# by {actor} · {timestamp}"


async def test_legacy_body_override_keeps_the_new_title_and_footer():
    theme = load_theme()
    theme.cards = {
        "moderation.logs.ban.description": {
            "v": 1,
            "blocks": [
                {
                    "type": "container",
                    "color": "info",
                    "children": [
                        {"type": "text", "body": "legacy body for {member}"}
                    ],
                }
            ],
        }
    }
    doc = await default_log_document(
        _Bot(theme),
        1,
        LOG_SPECS["moderation.logs.ban"],
        member="<@2>",
        member_id="2",
        moderator="<@3>",
        reason="spam",
    )
    children = _children(doc)
    assert children[0]["body"] == "# moderation.logs.ban.title"
    assert "legacy body for <@2>" in children[1]["body"]
    assert children[-1]["body"] == "-# {timestamp}"


async def test_full_card_override_wins_over_the_log_builder():
    override = {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": "danger",
                "children": [{"type": "text", "body": "full override"}],
            }
        ],
    }
    theme = load_theme()
    theme.cards = {"moderation.logs.ban": override}
    doc = await get_effective_document(
        _Bot(theme), 1, "moderation.logs.ban", {}
    )
    assert doc == override


async def test_debug_panel_is_a_rich_card():
    doc = await default_debug_panel(
        _Bot(),
        1,
        guilds=2,
        members=15,
        latency="500",
        uptime="1h",
        version="1.0",
        theme="default",
        logging="On",
        audit="Off",
        timestamp="now",
    )
    children = _children(doc)
    assert children[0]["body"] == "# Debug panel"
    assert "debug.guilds" in children[1]["body"]
    assert "debug.panel.logging" in children[1]["body"]
    assert children[-1]["body"] == "-# now"

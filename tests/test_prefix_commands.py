"""Prefix command layer: the ``PrefixContext`` facade, aliases and feedback.

The prefix surface is thin on purpose: aliases reuse the slash handlers
through the facade, so the tests here lock three things: the facade maps the
slash context surface onto a prefix context correctly, every alias is
registered (case-insensitively, with the themed ``!help`` replacing py-cord's
default), and failures answer the invoker instead of only logging.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.bot import RosemaryBot
from rosemary.cogs.about import AboutCog
from rosemary.cogs.cleaner import CleanerCog
from rosemary.cogs.info import InfoCog
from rosemary.cogs.moderation import ModerationCog
from rosemary.cogs.roleplay import RoleplayCog
from rosemary.cogs.utility import UtilityCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.responses import PrefixContext
from rosemary.ui.theme import load_theme


class FmtTranslator:
    """Key-echoing translator that keeps formatted variables assertable."""

    async def t(self, guild_id, key, **variables):
        if not variables:
            return key
        return key + "?" + "&".join(f"{k}={v}" for k, v in sorted(variables.items()))

    async def raw(self, guild_id, key):
        return []


class PtTranslator:
    """Real pt-BR catalog text with theme emojis and variables resolved."""

    def __init__(self):
        self.theme = load_theme()
        with open("language/pt-BR.yaml", encoding="utf-8") as fh:
            self.catalog = yaml.safe_load(fh) or {}

    def _text(self, key: str):
        node = self.catalog
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node if isinstance(node, str) else key

    async def t(self, guild_id, key, **variables):
        text = self._text(key)
        mapping = {**self.theme.emojis, **variables}
        for name, value in mapping.items():
            text = text.replace(f"{{{name}}}", str(value))
        return text

    async def raw(self, guild_id, key):
        return self._text(key)


class FakeRole:
    def __init__(self, position: int) -> None:
        self.position = position

    def __lt__(self, other) -> bool:
        return self.position < other.position

    def __le__(self, other) -> bool:
        return self.position <= other.position

    def __gt__(self, other) -> bool:
        return self.position > other.position

    def __ge__(self, other) -> bool:
        return self.position >= other.position


class AllPerms:
    def __getattr__(self, name):
        return True


class FakeMember:
    def __init__(self, member_id: int, *, bot: bool = False, position: int = 1) -> None:
        self.id = member_id
        self.bot = bot
        self.name = f"member{member_id}"
        self.display_name = self.name
        self.mention = f"<@{member_id}>"
        self.display_avatar = None
        self.guild_permissions = AllPerms()
        self.top_role = FakeRole(position)


class FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.name = "Girassol"
        self.owner_id = 999
        self.me = FakeMember(99, position=10)
        self._members: dict[int, FakeMember] = {}

    def add_member(self, member: FakeMember) -> None:
        self._members[member.id] = member

    def get_member(self, member_id):
        return self._members.get(member_id)

    def get_channel_or_thread(self, channel_id):
        return None


class FakePrefixCtx:
    """The slice of ``commands.Context`` the handlers reach through the facade."""

    def __init__(self, guild, author) -> None:
        self.guild = guild
        self.author = author
        self.reply = AsyncMock()
        self.send = AsyncMock()


def _view_text(view) -> str:
    parts: list[str] = []

    def walk(item) -> None:
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
        # Containers keep their children in ``items``; ActionRows in
        # ``children``. Walk both so any V2 shape yields its text.
        for attr in ("children", "items"):
            for child in getattr(item, attr, None) or []:
                walk(child)

    for child in getattr(view, "children", None) or []:
        walk(child)
    return "\n".join(parts)


def _replied_view(ctx) -> object:
    assert ctx.reply.call_count >= 1
    view = ctx.reply.call_args.kwargs.get("view")
    assert view is not None
    return view


def _base_bot(tmp_path, translator=None) -> object:
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", help_command=None)
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.translator = translator or FmtTranslator()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    return bot


#: facade ---------------------------------------------------------------


async def test_prefix_context_maps_the_slash_surface():
    inner = SimpleNamespace(
        guild=SimpleNamespace(id=7),
        author=SimpleNamespace(id=1),
        reply=AsyncMock(),
        send=AsyncMock(),
    )
    facade = PrefixContext(inner)

    assert facade.guild_id == 7
    assert facade.author is inner.author  # delegation

    await facade.respond(view="v", ephemeral=True)
    inner.reply.assert_called_once_with(view="v", mention_author=False)

    await facade.followup.send(content="c", ephemeral=True)
    inner.send.assert_called_once_with(content="c")

    assert await facade.defer() is None  # no interaction window to ACK


#: registration ---------------------------------------------------------


def test_real_bot_registers_aliases_case_insensitively(tmp_path):
    bot = RosemaryBot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.add_cog(AboutCog(bot))
    bot.add_cog(ModerationCog(bot))
    bot.add_cog(UtilityCog(bot))
    bot.add_cog(InfoCog(bot))
    bot.add_cog(CleanerCog(bot))

    expected = [
        "about", "help", "limpar", "ping",
        "serverinfo", "userinfo", "roleinfo", "channelinfo", "emoji",
        "ban", "kick", "warn", "mute", "warnings", "unwarn",
    ]
    for name in expected:
        assert bot.get_command(name) is not None, f"!{name} is not registered"

    # case_insensitive: !BAN matches like !ban
    assert bot.get_command("BAN") is bot.get_command("ban")
    assert bot.get_command("WARNINGS") is bot.get_command("warnings")

    # help_command=None removed py-cord's default: !help is the themed one
    help_cmd = bot.get_command("help")
    assert help_cmd is not None
    assert isinstance(help_cmd.cog, AboutCog)

    # the purge alias keeps its manage_messages guard
    assert bot.get_command("limpar").checks


#: moderation -----------------------------------------------------------


def _moderation_world(tmp_path):
    bot = _base_bot(tmp_path)
    cog = ModerationCog(bot)
    bot.add_cog(cog)
    guild = FakeGuild()
    author = FakeMember(1, position=10)
    ctx = FakePrefixCtx(guild, author)
    return bot, cog, guild, author, ctx


async def test_mute_prefix_uses_the_setting_default_duration(tmp_path):
    bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    await bot.storage.set(1, "moderation.mute_seconds", 3600)
    target = FakeMember(55, position=1)

    await cog.mute_prefix(ctx, [target], None)

    text = _view_text(_replied_view(ctx))
    assert "duration=1h" in text  # moderation.mute_seconds, not an error


async def test_mute_prefix_parses_explicit_duration(tmp_path):
    _bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    target = FakeMember(55, position=1)

    await cog.mute_prefix(ctx, [target], "10m")

    text = _view_text(_replied_view(ctx))
    assert "duration=10m" in text


async def test_mute_prefix_rejects_an_invalid_duration(tmp_path):
    _bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    target = FakeMember(55, position=1)

    await cog.mute_prefix(ctx, [target], "banana")

    text = _view_text(_replied_view(ctx))
    assert "mute.invalid_duration" in text
    assert "duration=" not in text  # no confirmation dialog was built


async def test_warnings_prefix_shows_the_card(tmp_path):
    _bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    target = FakeMember(55, position=1)

    await cog.warnings_prefix(ctx, target)

    assert "warnings.none" in _view_text(_replied_view(ctx))


async def test_unwarn_prefix_removes_one_warning(tmp_path):
    _bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    target = FakeMember(55, position=1)
    await cog.warnings.add_warning(1, target.id, "spam", 1, "Girassol")

    await cog.unwarn_prefix(ctx, target, 1)

    text = _view_text(_replied_view(ctx))
    assert "unwarn.removed" in text
    assert await cog.warnings.get_warnings(1, target.id) == []


async def test_unwarn_prefix_without_index_opens_the_clear_all_dialog(tmp_path):
    _bot, cog, _guild, _author, ctx = _moderation_world(tmp_path)
    target = FakeMember(55, position=1)
    await cog.warnings.add_warning(1, target.id, "spam", 1, "Girassol")

    await cog.unwarn_prefix(ctx, target, None)

    # ClearWarningsView goes out the same reply; the registration stays until
    # the staff clicks confirm.
    _replied_view(ctx)
    assert await cog.warnings.get_warnings(1, target.id)


#: cleaner --------------------------------------------------------------


async def test_limpar_prefix_opens_the_confirm_panel(tmp_path):
    bot = _base_bot(tmp_path)
    cog = CleanerCog(bot)
    bot.add_cog(cog)
    guild = FakeGuild()
    author = FakeMember(1, position=10)
    ctx = FakePrefixCtx(guild, author)

    await cog.limpar_prefix(ctx, 5)

    view = _replied_view(ctx)
    assert "cleaner" in type(view).__name__.lower()


#: roleplay -------------------------------------------------------------


async def test_roleplay_prefix_answers_bot_targets(tmp_path):
    bot = _base_bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
    guild = FakeGuild()
    guild.add_member(FakeMember(77, bot=True))
    message = SimpleNamespace(
        guild=guild,
        author=FakeMember(5, position=5),
        content="!HUG <@77>",  # uppercase verb: the trigger is case-insensitive
        reply=AsyncMock(),
    )

    await cog.on_message(message)

    message.reply.assert_called_once()
    assert message.reply.call_args.args[0] == "roleplay.error_bot"


#: !help ----------------------------------------------------------------


async def test_help_prefix_lists_localized_commands(tmp_path):
    """Text commands only: latin prefix names, localized descriptions."""
    bot = _base_bot(tmp_path, translator=PtTranslator())
    await bot.storage.set(1, "general.prefix", "?")
    cog = AboutCog(bot)
    bot.add_cog(cog)
    guild = FakeGuild()
    ctx = FakePrefixCtx(guild, FakeMember(1, position=10))

    await cog.help_prefix(ctx)

    assert ctx.reply.call_args.kwargs.get("mention_author") is False
    text = _view_text(_replied_view(ctx))
    # groups with pt-BR headings, entries in the ``**!name:** description``
    # shape, the guild's real prefix applied
    assert "## Moderação" in text
    assert "**`?ban`:** Banir um membro" in text  # bold + inline code (house standard)
    assert "## Informações" in text
    assert "**`?serverinfo`:**" in text
    assert "**`?help`:** Mostra esta lista de comandos" in text
    assert "prefixo `?`" in text  # intro: the guild prefix as inline code
    # the slash listing is gone: no localized slash names anywhere
    assert "/ban" not in text
    assert "?sobre" not in text
    # roleplay is off by default: the interactions group is omitted
    assert "Interações" not in text


async def test_help_prefix_lists_interactions_when_roleplay_enabled(tmp_path):
    bot = _base_bot(tmp_path, translator=PtTranslator())
    await bot.storage.set(1, "roleplay.enabled", True)
    cog = AboutCog(bot)
    bot.add_cog(cog)
    ctx = FakePrefixCtx(FakeGuild(), FakeMember(1, position=10))

    await cog.help_prefix(ctx)

    text = _view_text(_replied_view(ctx))
    assert "## Interações" in text
    assert "`!hug · !kiss" in text  # one code span over every latin action verb
    assert "!cuddle`" in text  # the same span closes right after the last verb


def test_help_table_covers_every_registered_prefix_command(tmp_path):
    """A prefix alias created without a !help entry fails here."""
    from rosemary.cogs.about import _HELP_SECTIONS

    bot = RosemaryBot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    for cog_class in (AboutCog, ModerationCog, UtilityCog, InfoCog, CleanerCog):
        bot.add_cog(cog_class(bot))

    listed = {
        name for _group, entries in _HELP_SECTIONS for name, _desc_key in entries
    }
    registered = {cmd.name for cmd in bot.commands}
    assert registered <= listed, f"missing from !help: {sorted(registered - listed)}"


async def test_help_builder_accepts_variables_and_skips_empty_lists():
    """The builder takes **variables (live sends forward extras) and omits
    the block of a group the send site turned off, keeping placeholders
    when the value is absent (editor previews)."""
    from rosemary.cogs.about import default_help_document

    doc = await default_help_document(SimpleNamespace(), 1, interactions_list="", x=1)
    children = doc["blocks"][0]["children"]
    bodies = [c["body"] for c in children if c.get("type") == "text"]
    assert "{interactions_list}" not in bodies  # roleplay off: block omitted
    assert "{moderation_list}" in bodies

    doc = await default_help_document(SimpleNamespace(), 1)
    children = doc["blocks"][0]["children"]
    bodies = [c["body"] for c in children if c.get("type") == "text"]
    assert "{interactions_list}" in bodies  # preview: placeholder kept


#: error feedback -------------------------------------------------------


def _error_bot(tmp_path):
    bot = RosemaryBot.__new__(RosemaryBot)
    bot.storage = GuildStorage(tmp_path)
    bot.translator = FmtTranslator()
    bot.theme = load_theme()
    return bot


async def test_command_not_found_stays_silent(tmp_path):
    from discord.ext import commands

    bot = _error_bot(tmp_path)
    ctx = SimpleNamespace(command=None, guild=SimpleNamespace(id=1), reply=AsyncMock())

    await bot.on_command_error(ctx, commands.CommandNotFound("x"))

    assert not ctx.reply.call_args_list


async def test_permission_failure_answers_the_invoker(tmp_path):
    from discord.ext import commands

    bot = _error_bot(tmp_path)
    ctx = SimpleNamespace(command=None, guild=SimpleNamespace(id=1), reply=AsyncMock())

    await bot.on_command_error(ctx, commands.MissingPermissions(["manage_messages"]))

    text = _view_text(ctx.reply.call_args.kwargs["view"])
    assert "general.no_permission" in text


async def test_real_failure_answers_and_logs(tmp_path, caplog):
    from discord.ext import commands

    bot = _error_bot(tmp_path)
    ctx = SimpleNamespace(command=None, guild=SimpleNamespace(id=1), reply=AsyncMock())

    await bot.on_command_error(
        ctx, commands.CommandInvokeError(RuntimeError("boom"))
    )

    text = _view_text(ctx.reply.call_args.kwargs["view"])
    assert "general.unknown_error" in text
    assert "boom" in caplog.text


async def test_error_feedback_without_setup_is_a_noop():
    from discord.ext import commands

    bot = RosemaryBot.__new__(RosemaryBot)
    bot.storage = None
    bot.translator = None
    bot.theme = None
    ctx = SimpleNamespace(command=None, guild=SimpleNamespace(id=1), reply=AsyncMock())

    await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("early")))

    assert not ctx.reply.call_args_list

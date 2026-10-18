"""Runtime permission guard tests (core/command_guards.py).

Discord ignores ``default_member_permissions`` declared on subcommands of a
``SlashCommandGroup``, so each restricted handler calls the guard in code.
These fakes mirror the live ApplicationContext shape used at runtime.
"""

from rosemary.core.command_guards import require_admin, require_permissions


class _GuildPermissions:
    def __init__(self, administrator: bool = False, manage_messages: bool = False) -> None:
        self.administrator = administrator
        self.manage_messages = manage_messages


class _Author:
    def __init__(self, administrator: bool, manage_messages: bool = False) -> None:
        self.id = 4242
        self.guild_permissions = _GuildPermissions(administrator, manage_messages)


class _Response:
    def __init__(self, done: bool = False) -> None:
        self.done = done

    def is_done(self) -> bool:
        return self.done


class _Followup:
    def __init__(self) -> None:
        self.sent: dict | None = None

    async def send(self, *args, **kwargs) -> None:
        self.sent = {"args": args, "kwargs": kwargs}


class _Translator:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def t(self, guild_id: int, key: str, **_kwargs) -> str:
        self.calls.append((guild_id, key))
        return "DENIED_TEXT"


class _Bot:
    def __init__(self) -> None:
        self.translator = _Translator()


class _Command:
    qualified_name = "bump reset_week"


class _HTTPError(Exception):
    pass


class _Ctx:
    def __init__(self, *, administrator: bool, manage_messages: bool, done: bool = False) -> None:
        self.author = _Author(administrator, manage_messages)
        self.guild_id = 1546973619559604244
        self.bot = _Bot()
        self.command = _Command()
        self.response = _Response(done)
        self.followup = _Followup()
        self.responded: dict | None = None

    async def respond(self, *args, **kwargs) -> None:
        self.responded = {"args": args, "kwargs": kwargs}


async def test_administrator_passes() -> None:
    ctx = _Ctx(administrator=True, manage_messages=True)
    assert await require_admin(ctx) is True
    assert ctx.responded is None
    assert ctx.bot.translator.calls == []


async def test_administrator_denied_responds_ephemeral() -> None:
    ctx = _Ctx(administrator=False, manage_messages=False)
    assert await require_admin(ctx) is False
    assert ctx.responded is not None
    assert ctx.responded["kwargs"] == {"ephemeral": True}
    assert ctx.responded["args"] == ("DENIED_TEXT",)
    assert ctx.bot.translator.calls == [(ctx.guild_id, "general.no_permission")]


async def test_manage_messages_denied_for_admin_missing() -> None:
    ctx = _Ctx(administrator=False, manage_messages=False)
    assert await require_permissions(ctx, "manage_messages") is False
    assert ctx.responded is not None


async def test_manage_messages_allows_staff_member() -> None:
    ctx = _Ctx(administrator=False, manage_messages=True)
    assert await require_permissions(ctx, "manage_messages") is True
    assert ctx.responded is None


async def test_denial_uses_followup_when_already_acked() -> None:
    ctx = _Ctx(administrator=False, manage_messages=False, done=True)
    assert await require_admin(ctx) is False
    assert ctx.responded is None
    assert ctx.followup.sent is not None
    assert ctx.followup.sent["kwargs"] == {"ephemeral": True}


async def test_missing_permissions_attribute_denies(caplog) -> None:
    """Live shape check: a bare User in DMs has no guild_permissions."""
    ctx = _Ctx(administrator=False, manage_messages=False)
    del ctx.author.guild_permissions
    assert await require_admin(ctx) is False
    assert ctx.responded is not None


async def test_denial_is_logged(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="rosemary.core.command_guards"):
        ctx = _Ctx(administrator=False, manage_messages=False)
        await require_admin(ctx)
    assert any("Denied admin command" in record.message for record in caplog.records)

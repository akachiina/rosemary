"""Partnership edit flow: pre-filled modal, text edit and representative swap.

Locks ``/parcerias_admin editar``: guards answer ephemeral before any modal,
the modal prefills the raw template (the resolved text for legacy records),
the submit reposts the ad without touching the renewal clock, and a
representative swap re-keys the record, moves the partner role, DMs the new
representative and names the takeover in the log.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import yaml

import rosemary.cogs.partnerships as partnerships_module
from rosemary.cogs.partnerships import PartnershipsCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "language" / "pt-BR.yaml"


def _catalog_text(key: str) -> str:
    with CATALOG.open(encoding="utf-8") as fh:
        node = yaml.safe_load(fh) or {}
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else key


class FakeTranslator:
    """Formats like the real Translator: theme emojis as default vars."""

    def __init__(self):
        self.theme = load_theme()

    async def t(self, guild_id, key, **variables):
        template = _catalog_text(key)
        mapping = {**self.theme.emojis, **variables}
        try:
            return template.format(**mapping)
        except (KeyError, IndexError, ValueError):
            return template

    async def raw(self, guild_id, key):
        return _catalog_text(key)


def _bot(tmp_path):
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    bot.user.id = 42
    return bot


def _cog(tmp_path):
    return PartnershipsCog(_bot(tmp_path))


def _guild():
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Girassol"
    guild.get_member = MagicMock(return_value=None)
    return guild


def _member(member_id: int = 100):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.mention = f"<@{member_id}>"
    return member


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = _member(9)
    ctx.send_modal = AsyncMock()
    ctx.respond = AsyncMock()
    return ctx


def _ad_channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 55
    channel.mention = "<#55>"
    return channel


def _input_text(modal) -> discord.ui.InputText:
    for child in modal.children:
        item = getattr(child, "item", None)
        if isinstance(item, discord.ui.InputText):
            return item
    raise AssertionError("modal has no InputText field")


async def _prepare(tmp_path):
    cog = _cog(tmp_path)
    guild = _guild()
    await cog.bot.storage.set(1, "partnerships.enabled", True)
    await cog.bot.storage.set(1, "partnerships.channel", 55)
    await cog.bot.storage.set(1, "partnerships.ping_role", 777)
    cog._channel = AsyncMock(return_value=_ad_channel())
    ctx = _ctx(guild)
    return cog, guild, ctx


_PINNED_RENEWED = "2026-01-01T00:00:00+00:00"


async def _seed(cog, guild, *, rep_id=100, raw="", content="", message_id=111):
    """A partnership record with a pinned renewal clock (legacy has no raw)."""
    await cog.store.add(guild.id, rep_id, rep_id, 9, message_id, content, [], raw_text=raw)
    doc = await cog.store._doc(guild.id)
    entry = doc["partnerships"][str(rep_id)]
    entry["last_renewed_at"] = _PINNED_RENEWED
    await cog.store._save(guild.id, doc)


async def _open(cog, ctx, *, member, new_rep=None):
    await PartnershipsCog.partnerships_edit.callback(
        cog, ctx, member=member, new_rep=new_rep
    )


async def _submit(modal, raw: str):
    field = _input_text(modal)
    field.value = raw
    interaction = MagicMock()
    interaction.user = _member(9)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await modal.callback(interaction)
    return interaction


def _recorder(bucket: list, tag: str):
    """Async side effect recording ``(tag, user_id)`` per role call."""
    async def record(guild, user_id):
        bucket.append((tag, user_id))
    return record


#: modal prefill ----------------------------------------------------------------


async def test_edit_opens_modal_prefilled_with_raw_template(tmp_path):
    """The raw template keeps placeholders intact for the next edit."""
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, raw="Fala {rep}! {ping_role}", content="x")
    await _open(cog, ctx, member=_member())
    modal = ctx.send_modal.await_args.args[0]
    field = _input_text(modal)
    assert field.value == "Fala {rep}! {ping_role}"
    assert field.style == discord.InputTextStyle.paragraph
    assert field.max_length == 2000
    assert modal.title == _catalog_text("partnerships.edit_modal.title")
    assert len(modal.title) <= 45


async def test_edit_prefills_resolved_content_for_legacy_records(tmp_path):
    """Records from before raw_text exist: prefill the resolved text."""
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, content="Anúncio antigo <@100>")
    await _open(cog, ctx, member=_member())
    field = _input_text(ctx.send_modal.await_args.args[0])
    assert field.value == "Anúncio antigo <@100>"


#: guards ----------------------------------------------------------------------


async def test_edit_not_found_answers_instead_of_modal(tmp_path):
    cog, guild, ctx = await _prepare(tmp_path)
    await _open(cog, ctx, member=_member())
    ctx.send_modal.assert_not_called()
    assert ctx.respond.await_args.kwargs.get("ephemeral") is True
    assert "Sem parceria" in ctx.respond.await_args.args[0]


async def test_edit_new_rep_with_own_partnership_answers_error(tmp_path):
    """One record per rep: swapping onto an existing rep never opens a modal."""
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, rep_id=100, content="a")
    await _seed(cog, guild, rep_id=200, content="b")
    await _open(cog, ctx, member=_member(100), new_rep=_member(200))
    ctx.send_modal.assert_not_called()
    assert "já representa" in ctx.respond.await_args.args[0]


#: submit ----------------------------------------------------------------------


async def test_edit_submit_reposts_and_keeps_the_renewal_clock(tmp_path, monkeypatch):
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, raw="Fala {rep}! {ping_role}")
    deleted = []
    monkeypatch.setattr(cog, "_delete_ad", AsyncMock(side_effect=lambda g, e: deleted.append(e)))
    posted = {}

    async def fake_post(g, content, attachments, channel=None):
        posted["content"] = content
        return MagicMock(id=999)

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    monkeypatch.setattr(partnerships_module, "send_log", AsyncMock())

    await _open(cog, ctx, member=_member())
    interaction = await _submit(ctx.send_modal.await_args.args[0], "Fala {rep}! {ping_role}")
    assert posted["content"] == "Fala <@100>! <@&777>"
    assert len(deleted) == 1  # the old ad went away
    entry = await cog.store.get(guild.id, 100)
    assert entry is not None
    assert entry["content"] == posted["content"]
    assert entry["raw_text"] == "Fala {rep}! {ping_role}"
    assert entry["message_id"] == 999
    # An edit is not a renewal: the clock and the warning state stay put.
    assert entry["last_renewed_at"] == _PINNED_RENEWED
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "atualizado" in interaction.followup.send.await_args.args[0]


async def test_edit_submit_same_new_rep_treats_as_text_only(tmp_path, monkeypatch):
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, raw="texto {rep}")
    swaps = []
    monkeypatch.setattr(cog, "_strip_role", AsyncMock(side_effect=_recorder(swaps, "out")))
    monkeypatch.setattr(cog, "_assign_role", AsyncMock(side_effect=_recorder(swaps, "in")))
    monkeypatch.setattr(cog, "_post_ad", AsyncMock(return_value=MagicMock(id=999)))
    monkeypatch.setattr(cog, "_delete_ad", AsyncMock())

    await _open(cog, ctx, member=_member(100), new_rep=_member(100))
    await _submit(ctx.send_modal.await_args.args[0], "texto {rep}")
    assert swaps == []  # no role dance, no re-key
    assert await cog.store.get(guild.id, 100) is not None


async def test_edit_submit_swap_rekeys_moves_role_and_dms(tmp_path, monkeypatch):
    """Swapping the rep: repost names the new rep, the record moves to their
    key (so /partnerships renew finds it), roles transfer, and the new rep
    gets the rep_changed DM."""
    cog, guild, ctx = await _prepare(tmp_path)
    # Legacy record: only the resolved text, mentioning the old rep.
    await _seed(cog, guild, content="Anúncio de <@100>")
    posted = {}
    swaps = []
    dms = []
    log_vars = {}

    async def fake_post(g, content, attachments, channel=None):
        posted["content"] = content
        return MagicMock(id=999)

    async def fake_dm(g, user_id, key, **variables):
        dms.append((user_id, key))

    async def fake_log(guild_id, key, color, ids=(), **variables):
        log_vars.update(variables)

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    monkeypatch.setattr(cog, "_delete_ad", AsyncMock())
    monkeypatch.setattr(cog, "_strip_role", AsyncMock(side_effect=_recorder(swaps, "out")))
    monkeypatch.setattr(cog, "_assign_role", AsyncMock(side_effect=_recorder(swaps, "in")))
    monkeypatch.setattr(cog, "_dm", fake_dm)
    monkeypatch.setattr(cog, "_log", fake_log)

    await _open(cog, ctx, member=_member(100), new_rep=_member(200))
    interaction = await _submit(ctx.send_modal.await_args.args[0], "Anúncio de <@100>")
    # The legacy mention was rewritten to the new representative.
    assert posted["content"] == "Anúncio de <@200>"
    # The record moved to the new rep's key, clock untouched.
    assert await cog.store.get(guild.id, 100) is None
    entry = await cog.store.get(guild.id, 200)
    assert entry is not None
    assert entry["rep_id"] == 200
    assert entry["content"] == "Anúncio de <@200>"
    assert entry["last_renewed_at"] == _PINNED_RENEWED
    assert entry["message_id"] == 999
    # Role transfer + DM + log with the takeover.
    assert swaps == [("out", 100), ("in", 200)]
    assert dms == [(200, "rep_changed")]
    assert log_vars["user"] == "<@100>"
    assert log_vars["rep"] == "<@200>"
    assert log_vars["moderator"] == interaction.user.mention
    assert "transferida" in interaction.followup.send.await_args.args[0]


async def test_edit_submit_swap_resolves_rep_placeholder(tmp_path, monkeypatch):
    """A raw template's {rep} resolves to the NEW representative."""
    cog, guild, ctx = await _prepare(tmp_path)
    await _seed(cog, guild, raw="Fale com {rep}! {ping_role}")
    posted = {}
    monkeypatch.setattr(cog, "_delete_ad", AsyncMock())
    monkeypatch.setattr(cog, "_dm", AsyncMock())
    monkeypatch.setattr(cog, "_log", AsyncMock())

    async def fake_post(g, content, attachments, channel=None):
        posted["content"] = content
        return MagicMock(id=999)

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    await _open(cog, ctx, member=_member(100), new_rep=_member(200))
    await _submit(ctx.send_modal.await_args.args[0], "Fale com {rep}! {ping_role}")
    assert posted["content"] == "Fale com <@200>! <@&777>"

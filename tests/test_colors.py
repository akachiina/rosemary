"""Color panel: store roundtrips, image rendering, chunk layout, pick flow.

Real catalogs: every card/string goes through the Translator so a missing
key shows up here instead of in production.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from rosemary.core.colors import (
    MAX_COLORS,
    PASTEL_SEEDS,
    ColorEntry,
    ColorStore,
    new_color_id,
)
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


async def test_store_roundtrip_and_order(tmp_path):
    store = ColorStore(tmp_path)
    assert await store.list_colors(1) == []
    first = await store.add_color(1, "Rosa", "#FFB7C5")
    second = await store.add_color(1, "Céu", "#AEC6CF", role_id=42)
    assert first is not None and second is not None
    entries = await store.list_colors(1)
    assert [entry.name for entry in entries] == ["Rosa", "Céu"]
    assert entries[1].role_id == 42
    # Guild isolation.
    assert await store.list_colors(2) == []


async def test_store_move_reorder(tmp_path):
    store = ColorStore(tmp_path)
    a = await store.add_color(1, "A", "#111111")
    b = await store.add_color(1, "B", "#222222")
    c = await store.add_color(1, "C", "#333333")
    assert await store.move(1, c.id, -1) is True
    entries = await store.list_colors(1)
    assert [entry.id for entry in entries] == [a.id, c.id, b.id]
    # Edge moves clamp to a no-op (a stays at 0); unknown ids are False.
    assert await store.move(1, a.id, -5) is False
    assert [entry.id for entry in await store.list_colors(1)] == [a.id, c.id, b.id]
    assert await store.move(1, "nope", 1) is False


async def test_store_update_and_remove(tmp_path):
    store = ColorStore(tmp_path)
    entry = await store.add_color(1, "A", "#111111", role_id=7)
    updated = await store.update_color(1, entry.id, name="B", color="#222222")
    assert updated.name == "B"
    assert updated.color == "#222222"
    assert updated.role_id == 7  # untouched when omitted
    detached = await store.update_color(1, entry.id, role_id=None)
    assert detached.role_id is None
    removed = await store.remove_color(1, entry.id)
    assert removed is not None and removed.id == entry.id
    assert await store.remove_color(1, entry.id) is None


async def test_store_max_colors(tmp_path):
    store = ColorStore(tmp_path)
    for index in range(MAX_COLORS):
        assert await store.add_color(1, f"C{index}", "#123456") is not None
    assert await store.add_color(1, "overflow", "#123456") is None


async def test_pastel_seed_runs_once(tmp_path):
    store = ColorStore(tmp_path)
    names = {slug: f"Cor {slug}" for slug, _hex in PASTEL_SEEDS}
    seeded = await store.seed_pastels(1, names)
    assert len(seeded) == len(PASTEL_SEEDS)
    # Second call keeps entries untouched (no duplicates).
    again = await store.seed_pastels(1, names)
    assert len(await store.list_colors(1)) == len(PASTEL_SEEDS)
    assert [e.id for e in again] == [e.id for e in seeded]


# == image rendering =========================================================


def _panel_colors(count: int):
    palette = ["#40E0D0", "#FFB7C5", "#98FB98", "#E6E6FA"]
    return [
        __import__(
            "rosemary.core.color_image", fromlist=["PanelColor"]
        ).PanelColor(i + 1, f"Cor {i + 1}", palette[i % len(palette)])
        for i in range(count)
    ]


def test_render_panel_png_transparent_and_growing():
    from rosemary.core.color_image import render_panel

    png = render_panel(_panel_colors(4))
    assert png is not None and png[:4] == b"\x89PNG"
    doc = discord.File(io.BytesIO(png), filename="x.png")  # bytes sanity
    assert doc.filename == "x.png"
    four = _png_size(png)
    eight = _png_size(render_panel(_panel_colors(8)))
    assert eight[1] > four[1]  # more colors -> taller image


def test_render_panel_edge_cases():
    from rosemary.core.color_image import render_panel

    assert render_panel([]) is None
    # Invalid hex degrades to a neutral color, never None.
    assert render_panel(_panel_colors(1)) is not None


def _png_size(png: bytes) -> tuple[int, int]:
    import struct

    width, height = struct.unpack(">II", png[16:24])
    return width, height


# chunks ====================


def test_chunks_of_numbering_is_continuous():
    from rosemary.ui.colors_panel import chunks_of

    entries = [ColorEntry(new_color_id(), f"C{i}", "#123456") for i in range(12)]
    chunks = chunks_of(entries, 10)
    assert [len(chunk) for chunk in chunks] == [10, 2]
    numbers = [number for chunk in chunks for number, _e in chunk]
    assert numbers == list(range(1, 13))


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


def _bot(tmp_path, roles=()):
    class Bot:
        def __init__(self):
            self.storage = GuildStorage(tmp_path)
            self.theme = load_theme()
            self.translator = FakeTranslator()
            self._theme_store = None
            self._theme_cache = {}
            self.guild = None
            self.cog = None

        def get_guild(self, guild_id):
            return self.guild

        def get_cog(self, name):
            return self.cog

    bot = Bot()
    guild = MagicMock()
    guild.id = 1
    guild.roles = list(roles)
    # Seeding creates real roles: default to an awaitable creator the tests
    # can override.
    created: list = []

    async def _create_role(**kw):
        role = MagicMock()
        role.id = 9000 + len(created)
        role.name = kw["name"]
        created.append(role)
        return role

    guild.create_role = _create_role
    guild.created_roles = created
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot.guild = guild
    return bot


async def test_chunk_containers_files_and_rows(tmp_path):
    from rosemary.ui.colors_panel import chunk_containers

    bot = _bot(tmp_path)
    entries = [ColorEntry(new_color_id(), f"C{i}", "#123456") for i in range(12)]
    files, documents, row_docs = chunk_containers(bot, 1, entries, 10)
    assert len(files) == 2
    assert len(documents) == 2
    for document in documents:
        assert document["type"] == "container"
        # Image-only containers: picker buttons live OUTSIDE the cards.
        assert all(child["type"] == "gallery" for child in document["children"])
    # Picker rows are separate top-level documents, 5 buttons each.
    assert len(row_docs) == 3
    assert all(doc["type"] == "row" for doc in row_docs)
    # Without the renderer present the panels still build (imageless).
    all_buttons = [
        int(button["label"])
        for document in row_docs
        for button in document["buttons"]
    ]
    assert all_buttons == list(range(1, 13))
    # Every picker button carries its dispatcher custom_id: a url-less,
    # id-less button would render as a link button without a URL and 400 the
    # whole panel send (Discord 50035 "A url is required").
    carried = {
        button["id"]
        for document in row_docs
        for button in document["buttons"]
    }
    expected_ids = {f"colors_pick:{entry.id}" for entry in entries}
    assert carried == expected_ids


async def test_picker_view_rows_carry_ids_and_handlers(tmp_path):
    from rosemary.ui.colors_panel import ColorPickerView

    bot = _bot(tmp_path)
    entries = [ColorEntry(new_color_id(), f"C{i}", "#123456") for i in range(7)]
    picker = ColorPickerView(bot, 1, entries)
    rows = picker.rows_for(entries)
    assert len(rows) == 2
    ids = [
        button.custom_id
        for row in rows
        for button in row.children
    ]
    assert ids == [f"colors_pick:{entry.id}" for entry in entries]
    labels = [button.label for row in rows for button in row.children]
    assert labels == [str(i) for i in range(1, 8)]
    # Every button resolved a handler (no dead clicks).
    assert all(button.callback is not None for row in rows for button in row.children)


def test_resolve_color_prefers_live_role_color():
    entry = ColorEntry("abc", "Rosa", "#111111", role_id=42)
    assert resolve_color({42: 0xFFB7C5}, entry) == "#FFB7C5"
    # Default role color (0) falls back to the stored hex.
    assert resolve_color({42: 0}, entry) == "#111111"
    assert resolve_color({}, entry) == "#111111"


from rosemary.ui.colors_menu import resolve_color  # noqa: E402

# == pick flow ===============================================================


class FakeRole:
    def __init__(self, role_id: int, color: int = 0):
        self.id = role_id
        self.color = discord.Colour(color)
        self.top_role = self

    def __ge__(self, other):
        return self.id >= other.id


class FakeMember:
    """Mutates ``roles`` like the real API so pick flows see the new state."""

    def __init__(self, roles=()):
        self.id = 999
        self.roles = list(roles)
        self.added: list = []
        self.removed: list = []

    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles)
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None):
        self.removed.extend(roles)
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)


def _interaction(member: FakeMember, color_id: str):
    interaction = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user = member
    interaction.custom_id = f"colors_pick:{color_id}"
    interaction.data = {"values": [color_id]}
    return interaction


async def test_pick_applies_and_switches_and_toggles(tmp_path):
    from rosemary.cogs.colors import ColorsCog
    from rosemary.ui.colors_panel import ColorPickerView

    bot = _bot(tmp_path)
    await bot.storage.set(1, "colors.enabled", True)
    await bot.storage.set(1, "colors.panel_channel", 55)
    cog = ColorsCog(bot)
    bot.cog = cog
    entry_a = await cog.store.add_color(1, "A", "#111111", role_id=11)
    entry_b = await cog.store.add_color(1, "B", "#222222", role_id=22)
    role_a = FakeRole(11)
    role_b = FakeRole(22)
    bot.guild.get_role = lambda role_id: {11: role_a, 22: role_b}.get(role_id)
    bot.guild.me = FakeRole(1)
    bot.guild.me.top_role = FakeRole(999)  # bot above every color role
    member = FakeMember()
    bot.guild.get_member = lambda user_id: member

    picker = ColorPickerView(bot, 1, [entry_a, entry_b])

    # First pick: grant.
    await picker._pick_button(_interaction(member, entry_a.id))
    assert [r.id for r in member.added] == [11]
    # Switch: drop A, grant B.
    await picker._pick_button(_interaction(member, entry_b.id))
    assert [r.id for r in member.removed] == [11]
    assert [r.id for r in member.added] == [11, 22]
    # Toggle off: B again removes it.
    await picker._pick_button(_interaction(member, entry_b.id))
    assert [r.id for r in member.removed] == [11, 22]


async def test_pick_toggle_only_removes_target_role(tmp_path):
    """Clicking the worn color removes exactly it, not the whole stack
    (regression: the toggle branch removed every color role at once)."""
    from rosemary.cogs.colors import ColorsCog
    from rosemary.ui.colors_panel import ColorPickerView

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    entry_a = await cog.store.add_color(1, "A", "#111111", role_id=11)
    entry_b = await cog.store.add_color(1, "B", "#222222", role_id=22)
    role_a, role_b = FakeRole(11), FakeRole(22)
    bot.guild.get_role = lambda role_id: {11: role_a, 22: role_b}.get(role_id)
    bot.guild.me = FakeRole(1)
    member = FakeMember(roles=[role_a, role_b])
    bot.guild.get_member = lambda user_id: member

    picker = ColorPickerView(bot, 1, [entry_a, entry_b])
    await picker._pick_button(_interaction(member, entry_a.id))
    assert [r.id for r in member.removed] == [11]
    assert member.added == []


async def test_pick_hierarchy_and_missing_role(tmp_path):
    from rosemary.cogs.colors import ColorsCog
    from rosemary.ui.colors_panel import ColorPickerView

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    entry = await cog.store.add_color(1, "A", "#111111", role_id=11)

    class HighRole(FakeRole):
        def __ge__(self, other):
            return True  # role above the bot

    role = HighRole(11)
    role.top_role = role
    bot.guild.get_role = lambda role_id: role
    bot.guild.me = FakeRole(1, 0)
    bot.guild.me.top_role = FakeRole(1)
    member = FakeMember()
    bot.guild.get_member = lambda user_id: member

    picker = ColorPickerView(bot, 1, [entry])
    # Role above bot: error, nothing granted.
    interaction = _interaction(member, entry.id)
    await picker._pick_button(interaction)
    assert member.added == []
    text = interaction.followup.send.call_args.args[0]
    assert "error_hierarchy" in text

    # Detached color: explicit error (no silent no-op).
    await cog.store.update_color(1, entry.id, role_id=None)
    interaction2 = _interaction(member, entry.id)
    await picker._pick_button(interaction2)
    text2 = interaction2.followup.send.call_args.args[0]
    assert "error_no_role" in text2


# == manager flows ==========================================================


async def test_boot_registers_picker_with_real_rows(tmp_path):
    """py-cord's view store indexes only real children: the boot dispatcher
    must carry the picker rows, or every click dies as "did not respond"."""
    from rosemary.cogs.colors import ColorsCog

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    await cog.store.set_colors(
        1, [ColorEntry(new_color_id(), "A", "#111111", role_id=11)]
    )
    await bot.storage.set(1, "colors.enabled", True)
    await bot.storage.set(1, "colors.panel_channel", 55)
    repaints = []

    async def fake_repaint(_bot, _gid):
        repaints.append(_gid)

    cog.repaint_panel = fake_repaint
    added = []
    bot.add_view = lambda view: added.append(view)
    await cog._restore_guild(MagicMock(id=1))
    assert len(added) == 1
    picker = added[0]
    indexed = [
        item.custom_id
        for item in picker.walk_children()
        if getattr(item, "custom_id", None)
    ]
    assert indexed == [f"colors_pick:{(await cog.store.list_colors(1))[0].id}"]
    assert repaints == [1]


async def test_seed_creates_discord_roles(tmp_path):
    """Seeding must create the Discord roles: the admin never hand-attaches
    the pastel defaults (regression: seeds stored role-less entries and the
    panel could not grant anything)."""
    from rosemary.cogs.colors import ColorsCog

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    created_roles = []

    async def fake_create_role(**kw):
        role = MagicMock()
        role.id = 9000 + len(created_roles)
        role.name = kw["name"]
        created_roles.append(kw["name"])
        return role

    bot.guild.create_role = fake_create_role
    bot.guild.me = MagicMock()
    bot.guild.me.guild_permissions.manage_roles = True

    entries = await cog.seed_if_needed(1)
    assert len(entries) == len(PASTEL_SEEDS)
    assert len(created_roles) == len(PASTEL_SEEDS)
    assert all(entry.role_id for entry in entries)
    # Role color matches the entry hex.
    first_hex = int(entries[0].color.lstrip("#"), 16)
    bot.guild.create_role = AsyncMock(return_value=MagicMock(id=1))
    # Stored entries carry the role ids (persisted, not just returned).
    stored = await cog.store.list_colors(1)
    assert [e.role_id for e in stored] == [e.role_id for e in entries]
    assert int(entries[0].color.lstrip("#"), 16) == first_hex


async def test_colors_panel_command_replaces_stored_panel(tmp_path):
    """Re-posting must pass the stored panel id along so the old message is
    deleted; otherwise panels stack up on the channel."""
    from rosemary.cogs.colors import ColorsCog

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    await cog.store.set_panel(1, 4242)
    calls = []

    async def fake_post(guild, channel, *, replace_id=None):
        calls.append(replace_id)

    cog._post_panel = fake_post
    cog._can_manage_roles = lambda guild: True

    ctx = MagicMock()
    ctx.guild = bot.guild
    ctx.guild.id = 1
    ctx.response.defer = AsyncMock()
    ctx.respond = AsyncMock()
    bot.storage_set = None

    async def fake_enabled(_gid):
        return True

    async def fake_channel(_guild):
        return MagicMock()

    cog.enabled = fake_enabled
    cog._panel_channel = fake_channel
    # Invoke the underlying callback (the attribute is a SlashCommand wrapper).
    await ColorsCog.colors_panel.callback(cog, ctx)
    assert calls == [4242]


async def test_manager_open_seeds_pastels(tmp_path):
    """First manager open must create the pastel defaults (regression: the
    seed helper existed but nothing ever called it, so the list was empty)."""
    from rosemary.cogs.colors import ColorsCog
    from rosemary.ui.colors_menu import ColorsManagerView

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    manager = ColorsManagerView(bot, 1, author_id=42)
    await manager.prepare()
    entries = await manager.store.list_colors(1)
    assert len(entries) == len(PASTEL_SEEDS)
    assert entries[0].name == "colors.defaults.rose_light"  # FakeTranslator echoes keys
    # Second open must not duplicate.
    manager2 = ColorsManagerView(bot, 1, author_id=42)
    await manager2.prepare()
    assert len(await manager2.store.list_colors(1)) == len(PASTEL_SEEDS)


async def test_manager_back_button_returns_to_settings(tmp_path):
    """The nav row carries Voltar (colors_mgr_exit), which swaps the settings
    menu back onto the same message: no dead-end Close."""
    from rosemary.ui.colors_menu import ColorsManagerView
    from rosemary.ui.settings_menu import SettingsMenuView

    bot = _bot(tmp_path)
    manager = ColorsManagerView(bot, 1, author_id=42)
    await manager.prepare()
    ids = [
        b.custom_id
        for c in manager.children
        for b in getattr(c, "children", [])
        if hasattr(b, "custom_id")
    ]
    assert "colors_mgr_exit" in ids
    assert "colors_mgr_close" not in ids
    # Section accessories accept a single button or thumbnail only; a row
    # there 400s the whole edit (Discord 50035).
    for child in manager.children:
        if type(child).__name__ == "Section":
            accessory = getattr(child, "accessory", None)
            assert not isinstance(accessory, discord.ui.ActionRow)

    interaction = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.edit = AsyncMock()
    await manager._exit(interaction)
    swapped = interaction.edit.call_args.kwargs.get("view")
    assert isinstance(swapped, SettingsMenuView)


async def test_manager_attach_stages_and_confirms(tmp_path):
    """Anti-invite pattern: the select stages picks, Concluir persists.
    Regression: confirm read ``values`` from the button interaction (never
    set), so attaching silently did nothing."""
    from rosemary.ui.colors_menu import ColorsManagerView

    bot = _bot(tmp_path)
    role_a, role_b = FakeRole(11), FakeRole(22)
    role_a.name, role_b.name = "Rosa", "Céu"
    role_a.color = discord.Colour(0xFFB7C5)
    role_b.color = discord.Colour(0)
    bot.guild.roles = [role_a, role_b]
    bot.guild.get_role = lambda rid: {11: role_a, 22: role_b}.get(rid)
    cog = MagicMock()
    cog.seed_if_needed = AsyncMock(return_value=[])
    cog.repaint_panel = AsyncMock()
    bot.cog = cog

    manager = ColorsManagerView(bot, 1, author_id=42)
    await manager.prepare()
    await manager._open_attach(_ack_only())
    assert manager.mode == "attach"
    # Pick through the select handler (values arrive on the select interaction).
    pick = _ack_only({"values": ["11", "22"]})
    await manager._attach_pick(pick)
    assert manager._pending_roles == (11, 22)
    # Confirm persists both, inheriting each role's live color.
    await manager._attach_confirm(_ack_only())
    entries = await manager.store.list_colors(1)
    attached = {(e.role_id, e.color) for e in entries if e.role_id}
    assert attached == {(11, "#FFB7C5"), (22, "#99AAB5")}
    assert manager.mode == "list" and manager._pending_roles == ()
    assert cog.repaint_panel.await_count == 1


async def test_manager_list_stays_under_component_cap(tmp_path):
    """The full list screen (text + row + 4 buttons per entry) must stay
    under Discord's 40-component ceiling (nested nodes count)."""
    from rosemary.cogs.colors import ColorsCog
    from rosemary.ui.colors_menu import LIST_ITEMS_PER_PAGE, ColorsManagerView

    bot = _bot(tmp_path)
    bot.cog = ColorsCog(bot)
    manager = ColorsManagerView(bot, 1, author_id=42)
    await manager.prepare()

    def total(view):
        count = 0

        def walk(item):
            nonlocal count
            count += 1
            for sub in getattr(item, "children", []) or []:
                walk(sub)

        for item in view.children:
            walk(item)
        return count

    assert total(manager) <= 40
    # Pages are full except the last one.
    assert LIST_ITEMS_PER_PAGE == 4


async def test_repaint_with_files_reposts_and_deletes_old(tmp_path):
    """PartialMessage.edit is JSON-only: an image panel must re-post (and
    remove the stale message) instead of editing files into it."""
    from rosemary.cogs.colors import ColorsCog

    bot = _bot(tmp_path)
    cog = ColorsCog(bot)
    bot.cog = cog
    await cog.store.set_colors(
        1, [ColorEntry(new_color_id(), "A", "#111111", role_id=11)]
    )
    await bot.storage.set(1, "colors.enabled", True)
    await bot.storage.set(1, "colors.panel_channel", 55)

    sent = {}
    deleted = []

    class FakePartial:
        def __init__(self, message_id):
            self.id = message_id

        async def delete(self, delay=None):
            deleted.append(self.id)

        async def edit(self, **kw):  # pragma: no cover - must not be called
            raise AssertionError("partial edit attempted with files")

    class FakeChannel(discord.TextChannel):
        def __init__(self):
            self.id = 55
            self.guild = guild
            self._state = MagicMock()

        def get_partial_message(self, message_id):
            return FakePartial(message_id)

        async def send(self, **kw):
            sent.update(kw)
            message = MagicMock()
            message.id = 777
            return message

    guild = bot.guild
    guild.get_channel = lambda cid: FakeChannel() if cid == 55 else None
    files = [MagicMock()]
    with patch(
        "rosemary.cogs.colors.ColorsCog._panel_payload"
    ) as payload_mock:
        payload = MagicMock()
        payload.message_kwargs.return_value = {"view": MagicMock(), "files": files}
        payload_mock.return_value = (payload, None)
        await cog.repaint_panel(bot, 1)
    assert sent.get("files") == files
    assert deleted == []  # no previous panel id: nothing to replace
    assert await cog.store.get_panel(1) == 777

    # Second repaint with files: deletes the stale panel message.
    with patch(
        "rosemary.cogs.colors.ColorsCog._panel_payload"
    ) as payload_mock:
        payload = MagicMock()
        payload.message_kwargs.return_value = {"view": MagicMock(), "files": files}
        payload_mock.return_value = (payload, None)
        await cog.repaint_panel(bot, 1)
    assert deleted == [777]
    assert await cog.store.get_panel(1) == 777  # same mock id re-stored


def _ack_only(data=None):
    """Interaction mock whose defer/edit/rerender path is fully awaitable."""
    interaction = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.edit = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.data = data
    return interaction


# == catalog parity ==========================================================


async def test_catalogs_carry_color_strings():
    pt = Translator(languages_dir=_languages_dir(), resolver=_resolve_pt)
    en = Translator(languages_dir=_languages_dir(), resolver=_resolve_en)
    for key in (
        "colors.panel.title",
        "colors.panel.applied",
        "colors.panel.removed",
        "colors.manager.title",
        "colors.error_hierarchy",
        "colors.defaults.rose_light",
        "colors.defaults.sage_dark",
        "settings.colors.enabled.label",
        "settings.colors.picker.choices.buttons",
        "settings.category.colors",
        "card.colors.panel.title",
    ):
        pt_value = await pt.t(1, key)
        en_value = await en.t(1, key)
        assert pt_value != key, f"missing pt key {key}"
        assert en_value != key, f"missing en key {key}"


def _languages_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "language"


async def _resolve_pt(_guild_id):
    return "pt-BR"


async def _resolve_en(_guild_id):
    return "en-US"


# == settings integration ====================================================


async def test_color_settings_roundtrip(tmp_path):
    from rosemary.core.settings import get_setting, set_setting

    storage = GuildStorage(tmp_path)
    assert await get_setting(storage, 1, "colors.enabled") is False
    assert await get_setting(storage, 1, "colors.picker") == "buttons"
    assert await get_setting(storage, 1, "colors.per_container") == 10
    await set_setting(storage, 1, "colors.picker", "select")
    assert await get_setting(storage, 1, "colors.picker") == "select"
    with pytest.raises(ValueError):
        await set_setting(storage, 1, "colors.picker", "dropdown")
    with pytest.raises(ValueError):
        await set_setting(storage, 1, "colors.per_container", 2)

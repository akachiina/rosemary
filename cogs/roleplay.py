"""Roleplay: action cards with gifs between members.

``/roleplay hug @user`` (or ``!hug @user`` with the guild prefix) posts a
V2 card: the action sentence as the body (``@A abraçou @B``, both members
ping through the mention fields), a MediaGallery carrying the gif (URL only,
no upload) and a relative-timestamp footer. The row carries **Retribuir**
(target-only; sends a fresh card with the roles swapped, labelled with the
action: "Abraçar de volta") plus the **magnifier** linking the resolved gif
(even when the gif came from the API: the resolved url IS the source).
After a re-give the old card is edited in place: its components are
converted back, the re-give button is disabled and the card stays.

Gif resolution per action: the guild's ``roleplay.json`` override list
when seeded (first manager edit), else the theme's ``roleplay.gifs``
section, else the built-in nekos.best category (``attack`` maps to
``kick``). Entries are ``{url}`` (fixed) or ``{api: category}`` (random
gif from that category); within a list one entry is picked at random.

The card is a first-class CardSpec (``roleplay.card``) with a default
builder: themes can restyle everything (the gallery self-drops when
``{gif_url}`` resolves empty); the regive row stays in code because buttons
need registered callbacks.

No ``from __future__ import annotations`` here: py-cord inspects the
``discord.Option`` annotations of the dynamic slash commands and stringified
annotations break option parsing (same rule as moderation/invites/info).
"""

import contextlib
import logging
from typing import Any

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload, render_card_message
from rosemary.core.cards import safe_format
from rosemary.core.localized_group import LocalizedSlashCommandGroup
from rosemary.core.mentions import allowed_for_ids
from rosemary.core.roleplay import (
    ACTIONS,
    RoleplayStore,
    category_for,
    resolve_gif,
)
from rosemary.core.settings import get_setting
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

#: The `/roleplay` group (one subcommand per action: `/roleplay beijar`).
#: Module-level so the object is shared, never duplicated: a group held as a
#: class attribute by two cogs would be copied and registered twice.
#: Subcommand localization reuses the legacy flat keys
#: (``roleplay_hug.command``), so no catalog churn. The group name itself is
#: fixed (never localized); only subcommand names/descriptions are per guild.
roleplay_group = LocalizedSlashCommandGroup(
    "roleplay",
    description="Send a roleplay card",
    contexts={discord.InteractionContextType.guild},
)

def _member_slash(action: str):
    """Build one group subcommand for an action (name localized per guild).

    Module-level on purpose: the cog holds the group as a class attribute,
    and py-cord copies it at class-definition time, so subcommands attached
    later (e.g. from ``__init__``) would never reach the registered copy.
    The first parameter takes the invoking cog: group subcommands always run
    with ``.cog`` set (``callback(cog, ctx, ...)``).
    """

    @roleplay_group.command(
        name=action,
        description="Send a roleplay card",
    )
    async def cmd(
        cog,
        ctx: discord.ApplicationContext,
        member: discord.Option(
            discord.Member, description="Who receives the action"
        ),
    ) -> None:
        await cog._slash_run(ctx, action, member)

    cmd.__name__ = f"roleplay_{action}"
    return cmd


for _action in ACTIONS:
    _member_slash(_action)

#: Prefix-command aliases: English verb -> action key (plus the bare key).
_PREFIX_ALIASES: dict[str, str] = {
    "hug": "hug",
    "kiss": "kiss",
    "pat": "pat",
    "slap": "slap",
    "bite": "bite",
    "punch": "punch",
    "attack": "attack",
    "poke": "poke",
    "dance": "dance",
    "highfive": "highfive",
    "stare": "stare",
    "cuddle": "cuddle",
    # Portuguese verbs (the slash commands are localized; prefix stays latin).
    "abraco": "hug",
    "abracar": "hug",
    "beijar": "kiss",
    "cafuné": "pat",
    "cafune": "pat",
    "bofetada": "slap",
    "morder": "bite",
    "socar": "punch",
    "atacar": "attack",
    "cutucar": "poke",
    "dancar": "dance",
    "dançar": "dance",
    "toque": "highfive",
    "encarar": "stare",
    "dormir": "cuddle",
}

_REGIVE_PREFIX = "roleplay_regive"

#: The posted panel's ids: the native user select and the action buttons.
_PICK_ID = "roleplay_pick"
_PLAY_PREFIX = "roleplay_play"


async def _panel_target_id(interaction: discord.Interaction) -> int:
    """The member picked in the panel's native user select (0 when none).

    The selected value lives in the message's components, not in a view
    field: the panel is one message and the dispatcher may be the boot view
    (a fresh object), so reading the message is the only stable source.
    The panel's container is the first component; the select is nested.
    """
    message = getattr(interaction, "message", None)
    for component in getattr(message, "components", None) or []:
        stack = [component]
        while stack:
            node = stack.pop(0)
            if getattr(node, "custom_id", None) == _PICK_ID:
                values = list(getattr(node, "values", None) or [])
                try:
                    return int(values[0]) if values else 0
                except (TypeError, ValueError):
                    return 0
            children = list(getattr(node, "children", None) or []) + list(
                getattr(node, "items", None) or []
            )
            stack.extend(children)
    return 0

#: Pre-rename custom_id of the regive button. Cards posted before the
#: interactions -> roleplay rename carry this id; the dispatcher answers it
#: with the same handler so old buttons keep working.
_LEGACY_REGIVE_PREFIX = "interactions_regive"


def _sentence_key(action: str) -> str:
    return f"roleplay.actions.{action}"


async def _regive_row(
    bot,
    guild_id: int,
    action: str | None = None,
    source_url: str = "",
    disabled: bool = False,
    handler: Any = None,
) -> discord.ui.ActionRow:
    """The interaction row: re-give the action, or read the gif source.

    The re-give label names the action ("Abraçar de volta"): a generic
    "Retribuir" would make every card look the same. After the action was
    answered the button is disabled ("Retribuído"): it would only repeat a
    card that already exists. The source link is the magnifier alone: the
    card already has a footer, and a second label would crowd the row.

    ``handler`` is the dispatcher callback the button binds to (the
    dispatcher's own method); the row sent with the card passes it, the
    disabled row rebuilt in place passes none (a disabled button never fires).
    """
    theme = getattr(bot, "theme", None)
    text = await _regive_label(bot, guild_id, action, disabled)
    items: list[discord.ui.ViewItem] = [
        discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=text,
            emoji=theme.emoji("love_letter") if theme is not None else "💌",
            custom_id=_REGIVE_PREFIX,
            disabled=disabled,
        )
    ]
    if handler is not None:
        items[0].callback = handler
    if source_url:
        items.append(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                url=source_url,
                emoji=theme.emoji("search") if theme is not None else "🔍",
            )
        )
    return discord.ui.ActionRow(*items)


async def _regive_label(
    bot, guild_id: int, action: str | None = None, disabled: bool = False
) -> str:
    """The re-give label: the action's name, or the answered state.

    A card whose action the catalog does not name falls back to the generic
    "Retribuir": a raw key would ship to the member.
    """
    generic = "roleplay.regive"
    if disabled:
        key = "roleplay.regiven"
    elif action:
        key = f"roleplay.regive_labels.{action}"
    else:
        key = generic
    text = await bot.translator.t(guild_id, key)
    if text == key and key != generic:
        text = await bot.translator.t(guild_id, generic)
    return text


class RegiveView(MenuView):
    """One persistent dispatcher row for every interaction card.

    The regive button's custom_id is FIXED: the (actor, target, action)
    triple lives in the store keyed by the sent message's id, so one view
    serves every card (no per-card view accumulation in the ViewStore) and
    clicks keep working after a restart. The source link always points at
    the resolved gif url (API gifs included: that url IS the source).
    """

    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.register(_REGIVE_PREFIX, self._regive)
        self.register(_LEGACY_REGIVE_PREFIX, self._regive)

    async def build_row(
        self,
        action: str | None = None,
        source_url: str = "",
        disabled: bool = False,
    ) -> discord.ui.ActionRow:
        """The interaction row; the source link needs a real URL at send time."""
        return await _regive_row(
            self.bot,
            self.guild_id,
            action,
            source_url,
            disabled,
            handler=self._regive if not disabled else None,
        )

    async def _regive(self, interaction: discord.Interaction) -> None:
        """Resolve the card from the message id; only the target may regive."""
        message = getattr(interaction, "message", None)
        guild = interaction.guild
        if message is None or guild is None:
            return
        cog = self.bot.get_cog("RoleplayCog")
        if cog is None:
            return
        context = await cog.store.get_regive(guild.id, message.id)
        if context is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        guild.id, "roleplay.error_gone"
                    ),
                    ephemeral=True,
                )
            return
        member = interaction.user
        if getattr(member, "id", None) != context.get("target"):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        guild.id, "roleplay.error_not_target"
                    ),
                    ephemeral=True,
                )
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        action = str(context["action"])
        actor = guild.get_member(int(context["target"]))
        target = guild.get_member(int(context["actor"]))
        if actor is None or target is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        guild.id, "roleplay.error_gone"
                    ),
                    ephemeral=True,
                )
            return
        sent = await cog.send_interaction_card(
            guild, actor, target, action, channel=interaction.channel
        )
        if not sent:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        guild.id, "roleplay.error_no_gif"
                    ),
                    ephemeral=True,
                )
            return
        # The action was answered: the old card's button has nothing left to
        # do. Edit the message in place: its components are converted back
        # into a view (the card's text, gif and footer included), and only the
        # re-give button changes (disabled, "Retribuído"). Clearing the view
        # and adding a fresh row would delete the card.
        if message is not None:
            edited = discord.ui.DesignerView.from_message(message)
            found = False
            for item in edited.walk_children():
                if getattr(item, "custom_id", None) in (
                    _REGIVE_PREFIX,
                    _LEGACY_REGIVE_PREFIX,
                ):
                    item.disabled = True
                    item.label = await _regive_label(
                        self.bot, guild.id, disabled=True
                    )
                    found = True
            if not found:
                log.warning(
                    "The regive button of message %s in %s is gone; the card "
                    "stays as it is",
                    getattr(message, "id", "?"),
                    guild.id,
                )
            else:
                try:
                    await message.edit(view=edited)
                except discord.HTTPException as exc:
                    log.warning(
                        "Could not disable the regive row of message %s in %s: %s",
                        getattr(message, "id", "?"),
                        guild.id,
                        exc,
                    )


class RoleplayPanelView(MenuView):
    """The posted roleplay panel: pick a member, then an action.

    The native user select resolves the target without typing; the action
    buttons send the card. The selected target lives on the view (the panel
    is one message), so the button knows who was picked.
    """

    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.register(_PLAY_PREFIX, self._play)
        self.register(_PICK_ID, self._pick)

    def _emoji(self, name: str, fallback: str) -> str | None:
        theme = getattr(self.bot, "theme", None)
        value = theme.emoji(name) if theme is not None else ""
        return value or fallback or None

    async def panel_items(self) -> list[discord.ui.ViewItem]:
        """The user select and the action buttons (5 per row)."""
        items: list[discord.ui.ViewItem] = [
            discord.ui.ActionRow(
                self.make_native_select(
                    custom_id=_PICK_ID,
                    select_type=discord.ComponentType.user_select,
                    placeholder=await self.bot.translator.t(
                        self.guild_id, "roleplay.panel.pick"
                    ),
                )
            )
        ]
        row: list[discord.ui.ViewItem] = []
        for action in ACTIONS:
            row.append(
                self.make_button(
                    custom_id=f"{_PLAY_PREFIX}:{action}",
                    label=await self.bot.translator.t(
                        self.guild_id, f"roleplay.panel.actions.{action}"
                    ),
                    emoji=self._emoji("love_letter", "💌"),
                    style=discord.ButtonStyle.primary,
                )
            )
            if len(row) == 5:
                items.append(discord.ui.ActionRow(*row))
                row = []
        if row:
            items.append(discord.ui.ActionRow(*row))
        return items

    async def _pick(self, interaction: discord.Interaction) -> None:
        """The user select answers: the action buttons read the message."""
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _play(self, interaction: discord.Interaction) -> None:
        action = (interaction.custom_id or "").split(":", 1)[-1]
        guild = interaction.guild
        cog = self.bot.get_cog("RoleplayCog")
        t = self.bot.translator.t
        if guild is None or cog is None or action not in ACTIONS:
            return
        if not await cog._enabled(guild.id):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await t(guild.id, "roleplay.error_disabled"), ephemeral=True
                )
            return
        # The target comes from the select on the message the button rides:
        # the panel is one message, and the selected value is on the
        # message's components (no in-memory state, no stale target).
        target_id = await _panel_target_id(interaction)
        target = guild.get_member(target_id) if target_id else None
        if target is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await t(guild.id, "roleplay.panel.pick_first"), ephemeral=True
                )
            return
        if target.bot:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await t(guild.id, "roleplay.error_bot"), ephemeral=True
                )
            return
        if target.id == interaction.user.id:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await t(guild.id, "roleplay.error_self"), ephemeral=True
                )
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await cog.send_interaction_card(
            guild, interaction.user, target, action, channel=interaction.channel
        )


class RoleplayCog(commands.Cog):
    """The roleplay surface: action cards with gifs."""

    # Held as a class attribute so add_cog registers the group (with the
    # subcommands attached below by register_action_commands).
    group = roleplay_group

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = RoleplayStore(bot.storage.data_dir)
        self._regive_view: RegiveView | None = None
        self._panel_target: dict[int, int] = {}

    async def start(self) -> None:
        """Register the persistent regive dispatcher and the posted panels."""
        view = RegiveView(self.bot, guild_id=0)
        # py-cord's ViewStore indexes only walk_children() results: an empty
        # dispatcher view registers nothing and every click dies unanswered.
        view.add_item(await view.build_row())
        self.bot.add_view(view)
        self._regive_view = view
        from rosemary.core.panels import register as register_panel

        register_panel(
            "roleplay.panel",
            self.repaint_panel,
            setting_keys=("roleplay.enabled",),
        )
        for guild in self.bot.guilds:
            if await self._enabled(guild.id):
                panel = RoleplayPanelView(self.bot, guild.id)
                for item in await panel.panel_items():
                    panel.add_item(item)
                self.bot.add_view(panel)
                await self.repaint_panel(self.bot, guild.id)

    @roleplay_group.command(
        name="panel",
        description="[ADMIN] Post the roleplay panel",
    )
    async def roleplay_panel(self, ctx: discord.ApplicationContext) -> None:
        """Post the roleplay panel (user select + action buttons)."""
        guild = ctx.guild
        if guild is None:
            return
        if not await ctx.author.guild_permissions.administrator:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "general.no_permission"),
                ephemeral=True,
            )
        if not await self._enabled(guild.id):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "roleplay.error_disabled"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        await self._set_panel_channel(guild.id, ctx.channel.id)
        await self._post_panel(guild, ctx.channel)
        await ctx.followup.send(
            await self.bot.translator.t(guild.id, "roleplay.panel.posted"),
            ephemeral=True,
        )

    #: helpers ---------------------------------------------------------------

    async def _enabled(self, guild_id: int) -> bool:
        return bool(
            await get_setting(self.bot.storage, guild_id, "roleplay.enabled")
        )

    #: panel -----------------------------------------------------------------

    async def _panel_view(self, guild_id: int):
        """The roleplay panel: the card frame + the action rows.

        The member picks the target in the native user select, then clicks
        the action ("Abraçar", "Beijar"...): no command, no typing. The
        select and the buttons are code (the callbacks are registered), the
        document owns the heading and the body.

        The panel view is rebuilt on every render (and registered as a
        persistent view), so its selected target is read from the message's
        select through the dispatcher instead of a stale in-memory value.
        """
        from rosemary.core.card_service import render_card_message, simple_payload

        payload, _allowed = await render_card_message(
            self.bot, guild_id, "roleplay.panel"
        )
        if payload is None:
            payload = simple_payload(
                self.bot,
                guild_id,
                "roleplay.panel",
                title=await self.bot.translator.t(guild_id, "roleplay.panel.title"),
                body=await self.bot.translator.t(guild_id, "roleplay.panel.text"),
                color="social",
            )
        panel = RoleplayPanelView(self.bot, guild_id)
        items = await panel.panel_items()
        if payload.embed is not None:
            view = payload.view or discord.ui.View(timeout=None)
            for item in items:
                view.add_item(item)
            from rosemary.core.card_payload import CardPayload

            return CardPayload(embed=payload.embed, view=view)
        if payload.view is not None:
            for item in items:
                payload.view.add_item(item)
        return payload

    async def _post_panel(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> None:
        message = await channel.send(
            **(await self._panel_view(guild.id)).message_kwargs()
        )
        await self.store.set_panel(guild.id, message.id)

    async def repaint_panel(self, bot, guild_id: int) -> None:
        """Edit the posted panel in place (theme/setting changed)."""
        if not await self._enabled(guild_id):
            return
        guild = bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(
            int((await self.store._doc(guild_id)).get("panel_channel") or 0)
        )
        if not isinstance(channel, discord.TextChannel):
            return
        payload = await self._panel_view(guild_id)
        panel_id = await self.store.get_panel(guild_id)
        if panel_id is not None:
            try:
                await channel.get_partial_message(panel_id).edit(
                    **payload.message_kwargs()
                )
                return
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Roleplay panel repaint failed in %s: %s", guild_id, exc)
                return
        await self._post_panel(guild, channel)

    async def _set_panel_channel(self, guild_id: int, channel_id: int) -> None:
        doc = await self.store._doc(guild_id)
        doc["panel_channel"] = channel_id
        await self.store._save(guild_id, doc)

    async def gifs_for(self, guild_id: int, action: str) -> list[dict[str, str]]:
        """Guild overrides when seeded, else the theme's built-in list."""
        gifs = await self.store.get_gifs(guild_id, action)
        if gifs:
            return gifs
        theme = self.bot.theme
        return theme.roleplay_gifs(action) if theme is not None else []

    #: card send -------------------------------------------------------------

    async def send_interaction_card(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        action: str,
        channel: discord.abc.Messageable | None = None,
    ) -> bool:
        """Resolve a gif and post the card (best-effort pings).

        Returns ``False`` when no gif source works (callers answer the user
        with the no-gif notice instead of leaving the click silent).
        """
        guild_id = guild.id
        configured = await self.gifs_for(guild_id, action)
        no_repeat = bool(
            await get_setting(self.bot.storage, guild_id, "roleplay.no_repeat_gifs")
        )
        recent = await self.store.get_recent(guild_id, action) if no_repeat else []
        gif_url, from_api = await resolve_gif(
            configured, category_for(action), recent
        )
        if gif_url is None:
            return False
        if channel is None:
            return False
        if no_repeat:
            with contextlib.suppress(Exception):
                await self.store.remember_gif(guild_id, action, gif_url)
        t = self.bot.translator.t
        action_text = await t(guild_id, _sentence_key(action))
        variables = {
            "actor": actor.mention,
            "actor_name": actor.display_name,
            "target": target.mention,
            "actor_avatar": actor.display_avatar.url,
            "action": action_text,
            "gif_url": gif_url,
            "timestamp": discord.utils.format_dt(discord.utils.utcnow(), style="R"),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "roleplay.card", variables
        )
        if payload is None:
            payload = await self._fallback_card(guild_id, variables)
            # Mentions are content: the fallback renders both mentions, so the
            # card still pings like the themed path does.
            allowed = await allowed_for_ids(
                self.bot, guild_id, "roleplay.card", user_ids=[actor.id, target.id]
            )
        view = RegiveView(self.bot, guild_id)
        # The source link always points at the resolved gif (API gifs
        # included: that exact url IS the source). The re-give label names
        # the action, so the row says what it does.
        row = await view.build_row(action=action, source_url=gif_url)
        view.add_item(row)
        if payload.embed is not None:
            embed_view = payload.view or discord.ui.View(timeout=None)
            for item in row.children:
                embed_view.add_item(item)
            # The content (the ping carrier) rides along: an embed card may
            # carry the essential ping in its content.
            payload = CardPayload(
                embed=payload.embed, view=embed_view, content=payload.content
            )
        else:
            payload.view.add_item(row)
        try:
            sent = await channel.send(**payload.message_kwargs(), allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Interaction card send failed in %s: %s", guild_id, exc)
            return True
        # The regive context keys on the posted message: without it the
        # button answers "gone". Send first, remember after. The source url
        # travels too: after a re-give the old row is rebuilt disabled and
        # the link must survive the edit.
        with contextlib.suppress(Exception):
            await self.store.set_regive(
                guild_id,
                sent.id,
                action,
                actor.id,
                target.id,
                source_url=gif_url,
            )
        return True

    async def _fallback_card(self, guild_id: int, variables: dict[str, str]) -> CardPayload:
        """Code-built card when even the default builder failed."""
        from rosemary.core.card_service import simple_payload

        theme = self.bot.theme
        emoji_map = getattr(theme, "emojis", {}) if theme else {}
        mapping = {**emoji_map, **variables}
        # Plain placeholders here (the fallback pings via allowed_for_ids):
        # `{@...}` would render literally outside the document renderer.
        sentence = safe_format(
            "{actor} {action} {target}",
            mapping,
        )
        return simple_payload(
            self.bot,
            guild_id,
            "roleplay.card",
            body=sentence,
            color="social",
        )

    #: prefix trigger ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Prefix triggers (``!hug @user``) using the guild's prefix."""
        if message.guild is None or message.author.bot:
            return
        if not await self._enabled(message.guild.id):
            return
        prefix = await get_setting(
            self.bot.storage, message.guild.id, "general.prefix"
        )
        if not prefix or not message.content.lower().startswith(prefix.lower()):
            return
        parts = message.content[len(prefix):].strip().split()
        if len(parts) < 2:
            return
        verb, raw_target = parts[0].lower(), parts[1]
        action = _PREFIX_ALIASES.get(verb) or (
            verb if verb in ACTIONS else None
        )
        if action is None:
            return
        target_id = _parse_mention(raw_target)
        if target_id is None:
            return
        target = message.guild.get_member(target_id)
        if target is None:
            return
        if target.bot:
            # Same answer the slash command gives; silence here would look
            # like a dead trigger.
            with contextlib.suppress(discord.HTTPException):
                await message.reply(
                    await self.bot.translator.t(
                        message.guild.id, "roleplay.error_bot"
                    ),
                    mention_author=False,
                )
            return
        if target.id == message.author.id:
            with contextlib.suppress(discord.HTTPException):
                await message.reply(
                    await self.bot.translator.t(
                        message.guild.id, "roleplay.error_self"
                    ),
                    mention_author=False,
                )
            return
        sent = await self.send_interaction_card(
            message.guild, message.author, target, action, channel=message.channel
        )
        if not sent:
            with contextlib.suppress(discord.HTTPException):
                await message.reply(
                    await self.bot.translator.t(
                        message.guild.id, "roleplay.error_no_gif"
                    ),
                    mention_author=False,
                )

    #: slash commands ---------------------------------------------------------

    async def _slash_run(
        self,
        ctx: discord.ApplicationContext,
        action: str,
        member: discord.Member,
    ) -> None:
        guild = ctx.guild
        # THINKING (public, not ephemeral): the card lands in the channel in
        # a few seconds; the placeholder is deleted right after the send, so
        # there is no "roleplay enviado!" clutter and no stuck "thinking".
        if not ctx.response.is_done():
            await ctx.response.defer()
        if not await self._enabled(guild.id):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "roleplay.error_disabled"),
                ephemeral=True,
            )
        if member.id == ctx.author.id:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "roleplay.error_self"),
                ephemeral=True,
            )
        if member.bot:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "roleplay.error_bot"),
                ephemeral=True,
            )
        sent = await self.send_interaction_card(
            guild, ctx.author, member, action, channel=ctx.channel
        )
        if not sent:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "roleplay.error_no_gif"),
                ephemeral=True,
            )
        # The card is the answer: the "thinking" placeholder goes away
        # instead of leaving a second "roleplay enviado!" message behind.
        with contextlib.suppress(discord.HTTPException):
            await ctx.delete(delay=2.0)

def _parse_mention(raw: str) -> int | None:
    """``<@123>`` / ``<@!123>`` / bare id -> id, else ``None``."""
    raw = raw.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!")
    if raw.isdigit():
        return int(raw)
    return None

"""Roleplay: action cards with gifs between members.

``/roleplay hug @user`` (or ``!hug @user`` with the guild prefix) posts a
V2 card: the action as the heading, the sentence as a body line
(``{love_letter} @A abraçou @B``, both members ping), a MediaGallery
carrying the gif (URL only, no upload), a relative-timestamp footer and
one button: **Retribuir** (target-only; sends a fresh card with the roles
swapped) plus **Fonte da imagem** linking the resolved gif (even when the
gif came from the API: the resolved url IS the source).

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

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload, render_card_message
from rosemary.core.cards import safe_format
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
roleplay_group = discord.SlashCommandGroup(
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

#: Pre-rename custom_id of the regive button. Cards posted before the
#: interactions -> roleplay rename carry this id; the dispatcher answers it
#: with the same handler so old buttons keep working.
_LEGACY_REGIVE_PREFIX = "interactions_regive"


def _sentence_key(action: str) -> str:
    return f"roleplay.actions.{action}"


async def default_interaction_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default roleplay card (``roleplay.card``).

    Layout: the heading names only the action (the theme's ``title``
    markdown template, ``# ...``, beside the actor's avatar thumbnail);
    the action sentence itself is a body line, never the title. Both
    members ping through ``{@actor}``/``{@target}`` content mentions. The
    gif gallery self-drops when ``{gif_url}`` resolves empty; a divider
    and the relative-timestamp footer close the card. Placeholders stay
    literal: this is a template resolved at render.
    """
    from rosemary.core.cards import safe_format

    theme = getattr(bot, "theme", None)
    emojis = getattr(theme, "emojis", {}) if theme else {}
    mapping = {**emojis, **variables}

    def raw(key: str) -> str:
        return safe_format(key, mapping)

    action_title = raw("{love_letter} {action}")
    heading = theme.md("title", title=action_title) if theme else f"# {action_title}"
    sentence = raw("{love_letter} {@actor} {action} {@target}")
    avatar = str(variables.get("actor_avatar") or "{actor_avatar}")
    return {
        "v": 1,
        "blocks": [
            {"type": "container", "color": "brand", "children": [
                {"type": "section",
                 "accessory": {"type": "thumbnail", "url": avatar},
                 "children": [
                     {"type": "text", "body": heading},
                 ]},
                {"type": "text", "body": sentence},
                {"type": "gallery", "urls": ["{gif_url}"]},
                {"type": "divider"},
                {"type": "text", "body": "-# {timestamp}"},
            ]}
        ],
    }


def _register_default_builder() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("roleplay.card", default_interaction_document)


_register_default_builder()


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

    async def build_row(self, source_url: str = ""):
        """The regive button; the source link needs a real URL at send time."""
        label = await self.bot.translator.t(self.guild_id, "roleplay.regive")
        items = [
            self.make_button(
                custom_id=_REGIVE_PREFIX,
                label=label,
                emoji=(
                    self.bot.theme.emoji("love_letter")
                    if getattr(self.bot, "theme", None)
                    else "💌"
                ),
                style=discord.ButtonStyle.primary,
            )
        ]
        if source_url:
            items.append(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    url=source_url,
                    label=None,
                    emoji=(
                        self.bot.theme.emoji("search")
                        if getattr(self.bot, "theme", None)
                        else "🔍"
                    ),
                )
            )
        return discord.ui.ActionRow(*items)

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
        if not sent and not interaction.response.is_done():
            await interaction.response.send_message(
                await self.bot.translator.t(
                    guild.id, "roleplay.error_no_gif"
                ),
                ephemeral=True,
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

    async def start(self) -> None:
        """Register the persistent regive dispatcher once per process."""
        view = RegiveView(self.bot, guild_id=0)
        # py-cord's ViewStore indexes only walk_children() results: an empty
        # dispatcher view registers nothing and every click dies unanswered.
        view.add_item(await view.build_row())
        self.bot.add_view(view)
        self._regive_view = view

    #: helpers ---------------------------------------------------------------

    async def _enabled(self, guild_id: int) -> bool:
        return bool(
            await get_setting(self.bot.storage, guild_id, "roleplay.enabled")
        )

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
        gif_url, from_api = await resolve_gif(configured, category_for(action))
        if gif_url is None:
            return False
        if channel is None:
            return False
        t = self.bot.translator.t
        action_text = await t(guild_id, _sentence_key(action))
        variables = {
            "actor": actor.mention,
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
        # included: that exact url IS the source).
        row = await view.build_row(source_url=gif_url)
        view.add_item(row)
        if payload.embed is not None:
            embed_view = payload.view or discord.ui.View(timeout=None)
            for item in row.children:
                embed_view.add_item(item)
            payload = CardPayload(embed=payload.embed, view=embed_view)
        else:
            payload.view.add_item(row)
        try:
            sent = await channel.send(**payload.message_kwargs(), allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Interaction card send failed in %s: %s", guild_id, exc)
            return True
        # The regive context keys on the posted message: without it the
        # button answers "gone". Send first, remember after.
        with contextlib.suppress(Exception):
            await self.store.set_regive(
                guild_id, sent.id, action, actor.id, target.id
            )
        return True

    async def _fallback_card(self, guild_id: int, variables: dict[str, str]) -> CardPayload:
        """Code-built card when even the default builder failed."""
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        view = DesignerView(store=False)
        emoji_map = getattr(theme, "emojis", {}) if theme else {}
        mapping = {**emoji_map, **variables}
        # Plain placeholders here (the fallback pings via allowed_for_ids):
        # `{@...}` would render literally outside the document renderer.
        heading_text = safe_format("{love_letter} {action}", mapping)
        sentence = safe_format(
            "{love_letter} {actor} {action} {target}",
            mapping,
        )
        heading = theme.md("title", title=heading_text) if theme else f"# {heading_text}"
        view.add_item(
            designer_container(
                theme.color("brand") if theme else discord.Colour.blurple(),
                TextDisplay(heading),
                TextDisplay(sentence),
            )
        )
        return CardPayload(view=view)

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
        if not prefix or not message.content.startswith(prefix):
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
        if target is None or target.bot:
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

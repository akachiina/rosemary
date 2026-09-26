"""Interactions (roleplay): action cards with gifs between members.

``/hug @user`` (or ``!hug @user`` with the guild prefix) posts a V2 card:
the action sentence (``@A hugged @B``), a MediaGallery carrying the gif
(URL only, no upload), a footer prompting ``/genero`` until both
participants picked a gender, and two buttons: **Retribuir** (target-only;
sends a fresh card with the roles swapped) and **Fonte da imagem** (link
button to the gif URL).

Gifs come from the guild's ``roleplay.json`` override list when seeded
(first manager edit), else from the theme's ``roleplay.gifs.<action>``
section. ``pick_gif`` matches the (actor, target) gender pair in layers
(exact -> partial -> unfiltered), so themed ``ff`` gifs serve pairs of
women without code knowing anything about genders.

The card is a first-class CardSpec (``interactions.card``): themes can
restyle the sentence/heading; the gallery and buttons stay in code because
galleries need real URLs and buttons need registered callbacks.
"""

from __future__ import annotations

import contextlib
import logging

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload, render_card_message
from rosemary.core.cards import safe_format
from rosemary.core.mentions import allowed_for_ids
from rosemary.core.roleplay import (
    ACTIONS,
    GENDERS,
    RoleplayStore,
    normalize_gender,
    pick_gif,
)
from rosemary.core.settings import get_setting
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

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

_REGIVE_PREFIX = "interactions_regive"


def _sentence_key(action: str) -> str:
    return f"interactions.actions.{action}"


class RegiveView(MenuView):
    """Persistent pair of buttons under an interaction card."""

    def __init__(self, bot, guild_id: int, action: str, actor_id: int, target_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.action = action
        self.actor_id = actor_id
        self.target_id = target_id
        self.register(f"{_REGIVE_PREFIX}", self._regive)

    async def build_row(self):
        """The regive button; the source link needs a real URL at send time."""
        cog = self.bot.get_cog("InteractionsCog")
        label = await self.bot.translator.t(self.guild_id, "interactions.regive")
        return discord.ui.ActionRow(
            self.make_button(
                custom_id=_REGIVE_PREFIX,
                label=label or "Retribuir",
                style=discord.ButtonStyle.primary,
            ),
            *(
                [cog.source_button(self.guild_id, self.action)]
                if cog is not None
                else []
            ),
        )

    async def _regive(self, interaction: discord.Interaction) -> None:
        """Only the original target may regive; roles swap in a fresh card."""
        member = interaction.user
        if getattr(member, "id", None) != self.target_id:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        self.guild_id, "interactions.error_not_target"
                    ),
                    ephemeral=True,
                )
            return
        guild = interaction.guild
        if guild is None:
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        cog = self.bot.get_cog("InteractionsCog")
        if cog is None:
            return
        actor = guild.get_member(self.target_id)
        target = guild.get_member(self.actor_id)
        if actor is None or target is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    await self.bot.translator.t(
                        self.guild_id, "interactions.error_gone"
                    ),
                    ephemeral=True,
                )
            return
        sent = await cog.send_interaction_card(
            guild, actor, target, self.action, channel=interaction.channel
        )
        if not sent and not interaction.response.is_done():
            await interaction.response.send_message(
                await self.bot.translator.t(
                    self.guild_id, "interactions.error_no_gif"
                ),
                ephemeral=True,
            )


class InteractionsCog(commands.Cog):
    """The roleplay surface: gendered action cards with gifs."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = RoleplayStore(bot.storage.data_dir)

    #: helpers ---------------------------------------------------------------

    async def _enabled(self, guild_id: int) -> bool:
        return bool(
            await get_setting(self.bot.storage, guild_id, "interactions.enabled")
        )

    async def gifs_for(self, guild_id: int, action: str) -> list[dict[str, str]]:
        """Guild overrides when seeded, else the theme's built-in list."""
        gifs = await self.store.get_gifs(guild_id, action)
        if gifs:
            return gifs
        theme = self.bot.theme
        return theme.roleplay_gifs(action) if theme is not None else []

    def source_button(self, guild_id: int, action: str) -> discord.ui.Button:
        """Link button to the configured gif URL (emoji-only label)."""
        url = ""
        with contextlib.suppress(Exception):
            gifs = self._sync_gifs(guild_id, action)
            if gifs:
                url = gifs[0]["url"]
        return discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=url or "https://example.com",
            label=None,
            emoji=self.bot.theme.emoji("search") if self.bot.theme else "🔍",
        )

    def _sync_gifs(self, guild_id: int, action: str) -> list[dict[str, str]]:
        """Synchronous gif lookup for link buttons (theme snapshot only)."""
        theme = self.bot.theme
        return theme.roleplay_gifs(action) if theme is not None else []

    async def resolve_gif(
        self, guild_id: int, action: str, actor_gender: str, target_gender: str
    ) -> dict[str, str] | None:
        return pick_gif(await self.gifs_for(guild_id, action), actor_gender, target_gender)

    #: card send -------------------------------------------------------------

    async def send_interaction_card(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        action: str,
        channel: discord.abc.Messageable | None = None,
    ) -> bool:
        """Resolve genders, pick a gif and post the card (best-effort pings).

        Returns ``False`` when no gif matches the pair (callers answer the
        user with the no-gif notice instead of leaving the click silent)."""
        guild_id = guild.id
        actor_gender = await self.store.get_gender(guild_id, actor.id)
        target_gender = await self.store.get_gender(guild_id, target.id)
        gif = await self.resolve_gif(guild_id, action, actor_gender, target_gender)
        if gif is None:
            return False
        if channel is None:
            return False
        t = self.bot.translator.t
        action_text = await t(guild_id, _sentence_key(action))
        needs_prompt = not await self.store.has_gender(
            guild_id, actor.id
        ) or not await self.store.has_gender(guild_id, target.id)
        footer = (
            await t(guild_id, "interactions.gender_footer") if needs_prompt else ""
        )
        variables = {
            "actor": actor.mention,
            "target": target.mention,
            "action": action_text,
            "gif_url": gif["url"],
            "footer": footer,
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, "interactions.card", variables
        )
        if payload is None:
            payload = await self._fallback_card(guild_id, variables)
            allowed = discord.AllowedMentions.none()
        view = RegiveView(self.bot, guild_id, action, actor.id, target.id)
        view.add_item(await view.build_row())
        if payload.embed is not None:
            embed_view = payload.view or discord.ui.View(timeout=None)
            for item in view.children:
                embed_view.add_item(item)
            payload = CardPayload(embed=payload.embed, view=embed_view)
        else:
            payload.view.add_item(view.children[0])
        allowed = await allowed_for_ids(
            self.bot, guild_id, "interactions.card", user_ids=[actor.id, target.id]
        )
        try:
            await channel.send(**payload.message_kwargs(), allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Interaction card send failed in %s: %s", guild_id, exc)
        return True

    async def _fallback_card(self, guild_id: int, variables: dict[str, str]) -> CardPayload:
        """Code-built card when no theme override/builder exists."""
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        view = DesignerView(store=False)
        emoji_map = getattr(theme, "emojis", {}) if theme else {}
        sentence = safe_format(
            "{action}\n{gif_url}",
            {**emoji_map, **variables},
        )
        view.add_item(
            designer_container(
                theme.color("brand") if theme else discord.Colour.blurple(),
                TextDisplay(
                    theme.md("title", title=variables["action"])
                    if theme
                    else variables["action"]
                ),
                TextDisplay(sentence),
            )
        )
        return CardPayload(view=view)

    #: slash commands ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Prefix triggers (``!hug @user``) using the guild's prefix."""
        if message.guild is None or message.author.bot:
            return
        if not await self._enabled(message.guild.id):
            return
        from rosemary.core.settings import get_setting as _gs

        prefix = await _gs(self.bot.storage, message.guild.id, "general.prefix")
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
                        message.guild.id, "interactions.error_self"
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
                        message.guild.id, "interactions.error_no_gif"
                    ),
                    mention_author=False,
                )

    def _member_slash(self, action: str, description_key: str):
        """Build one localized slash command for an action."""

        @commands.slash_command(
            name=f"interactions_{action}",
            description="Send an interaction card",
            contexts={discord.InteractionContextType.guild},
        )
        async def cmd(
            ctx: discord.ApplicationContext,
            member: discord.Option(
                discord.Member, description="Who receives the action"
            ),
        ) -> None:
            await self._slash_run(ctx, action, member, description_key)

        cmd.__name__ = f"interactions_{action}"
        return cmd

    async def _slash_run(
        self,
        ctx: discord.ApplicationContext,
        action: str,
        member: discord.Member,
        description_key: str,
    ) -> None:
        guild = ctx.guild
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        if not await self._enabled(guild.id):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "interactions.error_disabled"),
                ephemeral=True,
            )
        if member.id == ctx.author.id:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "interactions.error_self"),
                ephemeral=True,
            )
        if member.bot:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "interactions.error_bot"),
                ephemeral=True,
            )
        sent = await self.send_interaction_card(
            guild, ctx.author, member, action, channel=ctx.channel
        )
        if not sent:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "interactions.error_no_gif"),
                ephemeral=True,
            )
        with contextlib.suppress(discord.HTTPException):
            await ctx.respond(
                await self.bot.translator.t(guild.id, "interactions.sent"),
                ephemeral=True,
            )

    @commands.slash_command(
        name="interactions_gender",
        description="Set your gender for interaction cards",
        contexts={discord.InteractionContextType.guild},
    )
    async def interactions_gender(
        self,
        ctx: discord.ApplicationContext,
        gender: discord.Option(
            str,
            description="Your gender",
            choices=[discord.OptionChoice(name=g, value=g) for g in GENDERS],
        ),
    ) -> None:
        """/genero: store the author's gender (ephemeral confirmation)."""
        guild = ctx.guild
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        stored = await self.store.set_gender(guild.id, ctx.author.id, normalize_gender(gender))
        label = await self.bot.translator.t(guild.id, f"interactions.genders.{stored}")
        await ctx.respond(
            await self.bot.translator.t(guild.id, "interactions.gender_set", gender=label),
            ephemeral=True,
        )

    async def _gender_command(self):
        """Registered as ``/genero``-style localized commands at boot."""

    #: registration -----------------------------------------------------------

    async def register_action_commands(self) -> None:
        """Add one localized slash command per action (called from start())."""
        for action in ACTIONS:
            cmd = self._member_slash(
                action, f"interactions.actions.{action}.description"
            )
            self.bot.add_application_command(cmd)

    async def start(self) -> None:
        await self.register_action_commands()


def _parse_mention(raw: str) -> int | None:
    """``<@123>`` / ``<@!123>`` / bare id -> id, else ``None``."""
    raw = raw.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!")
    if raw.isdigit():
        return int(raw)
    return None

"""Runtime admin guard for ``SlashCommandGroup`` subcommands.

Discord reads ``default_member_permissions`` only on top-level application
commands. Inside a ``SlashCommandGroup`` py-cord serializes the child's
declaration under ``options``, where the API ignores it, so the whole group
stays executable by every member. Every restricted subcommand of a mixed
group must therefore enforce its permission in code via
:func:`require_permissions` (or :func:`require_admin` for Administrator).

Pure top-level commands and fully-admin groups declared with the kwarg on the
group itself keep working as declared; they may still use the guards as
defense in depth.
"""

import contextlib
import logging

import discord

log = logging.getLogger(__name__)


async def require_permissions(
    ctx: discord.ApplicationContext, permission: str = "administrator"
) -> bool:
    """Allow only members holding ``permission`` on the guild.

    The server owner always holds every permission, so no separate owner
    branch is needed. Returns ``True`` when allowed. On denial, answers the
    interaction with an ephemeral localized error (followup when already
    ACKed) and logs the attempt; returns ``False`` so callers just ``return``.
    """
    perms = getattr(ctx.author, "guild_permissions", None)
    if perms is not None and getattr(perms, permission, False):
        return True
    log.warning(
        "Denied admin command /%s in %s by %s (missing %s)",
        getattr(ctx.command, "qualified_name", "?"),
        ctx.guild_id,
        getattr(ctx.author, "id", None),
        permission,
    )
    text = await ctx.bot.translator.t(ctx.guild_id, "general.no_permission")
    if not ctx.response.is_done():
        await ctx.respond(text, ephemeral=True)
    else:
        with contextlib.suppress(discord.HTTPException):
            await ctx.followup.send(text, ephemeral=True)
    return False


async def require_admin(ctx: discord.ApplicationContext) -> bool:
    """Helper of :func:`require_permissions` pinned to Administrator."""
    return await require_permissions(ctx, "administrator")

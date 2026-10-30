"""Interaction lifetime: Discord's 15 minute token window.

Discord's interaction tokens are valid for 15 minutes from the moment the
interaction was created ("Interaction tokens are valid for 15 minutes",
Discord API docs). A gateway RESUME replays the events the bot missed, so a
click made while the bot was offline can arrive long after its token died:
Discord then answers 404/10062 "Unknown interaction" to every response
attempt.

A dead token is an expected condition, not a crash: :func:`ack` reports it
instead of raising, and the caller skips the work (a replayed click must
never override a newer, deliberate choice).
"""

from __future__ import annotations

import datetime as dt
import logging

import discord

log = logging.getLogger(__name__)

#: Discord's documented interaction token lifetime, in seconds (15 minutes).
TOKEN_TTL = 900

#: Discord error code for an invalid or expired interaction token.
UNKNOWN_INTERACTION = 10062


def is_expired(interaction, *, now: dt.datetime | None = None) -> bool:
    """Whether the interaction token is past Discord's 15 minute window.

    ``interaction.created_at`` is the snowflake timestamp of the interaction,
    so the age is known without any HTTP call. An unknown age (test doubles,
    partial payloads) counts as fresh: the API answer is the final word.
    """
    created = getattr(interaction, "created_at", None)
    if not isinstance(created, dt.datetime):
        return False
    reference = now or dt.datetime.now(dt.UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.UTC)
    return (reference - created).total_seconds() > TOKEN_TTL


def is_dead_token(error: BaseException) -> bool:
    """Whether an HTTP error means "this interaction token is gone"."""
    return (
        isinstance(error, discord.HTTPException)
        and getattr(error, "code", 0) == UNKNOWN_INTERACTION
    )


def describe(interaction) -> str:
    """One line identifying the interaction, for the stale-token warning."""
    user = getattr(getattr(interaction, "user", None), "id", None)
    return (
        f"custom_id={getattr(interaction, 'custom_id', None)!r} "
        f"user={user} guild={getattr(interaction, 'guild_id', None)}"
    )


async def ack(interaction, *, ephemeral: bool = True) -> bool:
    """Acknowledge an interaction, tolerating a dead token.

    Returns True when the interaction is still usable (deferred here, or
    answered earlier) and False when the token is gone: already expired, or
    Discord answered 404/10062. The caller must then skip the work, because
    every followup would fail with the very same token.
    """
    if interaction.response.is_done():
        return True
    if is_expired(interaction):
        log.warning(
            "Stale interaction ignored (token expired after %ss): %s",
            TOKEN_TTL,
            describe(interaction),
        )
        return False
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except discord.HTTPException as exc:
        if not is_dead_token(exc):
            raise
        log.warning(
            "Stale interaction ignored (unknown interaction 10062): %s",
            describe(interaction),
        )
        return False
    return True

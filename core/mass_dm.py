"""Shared mass-DM fan-out for broadcast/reminder-style features.

Sends ``text`` to every non-bot member with a configurable pace delay,
counting successes and failures. ``Forbidden``/``HTTPException`` per member
never abort the run: the same ``suppress`` pattern every fan-out used
before, now in one place.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import discord


async def fan_out(
    members: Iterable[discord.Member], text: str, delay: float
) -> tuple[int, int]:
    """Send ``text`` to members; returns ``(sent, failed)``."""
    sent = failed = 0
    for member in list(members):
        if getattr(member, "bot", False):
            continue
        try:
            await member.send(text)
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(max(delay, 0))
    return sent, failed


def with_role(
    members: Iterable[discord.Member], role_id: int | None
) -> list[discord.Member]:
    """Filter ``members`` to those having ``role_id``; ``None``/``0`` keeps all."""
    if not role_id:
        return list(members)
    try:
        wanted = int(role_id)
    except (TypeError, ValueError):
        return list(members)
    kept = []
    for member in list(members):
        roles = getattr(member, "roles", None) or []
        if any(getattr(role, "id", None) == wanted for role in roles):
            kept.append(member)
    return kept

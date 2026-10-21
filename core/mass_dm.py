"""Shared mass-DM fan-out for broadcast/reminder-style features.

Sends ``text`` to every non-bot member with a configurable pace delay,
counting successes and failures. ``Forbidden``/``HTTPException`` per member
never abort the run: the same ``suppress`` pattern every fan-out used
before, now in one place. Broadcasts may attach downloaded files; a failed
file upload falls back to the original URLs so one oversized file never
silently costs the whole DM.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class DmAttachment:
    """Downloaded attachment bytes that can be uploaded to a DM."""

    filename: str
    data: bytes
    url: str
    spoiler: bool = False

    def file(self) -> discord.File:
        """Build a fresh single-use ``File`` for one recipient."""
        return discord.File(
            io.BytesIO(self.data), filename=self.filename, spoiler=self.spoiler
        )


def _with_links(text: str, attachments: Sequence[DmAttachment]) -> str:
    links = "\n".join(attachment.url for attachment in attachments if attachment.url)
    return f"{text}\n{links}".strip()


async def fan_out(
    members: Iterable[discord.Member],
    text: str,
    delay: float,
    attachments: Sequence[DmAttachment] = (),
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Send one payload to members; returns ``(sent, failed)``.

    ``on_progress`` receives the running ``(sent, failed)`` totals after each
    recipient, which lets a cancelled broadcast persist partial counters.
    """
    sent = failed = 0
    files = list(attachments)
    for member in list(members):
        if getattr(member, "bot", False):
            continue
        if files:
            try:
                await member.send(
                    content=text or None,
                    files=[attachment.file() for attachment in files],
                )
                sent += 1
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException:
                try:
                    await member.send(content=_with_links(text, files))
                    sent += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
        else:
            try:
                await member.send(content=text or None)
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        if on_progress is not None:
            on_progress(sent, failed)
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

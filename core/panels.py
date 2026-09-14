"""Repaint registry for persistent panels.

A "panel" is a long-lived posted message that reflects bot state (the ticket
panel, a future partnerships board...). When the active theme changes or a
relevant setting is edited, stale panels must be rebuilt in place -- but the
theme/setting layers cannot know which features post panels. This registry
inverts the dependency: each feature registers an async repaint callback and
the shared entry points (:func:`repaint_guild`, :func:`on_theme_changed`,
:func:`on_setting_changed`, :func:`repaint_all`) fan out to every registrant.

Contract for a repaint callback: ``async def(bot, guild_id) -> None``. It must
be idempotent and defensive -- a failure in one panel never blocks the others
(each call site already wraps the fan-out in per-panel error handling).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

RepaintFn = Callable[[Any, int], Awaitable[None]]

#: name -> (relevant setting keys, callback)
_REGISTRY: dict[str, tuple[frozenset[str], RepaintFn]] = {}


def register(name: str, callback: RepaintFn, *, setting_keys: tuple[str, ...] = ()) -> None:
    """Register one panel's repaint (idempotent by ``name`` -- re-registering
    replaces, keeping module import semantics friendly)."""
    _REGISTRY[name] = (frozenset(setting_keys), callback)


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def registered() -> list[str]:
    return sorted(_REGISTRY)


async def repaint_guild(bot, guild_id: int) -> None:
    """Repaint every registered panel in one guild."""
    for name, (_keys, callback) in _REGISTRY.items():
        try:
            await callback(bot, guild_id)
        except Exception as exc:
            log.warning("panel repaint %r failed in guild %s: %s", name, guild_id, exc)


async def on_theme_changed(bot, guild_id: int) -> None:
    """A guild's active theme changed (or its files changed): repaint panels
    and rebuild the persistent action-button dispatch views."""
    await repaint_guild(bot, guild_id)
    from rosemary.core.card_actions import sync_guild

    try:
        await sync_guild(bot, guild_id)
    except Exception as exc:
        log.warning("card action re-sync failed in guild %s: %s", guild_id, exc)


async def on_setting_changed(bot, guild_id: int, key: str) -> None:
    """A setting changed: repaint only panels that declared interest."""
    for name, (keys, callback) in _REGISTRY.items():
        if key not in keys:
            continue
        try:
            await callback(bot, guild_id)
        except Exception as exc:
            log.warning("panel repaint %r failed in guild %s: %s", name, guild_id, exc)


async def repaint_all(bot) -> None:
    """Boot path: repaint every panel in every guild the bot can see."""
    for guild in list(getattr(bot, "guilds", [])):
        await repaint_guild(bot, guild.id)

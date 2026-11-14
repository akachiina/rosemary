"""The ``/convert file`` picker: one theme card, one select per category.

The panel is ephemeral: only the member who ran the command sees it. Each
category (image, video, audio, document, sticker) gets its own select, and
each option carries the theme's emoji for the format. The result is an
ephemeral card with the converted file attached, so the picker and the file
stay private.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import discord

from rosemary.core.convert import ConvertError, convert
from rosemary.core.convert_registry import (
    category_label_key,
    group_by_category,
    options_for,
)
from rosemary.core.host import HostConfig
from rosemary.ui.kit import Panel

log = logging.getLogger(__name__)

#: The target values that are pictures (the result card previews them).
_PREVIEW_TARGETS = {"png", "jpg", "webp", "gif", "bmp", "tiff", "ico", "sticker"}


def human_size(size: int) -> str:
    """A short human-readable byte count (``1.2 MB``)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ConvertPanel(Panel):
    """The format picker of one attachment (ephemeral)."""

    def __init__(
        self,
        bot: Any,
        guild_id: int,
        data: bytes,
        filename: str,
        kind: str,
        host: HostConfig,
    ) -> None:
        super().__init__(bot, guild_id, timeout=180)
        self.data = data
        self.filename = filename
        self.kind = kind
        self.host = host
        self.register("convert:go", self._choose)

    async def open_screen(self) -> None:
        """Set the picker's copy (the panel is prepared after this)."""
        await self.set_screen(
            "convert.title",
            "convert.body",
            filename=self.filename,
            color="support",
        )
        self.nav("close")

    async def sections(self) -> list[discord.ui.ViewItem]:
        return []

    async def actions(self) -> list[discord.ui.ViewItem]:
        rows: list[discord.ui.ViewItem] = []
        for category, options in group_by_category(self.kind).items():
            choices = []
            for option in options:
                choices.append(
                    discord.SelectOption(
                        label=(await self.t(option.label_key))[:100],
                        value=option.value,
                        description=(await self.t(option.description_key))[:100],
                        emoji=await self._option_emoji(option),
                    )
                )
            select = self.make_select(
                custom_id=f"convert:go:{category}",
                placeholder=(await self.t(category_label_key(category)))[:150],
                options=choices,
            )
            rows.append(discord.ui.ActionRow(select))
        return rows

    async def _option_emoji(self, option) -> str | None:
        """The option emoji: the theme token, else the registry fallback."""
        value = self.emoji(option.icon)
        if value:
            return value
        return option.fallback or None

    async def _choose(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        data = interaction.data or {}
        target = (data.get("values") or [""])[0]
        if not target:
            return
        await self._run(interaction, target)

    async def _run(self, interaction: discord.Interaction, target: str) -> None:
        try:
            data, name = await asyncio.to_thread(
                convert,
                self.data,
                self.filename,
                target,
                max_mb=self.host.convert_max_mb,
                timeout=self.host.convert_timeout,
            )
        except ConvertError as exc:
            self.flash = await self.t(f"convert.error_{exc.code}")
            self.flash_color = "danger"
            await self.rerender(interaction)
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Convert failed in %s: %s", self.guild_id, exc)
            self.flash = await self.t("convert.error")
            self.flash_color = "danger"
            await self.rerender(interaction)
            return
        format_label = await self._format_label(target)
        preview = f"attachment://{name}" if target in _PREVIEW_TARGETS else ""
        await self._send_result(interaction, data, name, format_label, preview)
        await self.set_screen(
            "convert.done",
            "convert.done_body",
            filename=self.filename,
            format=format_label,
            color="success",
        )
        self.nav("close")
        await self.rerender(interaction)

    async def _format_label(self, target: str) -> str:
        for option in options_for(self.kind):
            if option.value == target:
                return await self.t(option.label_key)
        return target.upper()

    async def _send_result(
        self,
        interaction: discord.Interaction,
        data: bytes,
        name: str,
        format_label: str,
        preview: str,
    ) -> None:
        from rosemary.core.card_service import render_card_message, simple_payload

        variables = {
            "filename": self.filename,
            "format": format_label,
            "size": human_size(len(data)),
            "preview": preview,
        }
        payload, allowed = await render_card_message(
            self.bot, self.guild_id, "convert.result", variables
        )
        if payload is None:
            payload = simple_payload(
                self.bot,
                self.guild_id,
                "convert.result",
                title=await self.t("convert.result.title"),
                body=await self.t(
                    "convert.result.body",
                    filename=self.filename,
                    format=format_label,
                    size=human_size(len(data)),
                ),
                color="success",
            )
            allowed = discord.AllowedMentions.none()
        file = discord.File(io.BytesIO(data), filename=name)
        await interaction.followup.send(
            **payload.message_kwargs(),
            file=file,
            allowed_mentions=allowed,
            ephemeral=True,
        )


__all__ = ["ConvertPanel", "human_size"]

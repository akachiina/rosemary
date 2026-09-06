"""Shared confirmation dialog with a buttonless terminal state.

Several features need the same flow (moderation actions, warning cleanup):
ask a question with confirm/cancel buttons, run an async action, then show
the outcome. The outcome reuses the question content plus the result, minus
the button rows — so the terminal state survives the rebuild in
:meth:`rerender`. Never rely on ``disable_all_items()`` before a rebuild:
:meth:`prepare` clears and recreates every component, wiping the disabled
flags.

Ad-hoc confirms that already clear their items and show plain text (tickets,
invites reset, cleaner, updater) are equivalent and need no migration.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from rosemary.ui.containers import TextDisplay, designer_container
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)


class ConfirmView(MenuView):
    """Generic ask-confirm-cancel dialog with a buttonless terminal state.

    Subclasses (or callers) provide the question copy through
    :meth:`question_items` and the work through ``on_confirm``. The default
    layout is one ``[confirm, cancel]`` row; override :meth:`action_rows`
    for extra buttons (e.g. notify-vs-silent variants). ``failure_key`` and
    ``cancelled_key`` keep each feature's copy untouched.
    """

    def __init__(
        self,
        bot,
        *,
        guild_id: int,
        owner_id: int | None,
        on_confirm: Any,
        confirm_label: str = "",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
        cancel_label: str = "",
        failure_key: str = "confirm.failed",
        cancelled_key: str = "confirm.cancelled",
    ) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.on_confirm = on_confirm
        self.confirm_label = confirm_label
        self.confirm_style = confirm_style
        self.cancel_label = cancel_label
        self.failure_key = failure_key
        self.cancelled_key = cancelled_key
        self.question_color = "warning"
        self.result: str | None = None
        self.result_state: str | None = None
        self.register("confirm_yes", self._confirm)
        self.register("confirm_no", self._cancel)

    async def _t(self, key: str, **kwargs: Any) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def question_items(self) -> list[discord.ui.ViewItem]:
        """Question content shown before any decision. Must be overridden."""
        raise NotImplementedError

    async def action_rows(self) -> list[discord.ui.ViewItem]:
        """Button rows for the question state. Override for extra buttons."""
        confirm = self.confirm_label or await self._t("confirm.confirm")
        cancel = self.cancel_label or await self._t("confirm.cancel")
        return [
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="confirm_yes",
                    label=confirm,
                    style=self.confirm_style,
                ),
                self.make_button(
                    custom_id="confirm_no",
                    label=cancel,
                    style=discord.ButtonStyle.secondary,
                ),
            )
        ]

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.result is not None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            self.result = await self.on_confirm(interaction)
            self.result_state = "success"
        except Exception as exc:
            log.error("Confirmed action failed: %s", exc)
            self.result = await self._t(self.failure_key)
            self.result_state = "danger"
        await self.rerender(interaction)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if self.result is not None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        self.result = await self._t(self.cancelled_key)
        self.result_state = "warning"
        await self.rerender(interaction)
        self.stop()

    async def build_items(self) -> list[discord.ui.ViewItem]:
        if self.result is not None:
            return [
                designer_container(
                    self.bot.theme.color(self.result_state or "info"),
                    *await self.question_items(),
                    TextDisplay(self.result),
                )
            ]
        return [
            designer_container(
                self.bot.theme.color(self.question_color),
                *await self.question_items(),
            ),
            *await self.action_rows(),
        ]

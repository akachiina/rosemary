"""Reusable interactive menu built on Components V2 (DesignerView).

py-cord's ``@button``/``@select`` decorators are incompatible with
``DesignerView`` (rejected at class creation). Menus instead use an explicit
handler registry keyed by ``custom_id``: subclasses register async handlers and
assemble ActionRows with :meth:`MenuView.make_button` / :meth:`MenuView.make_select`.
Because the interaction store dispatches by ``(component_type, message_id,
custom_id)``, rebuilding the view with stable custom_ids keeps callbacks working
across re-renders; :meth:`MenuView.rerender` handles that automatically.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

import discord

from rosemary.core.settings import MENU_TIMEOUT, get_setting

log = logging.getLogger(__name__)

Handler = Callable[[discord.Interaction], Awaitable[None]]


class MenuView(discord.ui.DesignerView):
    """Base class for interactive, re-renderable V2 menus.

    Subclasses implement :meth:`build_items` (called on every prepare) and
    register handlers through :meth:`register`. Views re-render with
    :meth:`rerender`, which rebuilds the items and edits the message in place.

    ``disable_on_timeout=True`` makes py-cord disable every component and edit
    the message when the idle timeout elapses, so menus visibly expire instead
    of leaving dead buttons that fail with "the application did not respond".
    """

    def __init__(
        self,
        *,
        timeout: float | None = MENU_TIMEOUT,
        author_id: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout, disable_on_timeout=True)
        self.author_id = author_id
        self._handlers: dict[str, Handler] = {}
        self._prepared = False

    # -- handler registry ---------------------------------------------------

    def register(self, custom_id: str, handler: Handler) -> Handler:
        """Register an async handler for a component ``custom_id``.

        May be used as a decorator. The handler receives a single
        :class:`discord.Interaction`.
        """
        self._handlers[custom_id] = handler
        return handler

    def _find_handler(self, custom_id: str) -> Handler | None:
        """Resolve a handler for ``custom_id``.

        Prefers an exact match; otherwise the longest registered prefix, so a
        single handler can cover dynamic ids like ``boost_manage:{role_id}``
        (``register("boost_manage", ...)``). The handler reads the full id from
        ``interaction.custom_id`` itself.
        """
        if custom_id in self._handlers:
            return self._handlers[custom_id]
        matches = [key for key in self._handlers if custom_id.startswith(key)]
        if not matches:
            return None
        return self._handlers[max(matches, key=len)]

    # -- item factories -----------------------------------------------------

    def make_button(
        self,
        *,
        custom_id: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: str | None = None,
        disabled: bool = False,
    ) -> discord.ui.Button:
        """Build a Button bound to the registered handler for ``custom_id``."""
        button = discord.ui.Button(
            style=style,
            label=label,
            emoji=emoji,
            custom_id=custom_id,
            disabled=disabled,
        )
        handler = self._find_handler(custom_id)
        if handler is not None:
            button.callback = handler
        else:
            log.warning("menu button %r has no handler; clicks will not ACK", custom_id)
        return button

    def make_select(
        self,
        *,
        custom_id: str,
        placeholder: str,
        options: list[discord.SelectOption],
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ) -> discord.ui.Select:
        """Build a string Select bound to the registered handler for ``custom_id``.

        Discord rejects selects with anything other than 1-25 options; callers
        must paginate or truncate before building.
        """
        if not 1 <= len(options) <= 25:
            raise ValueError(f"select {custom_id!r} needs 1-25 options, got {len(options)}")
        select = discord.ui.Select(
            select_type=discord.ComponentType.string_select,
            custom_id=custom_id,
            placeholder=(placeholder or "Choose an option")[:150],
            options=options,
            min_values=min_values,
            max_values=max_values,
            disabled=disabled,
        )
        handler = self._find_handler(custom_id)
        if handler is not None:
            select.callback = handler
        else:
            log.warning("menu select %r has no handler; clicks will not ACK", custom_id)
        return select

    def add_row(self, *items: discord.ui.ViewItem) -> None:
        """Add items to the view inside a single ActionRow."""
        self.add_item(discord.ui.ActionRow(*items))

    def make_native_select(
        self,
        *,
        custom_id: str,
        select_type: discord.ComponentType,
        placeholder: str = "",
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ) -> discord.ui.Select:
        """Build a native (user/role/channel/mentionable) Select bound to a handler.

        Native selects render server members/roles/channels automatically, so no
        ``options`` are supplied. The handler receives the raw values (IDs) in
        ``interaction.data["values"]``.
        """
        select = discord.ui.Select(
            select_type=select_type,
            custom_id=custom_id,
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            disabled=disabled,
        )
        handler = self._find_handler(custom_id)
        if handler is not None:
            select.callback = handler
        return select

    def make_role_select(
        self,
        *,
        custom_id: str,
        placeholder: str = "",
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ) -> discord.ui.Select:
        """Build a native role Select bound to the registered handler."""
        return self.make_native_select(
            custom_id=custom_id,
            select_type=discord.ComponentType.role_select,
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            disabled=disabled,
        )

    def make_user_select(
        self,
        *,
        custom_id: str,
        placeholder: str = "",
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ) -> discord.ui.Select:
        """Build a native user Select bound to the registered handler."""
        return self.make_native_select(
            custom_id=custom_id,
            select_type=discord.ComponentType.user_select,
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            disabled=disabled,
        )

    # -- lifecycle ----------------------------------------------------------

    async def prepare(self) -> None:
        """Rebuild the view's children for the current state.

        Called once before the initial ``respond`` and again before every
        re-render. The idle timeout is refreshed from the guild's
        ``general.menu_timeout`` setting so it can be configured in /settings.
        """
        await self._apply_menu_timeout()
        self.clear_items()
        for item in await self.build_items():
            self.add_item(item)
        self._prepared = True

    async def _apply_menu_timeout(self) -> None:
        """Override ``self.timeout`` with the guild-configured value."""
        bot = getattr(self, "bot", None)
        guild_id = getattr(self, "guild_id", None)
        if bot is None or guild_id is None:
            return
        try:
            value = await get_setting(bot.storage, guild_id, "general.menu_timeout")
            self.timeout = float(value)
        except (TypeError, ValueError, KeyError, AttributeError):
            return

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """Return the items to display for the current state.

        Must return a list of V2 items (Container/Section/ActionRow/...).
        """
        raise NotImplementedError

    async def rerender(self, interaction: discord.Interaction) -> None:
        """Rebuild items and edit the attached message in place."""
        await self.prepare()
        try:
            await interaction.edit(view=self)
        except discord.NotFound:
            # The interaction token expired (>3s without ack, error 10062);
            # refresh the underlying message directly so the menu still updates.
            message = getattr(interaction, "message", None)
            if message is not None:
                with contextlib.suppress(
                    discord.NotFound, discord.Forbidden, discord.HTTPException
                ):
                    await message.edit(view=self)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("rerender edit failed for view %s", type(self).__name__)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the view owner may interact with the menu."""
        if self.author_id is None:
            return True
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        """Suppress 'Invalid Webhook Token' when the interaction token has expired.

        ``disable_on_timeout`` triggers ``message.edit`` in the base class, which
        fails with HTTP 401 once the original interaction token expires (30+ min).
        """
        with contextlib.suppress(discord.HTTPException):
            await super().on_timeout()

    async def on_check_failure(self, interaction: discord.Interaction) -> None:
        """Reply ephemeral when a non-owner tries to use the menu.

        Without this, py-cord leaves the interaction unacknowledged and the
        other user sees "the application did not respond".
        """
        if interaction.response.is_done():
            return
        bot = getattr(self, "bot", None)
        guild_id = getattr(self, "guild_id", None)
        if bot is None or guild_id is None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Only the menu owner can use these controls.", ephemeral=True
                )
            return
        message = await bot.translator.t(guild_id, "menu.owner_only")
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(message, ephemeral=True)

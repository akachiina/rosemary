"""The UI kit: the primitives every interactive surface shares.

* :class:`Panel`: a menu whose chrome (the header, the body, the footer and
  the navigation) comes from the theme; the panel owns only its content;
* :class:`Navigation`: the shared navigation row (back, close, previous,
  next);
* :class:`Notice`: the one-line notice (the plain-text layer);
* :class:`EmptyState`: the standard "nothing here" block;
* :class:`Confirm`: the standard confirmation dialog.

The chrome lives in the theme (``panels.chrome``): every panel inherits the
same header, footer and navigation, so restyling all of them is one edit
and no panel draws a frame by hand.

The lifecycle (the ACK, the re-render, the timeout, the dead token and the
error) belongs to :mod:`rosemary.ui.runtime`: every panel answers a click
the same way.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import discord

from rosemary.core.feedback import line as notice_line
from rosemary.core.settings import MENU_TIMEOUT
from rosemary.ui.containers import (
    ActionRow,
    Container,
    DesignerView,
    Section,
    Separator,
    TextDisplay,
    divider,
    items_text,
)
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

#: The V2 layout primitives, re-exported so a panel imports one place.
__all__ = [
    "ActionRow",
    "BUTTON_STYLES",
    "Chrome",
    "Confirm",
    "ConfirmResult",
    "Container",
    "DEFAULT_CHROME",
    "DesignerView",
    "EmptyState",
    "NavAction",
    "Navigation",
    "Notice",
    "Panel",
    "Section",
    "Separator",
    "TextDisplay",
    "divider",
    "items_text",
    "menu_heading_items",
    "read_chrome",
]

#: The button styles a panel may ask by name.
BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


async def menu_heading_items(bot: Any, guild_id: int, key: str) -> list[discord.ui.ViewItem]:
    """The themed heading items of a menu (empty = the code default).

    Only the theme's own override answers here: the packaged document of a
    menu heading is the frame the menu code already draws, so a guild that
    wrote nothing keeps the code heading. A container in the theme document
    is rejected (the menu already provides one, and nesting containers
    400s the payload).
    """
    from rosemary.core.cards import ECHO_VARIABLES, CardsError, is_embed_document, render_document
    from rosemary.core.themes import card_document

    doc = await card_document(bot, guild_id, key)
    if doc is None:
        return []
    if is_embed_document(doc):
        log.warning("theme heading %s is an embed; menu headings must be V2 text", key)
        return []
    try:
        view = render_document(
            bot,
            doc,
            dict(ECHO_VARIABLES),
            guild_id=guild_id,
            card_key=key,
        )
    except CardsError as exc:
        log.warning("theme heading %s is invalid, using default: %s", key, exc)
        return []
    items = list(view.children)
    if any(isinstance(item, Container) for item in items):
        log.warning("theme heading %s must not contain a container; using default", key)
        return []
    return items


@dataclass(frozen=True)
class NavAction:
    """One entry of the shared navigation row."""

    action: str
    label: str
    emoji: str = ""
    style: str = "secondary"


@dataclass(frozen=True)
class Chrome:
    """The chrome of the panels, read from the theme (``panels.chrome``)."""

    header: str = "# {title}"
    body: str = "{body}"
    footer: str = "-# {footer}"
    navigation: tuple[NavAction, ...] = ()


#: The chrome used when the theme does not define one.
DEFAULT_CHROME = Chrome(
    navigation=(
        NavAction("back", "{ui.back}", "back"),
        NavAction("previous", "{ui.previous}", "previous"),
        NavAction("next", "{ui.next}", "next"),
        NavAction("close", "{ui.close}", "door"),
    ),
)


def read_chrome(theme: Any) -> Chrome:
    """The chrome of a theme (the built-in one when it defines none)."""
    raw = (getattr(theme, "panels", {}) or {}).get("chrome")
    if not isinstance(raw, dict):
        return DEFAULT_CHROME
    navigation: list[NavAction] = []
    for entry in raw.get("navigation") or []:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip()
        if not action:
            continue
        navigation.append(
            NavAction(
                action=action,
                label=str(entry.get("label") or ""),
                emoji=str(entry.get("emoji") or ""),
                style=str(entry.get("style") or "secondary"),
            )
        )
    return Chrome(
        header=str(raw.get("header") or DEFAULT_CHROME.header),
        body=str(raw.get("body") or DEFAULT_CHROME.body),
        footer=str(raw.get("footer") or DEFAULT_CHROME.footer),
        navigation=tuple(navigation) or DEFAULT_CHROME.navigation,
    )


def _has_text(text: str) -> bool:
    """Whether a filled chrome template carries real text.

    An empty variable leaves the template marker behind ("# {title}" with no
    title renders "# "); that is not a line, it is the frame waiting for copy.
    """
    body = text.strip()
    return bool(body) and body not in {"#", "##", "###", "-#"}


class Navigation:
    """The shared navigation row: back, close, previous, next.

    The chrome names the actions (and their labels); the panel says which
    ones it shows and, when a screen wants a different word, overrides the
    label.
    """

    def __init__(self, panel: Panel, chrome: Chrome) -> None:
        self.panel = panel
        self.chrome = chrome

    async def row(self) -> ActionRow | None:
        """The navigation row of the panel (``None`` when it shows none)."""
        panel = self.panel
        entries = {entry.action: entry for entry in self.chrome.navigation}
        buttons: list[discord.ui.Button] = []
        for action in panel.nav_actions:
            entry = entries.get(action)
            if entry is None:
                continue
            label = panel.nav_labels.get(action, entry.label)
            # A label in braces is a catalog reference ("{ui.back}"); the
            # braces are the theme's mark, the key is what the catalog
            # carries.
            if label.startswith("{") and label.endswith("}"):
                label = label[1:-1]
            style = panel.nav_styles.get(entry.action, entry.style)
            buttons.append(
                await panel.button(
                    panel.nav_ids.get(entry.action, f"panel_{entry.action}"),
                    label,
                    emoji=entry.emoji,
                    style=style,
                )
            )
        buttons.extend(panel.nav_extra)
        if not buttons:
            return None
        return ActionRow(*buttons)


class Panel(MenuView):
    """A menu whose chrome comes from the theme.

    The panel owns the content: :meth:`sections` (the blocks of the screen)
    and :meth:`actions` (the rows of buttons). The frame (the header, the
    body, the footer) and the navigation are the theme's, so every panel
    looks the same and restyling them all is one edit.
    """

    #: The header of the panel.
    title: str = ""
    #: The body of the panel (the line under the header).
    body: str = ""
    #: The footer of the panel (the small line at the bottom).
    footer: str = ""
    #: The accent token of the panel.
    color: str = "brand"
    #: The navigation actions the panel shows (the chrome names them).
    nav_actions: tuple[str, ...] = ("back", "close")
    #: The label of each navigation action (a catalog key or a literal).
    nav_labels: dict[str, str] = {}
    #: The style of each navigation action.
    nav_styles: dict[str, str] = {}
    #: The custom_id of each navigation action.
    nav_ids: dict[str, str] = {}
    #: The card key of the themed heading (the theme may restyle it).
    heading_key: str = ""

    def __init__(
        self,
        bot: Any,
        guild_id: int,
        *,
        author_id: int | None = None,
        timeout: float | None = MENU_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout, author_id=author_id)
        self.bot = bot
        self.guild_id = guild_id
        # The navigation maps are per instance: a panel may override a label
        # for one of its screens without touching the others.
        self.nav_actions = tuple(self.nav_actions)
        self.nav_labels = dict(self.nav_labels)
        self.nav_styles = dict(self.nav_styles)
        self.nav_ids = dict(self.nav_ids)
        #: The labels of the class, restored by :meth:`nav`.
        self._nav_defaults = dict(self.nav_labels)
        #: The panel's own buttons, appended to the navigation row.
        self.nav_extra: list[discord.ui.Button] = []
        #: The extra lines of the frame, written by the current screen.
        self.lines: list[discord.ui.ViewItem] = []
        #: The accessory of the header (a thumbnail, an icon).
        self.accent: discord.ui.ViewItem | None = None
        self.flash: str | None = None
        self.flash_color: str | None = None
        self.expired = False
        self._register_navigation()

    #: the shared helpers --------------------------------------------------

    @property
    def theme(self) -> Any:
        """The theme of the bot (the look & feel)."""
        return getattr(self.bot, "theme", None)

    async def t(self, key: str, **variables: Any) -> str:
        """One catalog string, in the guild's language."""
        translator = getattr(self.bot, "translator", None)
        if translator is None:
            return key
        return await translator.t(self.guild_id, key, **variables)

    def emoji(self, token: str | None) -> str | None:
        """A theme icon by token (``None`` when the theme has none)."""
        if not token:
            return None
        theme = self.theme
        if theme is None:
            return None
        value = theme.icon(token)
        return value or None

    def entry(self, label: str, value: str) -> TextDisplay:
        """One ``label: value`` line (the theme's markdown template)."""
        return TextDisplay(self.md("entry", label=label, value=value))

    def md(self, name: str, **kwargs: Any) -> str:
        """A markdown template of the theme (the structural text)."""
        theme = self.theme
        if theme is None:
            return name
        try:
            return theme.md(name, **kwargs)
        except KeyError:
            return name

    async def set_screen(
        self,
        title: str | None,
        body: str | None = None,
        *,
        color: str = "brand",
        heading: str = "",
        **variables: Any,
    ) -> None:
        """Set the copy of the current screen (the frame reads it).

        The title and body are catalog keys; the variables feed both. The
        extra per-screen state (the lines, the accent, the extra navigation
        buttons) is reset here, so a screen never inherits the previous one's
        decorations.
        """
        self.heading_key = heading
        self.title = await self.t(title, **variables) if title else ""
        self.body = await self.t(body, **variables) if body else ""
        self.color = color
        self.lines = []
        self.accent = None
        self.nav_extra = []

    async def button(
        self,
        custom_id: str,
        label: str,
        *,
        emoji: str | None = None,
        style: str | discord.ButtonStyle = "secondary",
        disabled: bool = False,
        literal: bool = False,
        **variables: Any,
    ) -> discord.ui.Button:
        """A button whose label comes from the catalog.

        The label is a catalog key (``settings.close``); a key the catalog
        does not carry renders as itself, so a literal works too. Pass
        ``literal=True`` when the text is already final (a computed label
        that must not be looked up).
        """
        text = label if literal else await self.t(label, **variables)
        button_style = (
            style
            if isinstance(style, discord.ButtonStyle)
            else BUTTON_STYLES.get(style, style)
        )
        return self.make_button(
            custom_id=custom_id,
            label=text,
            style=button_style,
            emoji=self.emoji(emoji),
            disabled=disabled,
        )

    async def select(
        self,
        custom_id: str,
        placeholder: str,
        options: list[discord.SelectOption],
        *,
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ) -> discord.ui.Select:
        """A string select whose placeholder comes from the catalog."""
        return self.make_select(
            custom_id=custom_id,
            placeholder=await self.t(placeholder),
            options=options,
            min_values=min_values,
            max_values=max_values,
            disabled=disabled,
        )

    #: the chrome ------------------------------------------------------------

    def _fill(self, template: str, **variables: Any) -> str:
        """Fill a chrome template, leaving an unknown placeholder alone.

        A theme may write an icon token (``{settings}``) or an asset
        reference (``{emojis/shield}``) in the chrome; both resolve here, so
        a custom emoji works in the header, the body and the footer.
        """
        from rosemary.core.assets import resolve_refs

        theme = self.theme
        values = dict(getattr(theme, "emojis", {}) or {})
        values.update(variables)
        try:
            filled = template.format(**values)
        except (KeyError, IndexError, ValueError):
            return resolve_refs(template)
        return resolve_refs(filled)

    async def frame(self) -> list[discord.ui.ViewItem]:
        """The frame of the panel: the container with the chrome."""
        chrome = read_chrome(self.theme)
        parts: list[discord.ui.ViewItem] = []
        heading = await self.heading()
        if heading:
            # The theme wrote its own heading: it replaces the chrome's
            # header and body (the menu brings its own container).
            parts.extend(heading)
        else:
            header = self._fill(chrome.header, title=self.title)
            if _has_text(header):
                if self.accent is not None:
                    # The header carries an accessory (a role icon, an image).
                    parts.append(Section(TextDisplay(header), accessory=self.accent))
                else:
                    parts.append(TextDisplay(header))
            body = self._fill(chrome.body, body=self.body)
            if _has_text(body):
                parts.append(TextDisplay(body))
        parts.extend(await self.header_lines())
        color = self.color
        if self.flash:
            parts.append(TextDisplay(self.flash))
            color = self.flash_color or color
        footer = self._fill(chrome.footer, footer=self.footer)
        if _has_text(footer):
            parts.append(TextDisplay(footer))
        self.flash = None
        self.flash_color = None
        if not parts:
            # A row-only surface (a review card, a ticket panel) carries no
            # chrome of its own: the card it rides already owns the frame.
            # An empty container would be an invalid payload.
            return []
        theme = self.theme
        return [
            Container(
                *parts, color=theme.color(color) if theme else discord.Colour.blurple()
            )
        ]

    async def heading(self) -> list[discord.ui.ViewItem]:
        """The themed heading of the panel (empty = the chrome's header).

        A theme may restyle the heading of a panel (``settings.title``,
        ``boost.home``): its own document answers, and the chrome's header
        and body step aside.
        """
        if not self.heading_key:
            return []
        return await menu_heading_items(self.bot, self.guild_id, self.heading_key)

    async def header_lines(self) -> list[discord.ui.ViewItem]:
        """The extra lines inside the frame (under the body)."""
        return list(self.lines)

    async def sections(self) -> list[discord.ui.ViewItem]:
        """The content of the panel (the blocks of the screen)."""
        return []

    async def actions(self) -> list[discord.ui.ViewItem]:
        """The action rows of the panel (the buttons and the selects)."""
        return []

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """The items of the panel: the frame, the content and the actions."""
        if self.expired:
            return await self.expired_items()
        return await self.compose(await self.sections(), await self.actions())

    async def compose(
        self,
        sections: list[discord.ui.ViewItem] | None = None,
        actions: list[discord.ui.ViewItem] | None = None,
    ) -> list[discord.ui.ViewItem]:
        """The items of a screen: the frame, the content, the actions, the nav.

        The frame is the chrome (the theme's), the navigation is the theme's
        too: the panel only provides the content.
        """
        items: list[discord.ui.ViewItem] = list(await self.frame())
        items.extend(sections or [])
        items.extend(actions or [])
        nav = await Navigation(self, read_chrome(self.theme)).row()
        if nav is not None:
            items.append(nav)
        return items

    async def expired_items(self) -> list[discord.ui.ViewItem]:
        """The terminal state of an expired panel (no buttons left)."""
        text = await self.t("menu.expired")
        theme = self.theme
        return [
            Container(
                TextDisplay(text),
                color=theme.color("warning") if theme else discord.Colour.blurple(),
            )
        ]

    #: the navigation --------------------------------------------------------

    def nav(self, *actions: str, **labels: str) -> None:
        """The navigation of the current screen.

        The actions are the ones the chrome names (``back``, ``previous``,
        ``next``, ``close``); a label overrides the chrome's for this
        screen (``nav("back", back="boost.buttons.cancel")``).
        """
        self.nav_actions = actions
        self.nav_labels = {**self._nav_defaults, **labels}

    def _register_navigation(self) -> None:
        """Bind the navigation actions of the panel to their handlers."""
        for action, handler in (
            ("back", self.on_back),
            ("close", self.on_close),
            ("previous", self.on_previous),
            ("next", self.on_next),
        ):
            self.register(self.nav_ids.get(action, f"panel_{action}"), handler)

    async def on_back(self, interaction: discord.Interaction) -> None:
        """Go back (the default: nothing to go back to)."""
        await self.rerender(interaction)

    async def on_close(self, interaction: discord.Interaction) -> None:
        """Close the panel (the buttons go away)."""
        from rosemary.ui.runtime import close

        await close(self, interaction)

    async def on_previous(self, interaction: discord.Interaction) -> None:
        """Go to the previous page (the default: stay)."""
        await self.rerender(interaction)

    async def on_next(self, interaction: discord.Interaction) -> None:
        """Go to the next page (the default: stay)."""
        await self.rerender(interaction)

    async def on_timeout(self) -> None:
        """Expire the panel: a terminal state, no dead buttons."""
        self.expired = True
        with contextlib.suppress(Exception):
            await super().on_timeout()


class EmptyState:
    """The standard "nothing here" block.

    A panel with no entries says so in one line, with the theme's icon:
    the member never sees an empty frame wondering what happened.
    """

    def __init__(self, text: str, *, icon: str | None = None) -> None:
        self.text = text
        self.icon = icon

    def item(self) -> TextDisplay:
        """The block itself (one line)."""
        body = f"{self.icon} {self.text}" if self.icon else self.text
        return TextDisplay(body)


class Notice:
    """The one-line notice: the plain-text layer of an ephemeral answer.

    The prefix is a theme style, so a guild restyles every notice at once.
    """

    @staticmethod
    async def send(
        target: Any,
        bot: Any,
        guild_id: int | None,
        text: str,
        *,
        style: str = "info",
    ) -> None:
        """Answer with one notice line (ACKs when needed)."""
        from rosemary.core.feedback import respond

        await respond(target, bot, guild_id, text, style=style, ephemeral=True)

    @staticmethod
    def line(bot: Any, guild_id: int | None, text: str, *, style: str = "info") -> str:
        """The notice line, as text (the plain-text layer)."""
        return notice_line(bot, guild_id, text, style=style)


@dataclass(frozen=True)
class ConfirmResult:
    """One confirmed-action outcome: the text plus the container color token.

    ``on_confirm`` may return a plain string (rendered with the default
    ``success`` color) or a :class:`ConfirmResult` to steer the terminal
    state's color (e.g. ``warning`` for a cancelled-by-feature flow).
    """

    text: str
    state: str = "success"

    @classmethod
    def wrap(cls, value: Any, *, default_state: str = "success") -> ConfirmResult:
        if isinstance(value, ConfirmResult):
            return value
        return cls(text=str(value), state=default_state)


class Confirm(Panel):
    """The standard confirmation dialog: ask, act, show the outcome.

    A confirmation is a conversation, not a dashboard: the question travels
    as plain text (the message content, with the theme's notice prefix) and
    the choices as a classic button row. The terminal state replaces the
    content with the outcome and drops the buttons.
    """

    def __init__(
        self,
        bot: Any,
        *,
        guild_id: int,
        owner_id: int | None = None,
        on_confirm: Any = None,
        confirm_label: str = "",
        confirm_style: str = "danger",
        confirm_emoji: str = "check",
        cancel_label: str = "",
        cancel_emoji: str = "back",
        failure_key: str = "confirm.failed",
        cancelled_key: str = "confirm.cancelled",
    ) -> None:
        super().__init__(bot, guild_id, author_id=owner_id)
        self.on_confirm = on_confirm
        self.confirm_label = confirm_label
        self.confirm_style = confirm_style
        self.confirm_emoji = confirm_emoji
        self.cancel_label = cancel_label
        self.cancel_emoji = cancel_emoji
        self.failure_key = failure_key
        self.cancelled_key = cancelled_key
        self.question_color = "warning"
        self.result: str | None = None
        self.result_state: str | None = None
        self.nav_actions = ()
        self.register("confirm_yes", self._confirm)
        self.register("confirm_no", self._cancel)

    async def question_items(self) -> list[discord.ui.ViewItem]:
        """The question content, shown before any decision."""
        raise NotImplementedError

    async def question_text(self) -> str:
        """The question as plain text (the body of the message)."""
        return items_text(await self.question_items())

    async def action_rows(self) -> list[discord.ui.ViewItem]:
        """The button rows of the question state."""
        confirm = self.confirm_label or await self.t("confirm.confirm")
        cancel = self.cancel_label or await self.t("confirm.cancel")
        return [
            ActionRow(
                await self.button(
                    "confirm_yes",
                    confirm,
                    style=self.confirm_style,
                    emoji=self.confirm_emoji,
                    literal=True,
                ),
                await self.button(
                    "confirm_no",
                    cancel,
                    style="secondary",
                    emoji=self.cancel_emoji,
                    literal=True,
                ),
            )
        ]

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.result is not None:
            await self._ack(interaction, suppress_errors=True)
            return
        if not await self._ack(interaction):
            return
        try:
            outcome = ConfirmResult.wrap(await self.on_confirm(interaction))
            self.result = outcome.text
            self.result_state = outcome.state
        except Exception as exc:
            log.error("Confirmed action failed: %s", exc)
            self.result = await self.t(self.failure_key)
            self.result_state = "danger"
        await self.rerender(interaction)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if self.result is not None:
            await self._ack(interaction, suppress_errors=True)
            return
        if not await self._ack(interaction):
            return
        self.result = await self.t(self.cancelled_key)
        self.result_state = "warning"
        await self.rerender(interaction)
        self.stop()

    async def content(self) -> str:
        """The message content of the current state (one notice line)."""
        if self.result is not None:
            body = self.result
            style = self.result_state or "info"
        else:
            body = await self.question_text()
            style = self.question_color
        return notice_line(self.bot, self.guild_id, body, style=style)

    async def message_kwargs(self) -> dict[str, Any]:
        """The keyword arguments of the confirm message (content + view).

        The mention policy rides along: the question names the target
        ("Ban <@7>?"), and a plain-text message would ping them. The
        Components V2 container it replaced never notified.
        """
        return {
            "content": await self.content(),
            "view": self,
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    async def rerender_payload(self) -> dict[str, Any]:
        """The re-render edits the content too (the outcome replaces it)."""
        return {
            "content": await self.content(),
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    async def build_items(self) -> list[discord.ui.ViewItem]:
        if self.result is not None:
            # Terminal state: no buttons left, only the outcome in the text.
            return []
        return list(await self.action_rows())

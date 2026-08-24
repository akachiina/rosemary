"""Reusable V2 modal builders for Rosemary.

All modals are :class:`discord.ui.DesignerModal` instances wired up through an
instance ``callback``, so cogs never subclass ``Modal``. Builders here cover the
two patterns every feature needs: free-text input and file upload (Discord's
native modal file-upload component). ``Label`` wraps each field so no modal has
bare ``InputText`` children.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

SubmitHandler = Callable[[discord.Interaction, str], Awaitable[None]]
FileSubmitHandler = Callable[[discord.Interaction, list[discord.Attachment]], Awaitable[None]]


def make_text_modal(
    *,
    title: str,
    custom_id: str,
    label: str,
    placeholder: str = "",
    value: str = "",
    required: bool = True,
    max_length: int = 100,
    on_submit: SubmitHandler,
) -> discord.ui.DesignerModal:
    """Build a single-field text modal.

    Args:
        title: Modal title (Discord caps at 45 characters).
        custom_id: Stable identifier for the modal.
        label: Field label shown to the user.
        placeholder: Hint text inside the field.
        value: Pre-filled value (e.g. the current role name).
        required: Whether the field must be filled.
        max_length: Maximum input length.
        on_submit: ``async (interaction, value) -> None`` called on submit.
    """
    field = discord.ui.InputText(
        placeholder=placeholder,
        value=value,
        required=required,
        custom_id="value",
        min_length=1,
        max_length=max_length,
    )
    modal = discord.ui.DesignerModal(title=title, custom_id=custom_id)
    modal.add_item(discord.ui.Label(label, item=field))

    async def submit(interaction: discord.Interaction) -> None:
        await on_submit(interaction, field.value)

    modal.callback = submit
    return modal


def make_by_id_modal(
    *,
    title: str,
    custom_id: str,
    label: str,
    placeholder: str,
    on_submit: SubmitHandler,
) -> discord.ui.DesignerModal:
    """Build a modal asking for a snowflake ID (member/role).

    ``on_submit`` receives the raw string; callers parse and resolve it against
    the guild. Used as the "add by ID" fallback on every picker screen.
    """
    return make_text_modal(
        title=title,
        custom_id=custom_id,
        label=label,
        placeholder=placeholder,
        max_length=20,
        on_submit=on_submit,
    )


def make_file_modal(
    *,
    title: str,
    custom_id: str,
    label: str,
    description: str = "",
    required: bool = False,
    on_submit: FileSubmitHandler,
) -> discord.ui.DesignerModal:
    """Build a modal with a native file-upload field.

    Discord sends only attachment metadata in the interaction, so ``on_submit``
    receives the uploaded :class:`discord.Attachment` objects and callers must
    download the bytes from the CDN (``await attachment.read()``).
    """
    upload = discord.ui.FileUpload(custom_id="file", required=required)
    modal = discord.ui.DesignerModal(title=title, custom_id=custom_id)
    modal.add_item(discord.ui.Label(label, item=upload, description=description or None))

    async def submit(interaction: discord.Interaction) -> None:
        await on_submit(interaction, upload.values or [])

    modal.callback = submit
    return modal

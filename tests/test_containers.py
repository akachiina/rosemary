"""Tests for ``rosemary.ui.containers`` (Components V2 builders)."""

from __future__ import annotations

from pathlib import Path

import discord
import pytest

from rosemary.ui.containers import designer_container, divider, header_display


def test_designer_container_builds():
    container = designer_container(
        discord.Colour.brand_green(),
        header_display("About"),
        divider(),
    )
    assert isinstance(container, discord.ui.Container)


def test_header_display():
    display = header_display("hello", "world")
    assert isinstance(display, discord.ui.TextDisplay)
    assert "hello" in display.content
    assert "world" in display.content


def test_divider():
    assert isinstance(divider(), discord.ui.Separator)


def test_designer_container_rejects_non_view_item():
    with pytest.raises(TypeError):
        designer_container(discord.Colour.brand_green(), "plain string")


def test_no_embeds_anywhere():
    source = Path(__file__).resolve().parents[2] / "rosemary" / "ui" / "containers.py"
    assert "Embed" not in source.read_text(encoding="utf-8")

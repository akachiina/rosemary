"""Every interactive panel renders exactly what the golden fixture froze.

The card fixtures pin the payload the card pipeline sends. These pin the other
half of the UI: the screens the kit assembles in Python, with the themed chrome,
the copy they show and the controls they offer. ``scripts/snapshot_panels.py``
writes them; when a change is deliberate, re-run it and review the diff::

    ../.venv/bin/python scripts/snapshot_panels.py
"""

from __future__ import annotations

import json

from golden_panels import GOLDEN_DIR, PANEL_NAMES, differences, make_bot, render_all

FIXTURES = {path.stem: path for path in GOLDEN_DIR.glob("*.json")}


def test_every_panel_has_a_fixture():
    missing = sorted(set(PANEL_NAMES) - set(FIXTURES))
    assert missing == [], f"missing fixtures: {missing}"


def test_no_stale_fixture():
    stale = sorted(set(FIXTURES) - set(PANEL_NAMES))
    assert stale == [], f"fixtures of panels that no longer exist: {stale}"


async def test_panels_match_the_golden():
    snapshots = await render_all(make_bot())
    changed: list[str] = []
    details: list[str] = []
    for name, snapshot in snapshots.items():
        expected = json.loads(FIXTURES[name].read_text(encoding="utf-8"))
        if snapshot == expected:
            continue
        changed.append(name)
        details.append(f"  {name}: " + "; ".join(differences(expected, snapshot)))
    assert changed == [], (
        "the rendered panels changed (re-run scripts/snapshot_panels.py if the "
        "change is deliberate):\n" + "\n".join(details)
    )


async def test_panels_are_not_empty():
    """A panel without copy or controls is a broken screen, not a design."""
    for name, snapshot in (await render_all(make_bot())).items():
        assert snapshot["texts"], f"{name}: no text"
        assert snapshot["buttons"] or snapshot["selects"], f"{name}: no controls"

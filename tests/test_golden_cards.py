"""Every card renders exactly what the golden fixture froze.

The fixtures (``tests/golden/cards/<key>.json``) are written by
``scripts/snapshot_cards.py``. They pin the payload a member receives (plain
text, classic embed or Components V2 view), so the UI System v2 refactor can
change the pipeline without changing what people see.

When a change is deliberate, re-run the script and review the diff::

    ../.venv/bin/python scripts/snapshot_cards.py
"""

from __future__ import annotations

import json

from golden_cards import GOLDEN_DIR, differences, make_bot, render_card

from rosemary.core.cards import all_cards

FIXTURES = {path.stem: path for path in GOLDEN_DIR.glob("*.json")}


def test_every_card_has_a_fixture():
    missing = sorted(spec.key for spec in all_cards() if spec.key not in FIXTURES)
    assert missing == [], f"missing fixtures: {missing}"


def test_no_stale_fixture():
    keys = {spec.key for spec in all_cards()}
    stale = sorted(set(FIXTURES) - keys)
    assert stale == [], f"fixtures of cards that no longer exist: {stale}"


async def test_cards_match_the_golden():
    bot = make_bot()
    changed: list[str] = []
    details: list[str] = []
    for spec in all_cards():
        snapshot = await render_card(bot, spec.key)
        expected = json.loads(FIXTURES[spec.key].read_text(encoding="utf-8"))
        if snapshot == expected:
            continue
        changed.append(spec.key)
        details.append(f"  {spec.key}: " + "; ".join(differences(expected, snapshot)))
    assert changed == [], (
        "the rendered cards changed (re-run scripts/snapshot_cards.py if the "
        "change is deliberate):\n" + "\n".join(details)
    )

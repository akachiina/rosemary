"""The Components V2 dialect must not leak into an embed or a plain text.

``-#`` (small text) and ``#`` (heading) are Components V2 markers. Discord
renders them literally outside V2, so an embed or a text card carrying one
shows the raw marker to the member.

The known offenders are frozen below. The list is empty: the copy in the
catalogs is semantic now (no marker), and the marker is added by the renderer
that speaks the dialect (the V2 serializer). A NEW leak fails the test at
once, and a fixed one asks to be removed from the list.
"""

from __future__ import annotations

from golden_cards import make_bot, render_card

from rosemary.core.cards import all_cards

#: Cards whose embed or text still carries the ``-#`` marker.
KNOWN_MARKER_LEAKS: frozenset[str] = frozenset()

#: Cards whose embed title still carries the V2 heading marker.
KNOWN_HEADING_LEAKS: frozenset[str] = frozenset()


def _text_blob(snapshot: dict) -> str:
    """Every visible text of a rendered card, whatever its form."""
    parts: list[str] = []
    content = snapshot.get("content")
    if isinstance(content, str):
        parts.append(content)
    embed = snapshot.get("embed") or {}
    parts.append(str(embed.get("title") or ""))
    parts.append(str(embed.get("description") or ""))
    parts.append(str((embed.get("footer") or {}).get("text") or ""))
    parts.append(str((embed.get("author") or {}).get("name") or ""))
    for field in embed.get("fields") or []:
        parts.append(str(field.get("name") or ""))
        parts.append(str(field.get("value") or ""))
    return "\n".join(parts)


async def test_no_v2_marker_in_embed_or_text():
    bot = make_bot()
    leaks: list[str] = []
    headings: list[str] = []
    for spec in all_cards():
        snapshot = await render_card(bot, spec.key)
        if snapshot["form"] == "card":
            continue  # V2 is the dialect's home
        if "-#" in _text_blob(snapshot):
            leaks.append(spec.key)
        embed = snapshot.get("embed") or {}
        if str(embed.get("title") or "").startswith("# "):
            headings.append(spec.key)

    new = sorted(set(leaks) - set(KNOWN_MARKER_LEAKS))
    fixed = sorted(set(KNOWN_MARKER_LEAKS) - set(leaks))
    assert new == [], f"new V2 marker leaks: {new}"
    assert fixed == [], f"fixed leaks, remove them from KNOWN_MARKER_LEAKS: {fixed}"

    new_headings = sorted(set(headings) - set(KNOWN_HEADING_LEAKS))
    fixed_headings = sorted(set(KNOWN_HEADING_LEAKS) - set(headings))
    assert new_headings == [], f"new V2 heading leaks: {new_headings}"
    assert fixed_headings == [], (
        f"fixed heading leaks, remove them from KNOWN_HEADING_LEAKS: {fixed_headings}"
    )

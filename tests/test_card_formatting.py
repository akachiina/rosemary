"""Inline-code standard for machine values in every card (AGENTS.md, Cards).

Snowflake IDs, command usages and the text-command prefix render as inline
code (single backticks) in card text: machine values are monospace, while
mentions, timestamps and plain counters are not. The catalogs are swept
recursively, the footers the code builds by hand are source-scanned and the
packaged documents are swept by slot, so a new card cannot regress the
standard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from catalog import load_catalog

#: Single-brace placeholders, the shape live card text uses.
_PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

#: The feature code that still lays a card out by hand: its footers are
#: written by hand too, so they are swept like the packaged ones.
_CODE_DIRS = ("cogs", "core", "ui")

#: The default package: the documents carry the footers as semantic slots
#: (the serializer writes the ``-#`` marker), so the sweep reads the slot.
_PACKAGE_DIR = Path("themes/default")


def _is_machine_value(name: str) -> bool:
    """IDs and the prefix are monospace material; labels/counters are not."""
    return name.endswith("_id") or name == "prefix"


def _card_strings(node):
    """Yield every string in the catalog."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _card_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _card_strings(item)
    elif isinstance(node, str):
        yield node


def test_machine_values_are_inline_code_in_catalogs():
    """Every ``{..._id}`` and ``{prefix}`` sits inside a code span per line."""
    offenders = []
    for code in ("en-US", "pt-BR"):
        catalog = load_catalog(code)
        for text in _card_strings(catalog):
            for line in text.splitlines():
                for match in _PLACEHOLDER_RE.finditer(line):
                    name = match.group(1)
                    if not _is_machine_value(name):
                        continue
                    start, end = match.start(), match.end()
                    # Doubled braces are editor-hint syntax, not card text.
                    if line[start - 1 : start] == "{" and line[end : end + 1] == "}":
                        continue
                    # Inside a code span: an odd number of backticks precedes
                    # the placeholder on its own line.
                    if line[:start].count("`") % 2 == 0:
                        offenders.append(f"{code}: {{{name}}}")
    assert offenders == []


def test_code_built_id_footers_are_inline_code():
    """The footers the code still builds by hand are swept like the packaged
    ones: on every ``-# `` footer line, a machine value opens inside an
    inline code span (odd backtick count before it on the line).

    The info family lives in the default package now (its footers are
    semantic slots), so the packaged sweep below carries its guarantee.
    """
    offenders: list[str] = []
    for directory in _CODE_DIRS:
        for path in sorted(Path(directory).rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "-#" not in line:
                    continue
                for match in _PLACEHOLDER_RE.finditer(line):
                    name = match.group(1)
                    if not _is_machine_value(name):
                        continue
                    if line[: match.start()].count("`") % 2 != 1:
                        offenders.append(f"{path}:{number}: {{{name}}}")
    assert offenders == []


def _footer_slots(node):
    """Every footer slot of a document tree (the value may be a list)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "footer":
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            yield item
                continue
            yield from _footer_slots(value)
    elif isinstance(node, list):
        for item in node:
            yield from _footer_slots(item)


def test_packaged_id_footers_are_inline_code():
    """The packaged documents carry the footers as semantic slots.

    The serializer writes the ``-#`` marker, so the slot is the footer text
    as the member reads it: the machine value (the id) still opens inside an
    inline code span.
    """
    held = False
    for path in sorted(_PACKAGE_DIR.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for footer in _footer_slots(data):
            for line in footer.splitlines():
                for match in _PLACEHOLDER_RE.finditer(line):
                    name = match.group(1)
                    if not _is_machine_value(name):
                        continue
                    assert line[: match.start()].count("`") % 2 == 1, (
                        f"{path}: machine value outside inline code: {name}"
                    )
                    held = True
    assert held, "the package carries no inline-code ID footer"

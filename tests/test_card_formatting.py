"""Inline-code standard for machine values in every card (AGENTS.md, Cards).

Snowflake IDs, command usages and the text-command prefix render as inline
code (single backticks) in card text: machine values are monospace, while
mentions, timestamps and plain counters are not. The catalogs are swept
recursively and the code-built info-family ID footers are source-scanned,
so a new card cannot regress the standard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

#: Single-brace placeholders, the shape live card text uses.
_PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

#: Bare info-footer text (``-# {user_id}``) in the builder sources.
_BARE_FOOTER_RE = re.compile(r'"-# \{[a-z0-9_]+_id\}"')

#: Standard info footer: the ID placeholder inside an inline code span.
_CODE_FOOTER_RE = re.compile(r'"-# `\{[a-z0-9_]+_id\}`"')


def _is_machine_value(name: str) -> bool:
    """IDs and the prefix are monospace material; labels/counters are not."""
    return name.endswith("_id") or name == "prefix"


def _card_strings(node):
    """Yield every string in the catalog, skipping editor hint nodes."""
    if isinstance(node, dict):
        for key, value in node.items():
            # ``placeholders:`` hints document the contract with doubled braces
            # for the card editor; they are never rendered card text.
            if key == "placeholders":
                continue
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
        with open(f"language/{code}.yaml", encoding="utf-8") as fh:
            catalog = yaml.safe_load(fh) or {}
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


def test_builder_id_footers_are_inline_code():
    """The info-family ID footers are code-built text, so scan the sources."""
    for rel in ("cogs/info.py", "cogs/utility.py"):
        source = Path(rel).read_text(encoding="utf-8")
        assert not _BARE_FOOTER_RE.findall(source), f"{rel}: ID footer without code"
        assert _CODE_FOOTER_RE.search(source), f"{rel}: no backticked ID footer"

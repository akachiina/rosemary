"""Placeholder resolution: the catalog, the variables and the theme emojis.

A document (or a card) carries templates. This module resolves them in two
phases, so each one happens exactly once:

* :meth:`Resolver.copy` resolves the **catalog** references (``{a.b.c}``):
  the copy of the language file, with its own catalog references resolved
  too. The send-site variables stay literal here.
* :meth:`Resolver.text` resolves the **variables** (``{name}``) and the
  **mention fields** (``{@name}``), collecting the ids that may ping.

Unknown names stay literal: a broken template never breaks a send (the lint
reports it separately).

The asset references (``{emojis/name}``) are resolved first, in both phases:
they name a real Discord emoji, not a variable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from rosemary.core.assets import resolve_refs

log = logging.getLogger(__name__)

#: One placeholder, in one pass: a mention field, a catalog key or a variable.
TOKEN_RE = re.compile(
    r"\{(?:"
    r"(?P<mention>@[a-zA-Z_][a-zA-Z0-9_]*)"
    r"|(?P<dotted>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)"
    r"|(?P<plain>[a-zA-Z_][a-zA-Z0-9_]*)"
    r")\}"
)

#: A mention token in an already-resolved value (``<@id>`` / ``<@&id>``).
MENTION_TOKEN_RE = re.compile(r"<@!?([0-9]{1,20})>|<@&([0-9]{1,20})>")

#: How deep a catalog reference may nest (``{a.b}`` pointing at ``{c.d}``).
MAX_CATALOG_DEPTH = 4


class Resolver:
    """Resolves the templates of one card, for one guild.

    ``variables`` are the send-site values (the card's contract), ``emojis``
    the theme's emoji tokens (the fallback for a variable name) and ``scope``
    the current item of an ``each`` block (the ``{entry.reason}`` paths).
    """

    def __init__(
        self,
        bot,
        guild_id: int | None,
        variables: Mapping[str, Any] | None = None,
        *,
        emojis: Mapping[str, str] | None = None,
        scope: Mapping[str, Any] | None = None,
        depth: int = 0,
    ) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.variables = dict(variables or {})
        self.emojis = dict(emojis or {})
        self.scope = dict(scope or {})
        self.depth = depth
        #: The mentions collected while the templates were resolved.
        self.mentions: dict[str, tuple[str, int]] = {}

    # -- catalog ---------------------------------------------------------

    async def copy(self, template: str) -> str:
        """Resolve the catalog references of a template (the variables stay).

        The copy of a catalog key may reference another key: the resolution
        recurses (bounded by :data:`MAX_CATALOG_DEPTH`), so a cycle cannot
        hang a send.
        """
        if not template or "{" not in template:
            return template
        base = resolve_refs(template)
        out: list[str] = []
        cursor = 0
        for match in TOKEN_RE.finditer(base):
            name = match.group("dotted")
            if name is None:
                continue
            out.append(base[cursor : match.start()])
            cursor = match.end()
            value = await self._catalog(name)
            if value is None:
                value = self._scope_value(name)
            out.append(match.group(0) if value is None else value)
        out.append(base[cursor:])
        return "".join(out)

    async def _catalog(self, key: str) -> str | None:
        """The catalog copy of ``key`` (with its own references resolved)."""
        if self.depth >= MAX_CATALOG_DEPTH:
            log.warning("catalog reference too deep, keeping %r literal", key)
            return None
        raw = getattr(self.bot.translator, "raw", None)
        if not callable(raw):
            return None
        found = await raw(self.guild_id, key)
        if not isinstance(found, str) or not found.strip() or found == key:
            return None
        nested = Resolver(
            self.bot,
            self.guild_id,
            self.variables,
            emojis=self.emojis,
            scope=self.scope,
            depth=self.depth + 1,
        )
        text = await nested.copy(found)
        self.mentions.update(nested.mentions)
        return text

    async def catalog_copy(self, key: str) -> str | None:
        """The copy of a catalog key (``None`` when the catalog has none).

        The document uses it to name the copy of a card: a key the catalog
        does not have leaves the slot empty instead of shipping the raw key.
        """
        return await self._catalog(key)

    def _scope_value(self, name: str) -> str | None:
        """A path in the current ``each`` item (``{entry.reason}``)."""
        if not self.scope:
            return None
        head, _, tail = name.partition(".")
        if head not in self.scope or not tail:
            return None
        value: Any = self.scope[head]
        for part in tail.split("."):
            if isinstance(value, Mapping) and part in value:
                value = value[part]
            else:
                return None
        return str(value)

    # -- variables -------------------------------------------------------

    async def text(self, template: str) -> tuple[str, dict[str, tuple[str, int]]]:
        """Resolve the variables and the mention fields of a template.

        Returns the resolved text and the mentions it carries (the placeholder
        name -> ``("user" | "role", id)``). A mention field whose value holds
        no mention token resolves silently: nothing is collected.
        """
        if not template:
            return "", {}
        base = resolve_refs(template)
        out: list[str] = []
        cursor = 0
        for match in TOKEN_RE.finditer(base):
            name = match.group("plain")
            mention = match.group("mention")
            dotted = match.group("dotted")
            if dotted is not None:
                # The catalog phase already resolved it; an unknown key stays
                # literal.
                continue
            out.append(base[cursor : match.start()])
            cursor = match.end()
            if mention is not None:
                value = self.variables.get(mention[1:])
                if value is None:
                    out.append(match.group(0))
                    continue
                out.append(str(value))
                self._collect(mention[1:], value)
                continue
            assert name is not None
            value = self.variables.get(name)
            if value is None:
                value = self.emojis.get(name)
            if value is None:
                out.append(match.group(0))
                continue
            out.append(str(value))
        out.append(base[cursor:])
        return "".join(out), dict(self.mentions)

    def _collect(self, name: str, value: Any) -> None:
        """Record the id a mention field resolved to."""
        found = MENTION_TOKEN_RE.search(str(value))
        if found is None or name in self.mentions:
            return
        role_id, user_id = found.group(2), found.group(1)
        self.mentions[name] = ("role" if role_id else "user", int(role_id or user_id))

    def child(self, scope: Mapping[str, Any]) -> Resolver:
        """A resolver for an ``each`` item (the scope of the item)."""
        return Resolver(
            self.bot,
            self.guild_id,
            self.variables,
            emojis=self.emojis,
            scope=scope,
            depth=self.depth,
        )


__all__ = ["MAX_CATALOG_DEPTH", "MENTION_TOKEN_RE", "TOKEN_RE", "Resolver"]

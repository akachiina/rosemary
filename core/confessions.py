"""Confession persistence: per-guild counter, posts, replies and bans.

One JSON file per guild (``confessions.json``). Entries cover both review
messages (``status`` ``pending``) and public posts (``open``/``denied``):
approval creates the public entry from the pending one. Replies live in
their own map keyed by reply message id, with the parent link that renders
the ``replies to`` line.

The author is sealed: with ``confessions.sealed_author`` the entries carry
``author_seal`` (the author id encrypted to the staff's public key) instead
of ``author_id`` and the claim hash instead of a user-linked alias. The
bot cannot open the seal (the private half is offline), so what is stored
does not reveal who wrote the confession. The legacy mode (the setting off)
keeps the plain ``author_id`` and the user-keyed ``aliases`` map.

The ban list is the same idea: when the seal is on, the stored value is
``HMAC(pepper, user_id)`` (the pepper lives in its own file), so the list
does not carry the ids. The claim token is the member's proof of
authorship: the store keeps only ``SHA-256(token)``, so the stateless
``/confissao responder <token>`` can mark the OP without linking the reply
to an account.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.alias_identity import ban_hash
from rosemary.core.storage import GuildStorage


class ConfessionStore:
    """CRUD for confession posts, anonymous replies and author bans."""

    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir
        self._storage = GuildStorage(
            data_dir, filename="confessions.json", use_defaults=False
        )

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        data = await self._storage.get(guild_id)
        if not isinstance(data.get("counter"), int):
            data["counter"] = 0
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        if not isinstance(data.get("replies"), dict):
            data["replies"] = {}
        if not isinstance(data.get("banned"), list):
            data["banned"] = []
        if not isinstance(data.get("aliases"), dict):
            data["aliases"] = {}
        return data

    async def next_number(self, guild_id: int) -> int:
        """Increment the per-guild counter and return the new number."""
        data = await self._doc(guild_id)
        data["counter"] = int(data.get("counter") or 0) + 1
        await self._storage.set_all(guild_id, data)
        return data["counter"]

    async def assign_alias(
        self, guild_id: int, number: int, user_id: int, seed: str
    ) -> str:
        """The alias of a user inside one confession thread (legacy mode).

        Get-or-create: the stored alias wins, so a participant keeps the
        same pseudonymous name for the whole thread. The mapping is keyed by
        the confession number, not by the message id, so it survives the
        review-to-public move. The sealed mode never calls this: the alias is
        stored on the entry and the claim token replaces the user link.
        """
        data = await self._doc(guild_id)
        aliases = data["aliases"]
        per_confession = aliases.get(str(number))
        if not isinstance(per_confession, dict):
            per_confession = {}
            aliases[str(number)] = per_confession
        existing = per_confession.get(str(user_id))
        if isinstance(existing, str) and existing:
            return existing
        per_confession[str(user_id)] = seed
        await self._storage.set_all(guild_id, data)
        return seed

    async def claim(
        self, guild_id: int, token: str
    ) -> dict[str, Any] | None:
        """The entry a claim token opens (the hash is the only stored form)."""
        from rosemary.core.alias_identity import claim_hash

        wanted = claim_hash(token)
        data = await self._doc(guild_id)
        for entry in data["entries"].values():
            if isinstance(entry, dict) and entry.get("claim_hash") == wanted:
                return dict(entry)
        for reply in data["replies"].values():
            if isinstance(reply, dict) and reply.get("claim_hash") == wanted:
                return dict(reply)
        return None

    async def put(
        self,
        guild_id: int,
        message_id: int,
        *,
        number: int,
        author_id: int = 0,
        channel_id: int,
        body: str = "",
        user_name: str = "",
        user_avatar: str = "",
        image_url: str = "",
        color_hex: str = "",
        status: str = "open",
        thread_id: int = 0,
        alias: str = "",
        alias_color: str = "",
        author_seal: str = "",
        claim_hash: str = "",
    ) -> None:
        """Record a review or public confession message.

        With ``author_seal`` the entry carries the sealed id (and the claim
        hash) instead of the plain ``author_id``: the caller has already
        encrypted it (the store never sees the private half). ``alias`` is
        the pseudonymous name (a fruit when the identity module is on), not
        a seed; ``alias_color`` keeps the legacy per-participant color.
        """
        if status not in ("pending", "open", "denied"):
            raise ValueError(f"unknown status {status!r}")
        data = await self._doc(guild_id)
        entry = {
            "number": number,
            # With the seal on the id is stored encrypted, never readable:
            # the caller passes the plain id, the store seals it.
            "author_id": int(author_id or 0),
            "channel_id": channel_id,
            "body": body,
            "user_name": user_name,
            "user_avatar": user_avatar,
            "image_url": image_url,
            "color_hex": color_hex,
            "status": status,
            "thread_id": thread_id,
            "reply_count": 0,
            "alias": alias,
            "alias_color": alias_color,
        }
        if author_seal:
            entry["author_seal"] = author_seal
        else:
            entry.pop("author_seal", None)
        if claim_hash:
            entry["claim_hash"] = claim_hash
        else:
            entry.pop("claim_hash", None)
        if author_seal:
            # The readable id never reaches the disk: the seal is the only
            # copy (the private half is offline, so nobody can open it).
            entry.pop("author_id", None)
        data["entries"][str(message_id)] = entry
        await self._storage.set_all(guild_id, data)

    async def rekey(
        self, guild_id: int, old_message_id: int, new_message_id: int
    ) -> bool:
        """Move an entry (or reply) to its real message id after posting.

        Review entries are staged under a placeholder key because the review
        message id only exists after the send.
        """
        data = await self._doc(guild_id)
        for section in ("entries", "replies"):
            if str(old_message_id) in data[section]:
                data[section][str(new_message_id)] = data[section].pop(
                    str(old_message_id)
                )
                await self._storage.set_all(guild_id, data)
                return True
        return False

    async def get(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        """Return one entry, or ``None`` when unknown."""
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(message_id))
        return dict(entry) if isinstance(entry, dict) else None

    async def get_by_number(
        self, guild_id: int, number: int
    ) -> tuple[int, dict[str, Any]] | None:
        """Find the public entry by confession number."""
        data = await self._doc(guild_id)
        for message_id, entry in data["entries"].items():
            if (
                isinstance(entry, dict)
                and entry.get("number") == number
                and entry.get("status") == "open"
            ):
                return int(message_id), dict(entry)
        return None

    async def set_status(
        self, guild_id: int, message_id: int, status: str
    ) -> dict[str, Any] | None:
        """Mark a review entry decided (``denied`` keeps it for the record)."""
        if status not in ("pending", "open", "denied"):
            raise ValueError(f"unknown status {status!r}")
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(message_id))
        if not isinstance(entry, dict):
            return None
        entry["status"] = status
        await self._storage.set_all(guild_id, data)
        return dict(entry)

    async def set_thread(
        self, guild_id: int, message_id: int, thread_id: int
    ) -> dict[str, Any] | None:
        """Record the lazily created discussion thread of a confession."""
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(message_id))
        if not isinstance(entry, dict):
            return None
        entry["thread_id"] = thread_id
        await self._storage.set_all(guild_id, data)
        return dict(entry)

    async def add_reply(
        self,
        guild_id: int,
        confession_message_id: int,
        reply_message_id: int,
        *,
        parent_id: int | None,
        parent_index: int,
        author_id: int = 0,
        body: str,
        image_url: str = "",
        alias: str = "",
        alias_color: str = "",
        is_op: bool = False,
        parent_is_op: bool = False,
        author_seal: str = "",
        claim_hash: str = "",
    ) -> tuple[int, dict[str, Any]] | None:
        """Record an anonymous reply; returns ``(index, reply)``.

        The index is the per-confession sequence feeding the ``#N-M``
        numbering; ``parent_index`` is 0 for direct replies. ``is_op`` marks
        a reply written by the confession's own author (resolved by the
        claim token in the sealed mode, never by a stored user id).
        ``author_seal`` carries the sealed author id, when the guild seals.
        """
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(confession_message_id))
        if not isinstance(entry, dict):
            return None
        entry["reply_count"] = int(entry.get("reply_count") or 0) + 1
        index = entry["reply_count"]
        reply = {
            "number": entry.get("number", 0),
            "confession_id": confession_message_id,
            "index": index,
            "parent_id": parent_id or 0,
            "parent_index": parent_index,
            "author_id": int(author_id or 0),
            "body": body,
            "image_url": image_url,
            "alias": alias,
            "alias_color": alias_color,
            "is_op": bool(is_op),
            "parent_is_op": bool(parent_is_op),
        }
        if author_seal:
            reply["author_seal"] = author_seal
        else:
            reply.pop("author_seal", None)
        if claim_hash:
            reply["claim_hash"] = claim_hash
        else:
            reply.pop("claim_hash", None)
        if author_seal:
            reply.pop("author_id", None)
        data["replies"][str(reply_message_id)] = reply
        await self._storage.set_all(guild_id, data)
        return index, dict(reply)

    async def get_reply(
        self, guild_id: int, message_id: int
    ) -> dict[str, Any] | None:
        """Return one reply entry, or ``None`` when unknown."""
        data = await self._doc(guild_id)
        reply = data["replies"].get(str(message_id))
        return dict(reply) if isinstance(reply, dict) else None

    async def discard(self, guild_id: int, message_id: int) -> None:
        """Drop a single key from entries and replies (no cascade).

        Used for review cleanups (the pending and public entries share the
        confession number, so the cascading :meth:`remove` would drop both)
        and for single reply deletes.
        """
        data = await self._doc(guild_id)
        data["entries"].pop(str(message_id), None)
        data["replies"].pop(str(message_id), None)
        await self._storage.set_all(guild_id, data)

    async def remove(self, guild_id: int, message_id: int) -> None:
        """Drop a public post plus every reply hanging off its number."""
        data = await self._doc(guild_id)
        key = str(message_id)
        entry = data["entries"].pop(key, None)
        numbers = set()
        if isinstance(entry, dict):
            numbers.add(entry.get("number"))
        if numbers:
            data["replies"] = {
                rid: item
                for rid, item in data["replies"].items()
                if not isinstance(item, dict) or item.get("number") not in numbers
            }
            data["entries"] = {
                mid: item
                for mid, item in data["entries"].items()
                if not isinstance(item, dict) or item.get("number") not in numbers
            }
        await self._storage.set_all(guild_id, data)

    async def ban(self, guild_id: int, user_id: int, *, sealed: bool = False) -> bool:
        """Ban an author from confessing. Returns ``False`` when already banned.

        With ``sealed`` the stored value is the HMAC of the id (the pepper
        lives in its own file), so the list never carries the ids; the
        legacy mode keeps the plain ids for compatibility.
        """
        if user_id <= 0:
            return False
        data = await self._doc(guild_id)
        if sealed:
            stored = ban_hash(self.data_dir, guild_id, user_id)
            banned = [str(v) for v in data["banned"] if str(v)]
            if stored in banned:
                return False
            banned.append(stored)
            data["banned"] = banned
            await self._storage.set_all(guild_id, data)
            return True
        banned = [int(v) for v in data["banned"] if int(v) > 0]
        if user_id in banned:
            return False
        banned.append(user_id)
        data["banned"] = banned
        await self._storage.set_all(guild_id, data)
        return True

    async def unban(self, guild_id: int, user_id: int, *, sealed: bool = False) -> bool:
        """Lift a confession ban. Returns ``False`` when not banned."""
        data = await self._doc(guild_id)
        if sealed:
            stored = ban_hash(self.data_dir, guild_id, user_id)
            banned = [str(v) for v in data["banned"] if str(v)]
            if stored not in banned:
                return False
            banned.remove(stored)
            data["banned"] = banned
            await self._storage.set_all(guild_id, data)
            return True
        banned = [int(v) for v in data["banned"] if int(v) > 0]
        if user_id not in banned:
            return False
        banned.remove(user_id)
        data["banned"] = banned
        await self._storage.set_all(guild_id, data)
        return True

    async def is_banned(self, guild_id: int, user_id: int, *, sealed: bool = False) -> bool:
        """Whether an author is currently banned from confessing.

        The check reads the stored shape, not the caller's mode: a guild
        whose list was written in the legacy shape (plain ids) still bans,
        and a guild whose list was written with the HMAC (hashes) checks the
        hash. That way a setting change or an old list never disables a ban.
        """
        data = await self._doc(guild_id)
        raw = [str(v) for v in data["banned"] if str(v)]
        if any(not value.isdigit() for value in raw):
            return ban_hash(self.data_dir, guild_id, user_id) in set(raw)
        if sealed:
            # The list has ids but the guild wants hashes: both shapes must
            # answer, or a legacy id would stop banning after the toggle.
            return user_id in {int(value) for value in raw} or ban_hash(
                self.data_dir, guild_id, user_id
            ) in set(raw)
        return user_id in {int(value) for value in raw}

    async def banned_list(self, guild_id: int) -> list[int]:
        """Every banned author id (staff listing).

        The legacy mode keeps the plain ids; the sealed mode stores hashes
        (the pepper is the only link back, and it is not in the store), so
        the listing shows the stored values without pretending they are ids.
        """
        data = await self._doc(guild_id)
        values = sorted({int(v) for v in data["banned"] if int(v) > 0})
        if values:
            return values
        return [str(v) for v in data["banned"] if str(v)]

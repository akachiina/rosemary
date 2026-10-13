"""Confession persistence: per-guild counter, posts, replies and bans.

One JSON file per guild (``confessions.json``). Entries cover both review
messages (``status`` ``pending``) and public posts (``open``/``denied``):
approval creates the public entry from the pending one. Replies live in
their own map keyed by reply message id, with the parent link that renders
the ``replies to`` line. Author ids stay in this file only: the public
cards never carry them.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.storage import GuildStorage


class ConfessionStore:
    """CRUD for confession posts, anonymous replies and author bans."""

    def __init__(self, data_dir) -> None:
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
        return data

    async def next_number(self, guild_id: int) -> int:
        """Increment the per-guild counter and return the new number."""
        data = await self._doc(guild_id)
        data["counter"] = int(data.get("counter") or 0) + 1
        await self._storage.set_all(guild_id, data)
        return data["counter"]

    async def put(
        self,
        guild_id: int,
        message_id: int,
        *,
        number: int,
        author_id: int,
        channel_id: int,
        body: str = "",
        user_name: str = "",
        user_avatar: str = "",
        image_url: str = "",
        color_hex: str = "",
        status: str = "open",
        thread_id: int = 0,
    ) -> None:
        """Record a review or public confession message."""
        if status not in ("pending", "open", "denied"):
            raise ValueError(f"unknown status {status!r}")
        data = await self._doc(guild_id)
        data["entries"][str(message_id)] = {
            "number": number,
            "author_id": author_id,
            "channel_id": channel_id,
            "body": body,
            "user_name": user_name,
            "user_avatar": user_avatar,
            "image_url": image_url,
            "color_hex": color_hex,
            "status": status,
            "thread_id": thread_id,
            "reply_count": 0,
        }
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
        author_id: int,
        body: str,
        image_url: str = "",
    ) -> tuple[int, dict[str, Any]] | None:
        """Record an anonymous reply; returns ``(index, reply)``.

        The index is the per-confession sequence feeding the ``#N-M``
        numbering; ``parent_index`` is 0 for direct replies.
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
            "author_id": author_id,
            "body": body,
            "image_url": image_url,
        }
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

    async def ban(self, guild_id: int, user_id: int) -> bool:
        """Ban an author from confessing. Returns ``False`` when already banned."""
        if user_id <= 0:
            return False
        data = await self._doc(guild_id)
        banned = [int(v) for v in data["banned"] if int(v) > 0]
        if user_id in banned:
            return False
        banned.append(user_id)
        data["banned"] = banned
        await self._storage.set_all(guild_id, data)
        return True

    async def unban(self, guild_id: int, user_id: int) -> bool:
        """Lift a confession ban. Returns ``False`` when not banned."""
        data = await self._doc(guild_id)
        banned = [int(v) for v in data["banned"] if int(v) > 0]
        if user_id not in banned:
            return False
        banned.remove(user_id)
        data["banned"] = banned
        await self._storage.set_all(guild_id, data)
        return True

    async def is_banned(self, guild_id: int, user_id: int) -> bool:
        """Whether an author is currently banned from confessing."""
        data = await self._doc(guild_id)
        return user_id in {int(v) for v in data["banned"] if int(v) > 0}

    async def banned_list(self, guild_id: int) -> list[int]:
        """Every banned author id (staff listing)."""
        data = await self._doc(guild_id)
        return sorted({int(v) for v in data["banned"] if int(v) > 0})

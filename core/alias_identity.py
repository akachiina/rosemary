"""Sealed confessions: the author id is encrypted, never stored readable.

The bot holds only the public half of a per-guild X25519 key pair: a
:class:`~nacl.public.SealedBox` encrypts the author id (and the reply's
author) with the staff's public key, so the bot itself cannot open what it
stored. The private half lives offline (or split in Shamir shares) and is
used by the ``confession_keys`` CLI only for a serious case (a threat, a
doxxing), not to browse confessions.

Every sealed blob carries the id of the key that sealed it
(``key_id:base64``): keys rotate (a new public key each period, the old
private destroyed after the moderation window), so a leaked or seized key
exposes only the confessions of its window.

The claim token is the member's proof of authorship: 128 random bits shown
once at send time; the store keeps only its SHA-256, so the member can
answer a confession (the OP stateless path) without the bot ever linking
the reply to an account. The token is not reversible: the hash is the only
thing stored.

The pepper protects the ban list: a random secret kept in its own file
(never in the settings, never in a card), so the stored list is
``HMAC(pepper, user_id)`` and the members who were banned can be checked
without the bot keeping the ids. It does not stop a host that has the
pepper file (the threat model of the whole feature: what is stored does
not reveal the author; the host can still read the process at send time).

The pseudonymous alias and its accent live at the bottom of this module:
the alias is a fruit drawn from the catalog list (one list per language,
so the copy stays translatable) and the accent is a random pastel (random
hue, fixed saturation/lightness ranges, customizable per call). Both are
stored per participant (or per entry in the sealed mode), so a member
keeps the same name and color for the thread.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

#: The key ring file of a guild (public halves and metadata only).
_KEYS_FILE = "confession_keys.json"

#: The default moderation window of a key (seconds): after it the private
#: half is destroyed and the sealed blobs become unreadable by construction.
DEFAULT_MODERATION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Key:
    """One public half of the confession key pair (the bot cannot open it)."""

    key_id: str
    public: str
    created_at: float
    retire_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "public": self.public,
            "created_at": self.created_at,
            "retire_at": self.retire_at,
        }


def new_key_id() -> str:
    """A short, readable id for one key period."""
    return secrets.token_hex(3)


def generate_keypair() -> tuple[str, str]:
    """A fresh ``(public_b64, private_b64)`` X25519 pair.

    The public half goes to the guild key ring; the private half is printed
    for the staff to store offline (or split with ``shamir-mnemonic``). It
    never reaches the bot's data directory.
    """
    private = PrivateKey.generate()
    return (
        base64.b64encode(private.public_key.encode()).decode(),
        base64.b64encode(private.encode()).decode(),
    )


def fingerprint(public_b64: str) -> str:
    """The readable fingerprint of a public key (the card's proof)."""
    digest = hashlib.sha256(public_b64.encode("utf-8")).hexdigest()
    return digest[:16]


def seal(public_b64: str, key_id: str, value: str) -> str:
    """Encrypt ``value`` for the staff's public key (``key_id:base64``)."""
    box = SealedBox(PublicKey(base64.b64decode(public_b64)))
    blob = box.encrypt(value.encode("utf-8"))
    return f"{key_id}:{base64.b64encode(blob).decode()}"


def open_seal(private_b64: str, sealed: str) -> str:
    """Decrypt a sealed value with the staff's private key (the CLI)."""
    key_id, _, blob = sealed.partition(":")
    if not key_id or not blob:
        raise ValueError("the sealed value has no key id")
    box = SealedBox(PrivateKey(base64.b64decode(private_b64)))
    try:
        return box.decrypt(base64.b64decode(blob)).decode("utf-8")
    except CryptoError as exc:
        raise ValueError("the sealed value does not match this key") from exc


def claim_token() -> str:
    """A fresh claim token: 128 random bits, base32 (no ambiguous glyphs)."""
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")


def claim_hash(token: str) -> str:
    """The stored form of a claim token (the token itself is never stored)."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class KeyRing:
    """The per-guild public keys and their rotation window."""

    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(data_dir, filename=_KEYS_FILE, use_defaults=False)

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        data = await self._storage.get(guild_id)
        keys = data.get("keys")
        return {"keys": keys if isinstance(keys, list) else []}

    async def keys(self, guild_id: int) -> list[Key]:
        """Every public key of the guild (the active one first)."""
        data = await self._doc(guild_id)
        parsed: list[Key] = []
        for raw in data["keys"]:
            if not isinstance(raw, dict):
                continue
            try:
                parsed.append(
                    Key(
                        key_id=str(raw.get("key_id") or ""),
                        public=str(raw.get("public") or ""),
                        created_at=float(raw.get("created_at") or 0.0),
                        retire_at=float(raw.get("retire_at") or 0.0),
                    )
                )
            except (TypeError, ValueError):
                continue
        return parsed

    async def active(self, guild_id: int) -> Key | None:
        """The key new confessions are sealed with (the newest active one)."""
        now = time.time()
        active = [
            key
            for key in await self.keys(guild_id)
            if key.public and (key.retire_at <= 0 or key.retire_at > now)
        ]
        return max(active, key=lambda key: key.created_at, default=None)

    async def add(
        self, guild_id: int, public_b64: str, *, moderation_seconds: int
    ) -> Key:
        """Add a public key (the active one), rotating the previous one."""
        now = time.time()
        key = Key(
            key_id=new_key_id(),
            public=public_b64,
            created_at=now,
            retire_at=now + max(int(moderation_seconds), 0),
        )
        data = await self._doc(guild_id)
        # The previous keys get the same window: the private half is kept
        # for moderation for ``moderation_seconds`` after the new one.
        data["keys"] = [
            {
                **entry,
                "retire_at": entry.get("retire_at")
                or now + max(int(moderation_seconds), 0),
            }
            for entry in data["keys"]
        ]
        data["keys"].append(key.as_dict())
        await self._storage.set_all(guild_id, data)
        return key

    async def prune(self, guild_id: int) -> list[str]:
        """Drop the keys whose window closed; return their ids."""
        now = time.time()
        data = await self._doc(guild_id)
        kept: list[dict[str, Any]] = []
        dropped: list[str] = []
        for entry in data["keys"]:
            retire_at = float(entry.get("retire_at") or 0.0) if isinstance(entry, dict) else 0.0
            if isinstance(entry, dict) and retire_at and retire_at <= now:
                dropped.append(str(entry.get("key_id") or ""))
                continue
            kept.append(entry)
        if dropped:
            data["keys"] = kept
            await self._storage.set_all(guild_id, data)
        return [key_id for key_id in dropped if key_id]


def _pepper_path(data_dir, guild_id: int) -> Path:
    return Path(data_dir) / str(guild_id) / "confession_pepper.key"


def pepper(data_dir, guild_id: int) -> bytes:
    """The ban-list pepper (generated on first use, never a setting).

    The file is written with 0600 and lives next to ``confessions.json``: a
    backup of the guild folder carries it, so the HMAC list is only as
    secret as the data it protects. That is the point: the pepper is not in
    the settings (a settings export does not leak it).
    """
    path = _pepper_path(data_dir, guild_id)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        value = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - the platform may not support it
            log.warning("Could not tighten the pepper permissions of %s", path)
        return value


def ban_hash(data_dir, guild_id: int, user_id: int) -> str:
    """The stored form of a banned author (the id is not kept)."""
    digest = hmac.new(
        pepper(data_dir, guild_id), str(int(user_id)).encode("utf-8"), hashlib.sha256
    )
    return digest.hexdigest()


def parse_seal(sealed: str) -> tuple[str, str]:
    """Split ``key_id:base64`` into its parts (empty id when malformed)."""
    key_id, _, blob = str(sealed).partition(":")
    return key_id, blob


def shamir_split(private_b64: str, threshold: int, shares: int) -> list[str] | None:
    """The Shamir shares of a private half (None when the package is absent).

    The shares are mnemonics the staff keeps in their DMs; ``threshold`` of
    ``shares`` combine back into the private half. The package is optional:
    without it the bot falls back to the owner-only delivery.
    """
    try:
        from shamir_mnemonic import generate_mnemonics
    except ImportError:
        return None
    try:
        groups = generate_mnemonics(
            1, [(max(int(threshold), 1), max(int(shares), 1))], base64.b64decode(private_b64)
        )
    except (ValueError, TypeError):
        return None
    if not groups:
        return None
    return [str(mnemonic) for mnemonic in groups[0]]


def shamir_open(shares) -> str:
    """Combine Shamir shares back into the private half (base64)."""
    from shamir_mnemonic import combine_mnemonics

    return base64.b64encode(combine_mnemonics(list(shares))).decode()


__all__ = [
    "DEFAULT_MODERATION_SECONDS",
    "Key",
    "KeyRing",
    "ban_hash",
    "claim_hash",
    "claim_token",
    "fingerprint",
    "generate_keypair",
    "open_seal",
    "parse_seal",
    "pepper",
    "random_alias",
    "random_color",
    "seal",
    "shamir_open",
    "shamir_split",
]


# ---------------------------------------------------------------------------
# The pseudonymous alias and its accent (the confessions' identity)
# ---------------------------------------------------------------------------

#: The default saturation range of the pastel accent.
DEFAULT_SATURATION: tuple[float, float] = (0.45, 0.70)

#: The default lightness range of the pastel accent.
DEFAULT_LIGHTNESS: tuple[float, float] = (0.62, 0.78)


def _unit() -> float:
    """A random float in ``[0, 1)`` (the secrets module has no float)."""
    return (secrets.randbits(53) + 1) / (1 << 53)


def _between(low: float, high: float) -> float:
    return low + (high - low) * _unit()


def random_alias(aliases) -> str:
    """One alias from the catalog list (empty when the list is empty)."""
    pool = [str(alias).strip() for alias in aliases if str(alias).strip()]
    return secrets.choice(pool) if pool else ""


def random_color(
    *,
    hue=None,
    saturation: tuple[float, float] = DEFAULT_SATURATION,
    lightness: tuple[float, float] = DEFAULT_LIGHTNESS,
) -> str:
    """A random pastel hex (``#rrggbb``), with customizable ranges.

    ``hue``: ``None`` draws the whole wheel, a float fixes the hue and a
    tuple draws inside the range (values are turns, 0..1). ``saturation``
    and ``lightness`` are the ranges the color is drawn from, so a guild
    can ask for a lighter or stronger pastel without touching the code.
    """
    import colorsys

    if hue is None:
        picked_hue = _unit()
    elif isinstance(hue, tuple):
        picked_hue = _between(*hue)
    else:
        picked_hue = float(hue)
    picked_sat = _between(*saturation)
    picked_light = _between(*lightness)
    red, green, blue = colorsys.hls_to_rgb(
        picked_hue % 1.0,
        min(max(picked_light, 0.0), 1.0),
        min(max(picked_sat, 0.0), 1.0),
    )
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"

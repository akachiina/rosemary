"""CLI of the confession key ring: generate, rotate, prune and open seals.

The bot holds only the public halves (the key ring); on the first sealed
confession it generates the pair and DMs the Shamir shares to the staff, so
this CLI is for inspection, rotation and opening a seal. The private half is
never in the bot's data directory: it lives in the staff's DMs (or offline).
The seal is ``SealedBox`` (X25519 + XSalsa20-Poly1305): the bot can seal the
author id but cannot open it.

Usage (run from the repository root)::

    python scripts/confession_keys.py generate 123
    python scripts/confession_keys.py generate 123 --shamir 3 5
    python scripts/confession_keys.py rotate 123
    python scripts/confession_keys.py prune 123
    python scripts/confession_keys.py open <key_id:base64> --private <base64>
    python scripts/confession_keys.py open <key_id:base64> --shares "..." "..."

``generate`` adds a new public key to the ring and prints the private half
(or the Shamir shares when ``--shamir`` is given, via ``shamir-mnemonic``).
The automatic path in the cog is the normal one: this command is for a
manual rotation or a recovery drill. ``rotate`` adds a new key and schedules
the old one for retirement after the guild's moderation window. ``prune``
drops the retired keys from the ring (the private half must be destroyed by
the staff after the same window). ``open`` decrypts one sealed blob with the
private half or with enough Shamir shares.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rosemary.core.alias_identity import (  # noqa: E402
    KeyRing,
    generate_keypair,
    open_seal,
    shamir_open,
    shamir_split,
)
from rosemary.core.settings import get_setting  # noqa: E402
from rosemary.core.storage import GuildStorage  # noqa: E402

DATA_DIR = ROOT / "data"

#: The fallback moderation window (7 days) when the setting is missing.
_DEFAULT_MODERATION = 7 * 24 * 60 * 60


async def _moderation_seconds(guild_id: int) -> int:
    storage = GuildStorage(DATA_DIR)
    value = await get_setting(storage, guild_id, "confessions.moderation_days")
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MODERATION


def _print_key(key_id: str, public: str, private: str) -> None:
    print(f"key id:  {key_id}")
    print(f"public:  {public}")
    print("private: " + private)
    print("guard the private half offline: it is the only way to open a seal")


async def generate(guild_id: int, shamir: tuple[int, int] | None) -> None:
    public, private = generate_keypair()
    key = await KeyRing(DATA_DIR).add(
        guild_id, public, moderation_seconds=await _moderation_seconds(guild_id)
    )
    _print_key(key.key_id, public, private)
    if shamir is not None:
        threshold, parts = shamir
        shares = shamir_split(private, threshold, parts)
        if shares is None:
            print("shamir-mnemonic is not installed (pip install shamir-mnemonic)")
            return
        for index, share in enumerate(shares, start=1):
            print(f"share {index}: {share}")


async def rotate(guild_id: int) -> None:
    """A new key for the guild; the old one retires after the window."""
    await generate(guild_id, None)
    print("the previous private half is destroyed after the moderation window")


async def prune(guild_id: int) -> None:
    dropped = await KeyRing(DATA_DIR).prune(guild_id)
    if dropped:
        print("removed keys:", ", ".join(dropped))
    else:
        print("no retired key")


def open_seal_command(seal: str, private: str, shares: list[str]) -> None:
    if shares:
        private = shamir_open(shares)
    print(open_seal(private, seal))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="a new key pair for a guild")
    gen.add_argument("guild_id", type=int)
    gen.add_argument("--shamir", nargs=2, type=int, metavar=("THRESHOLD", "PARTS"))

    sub.add_parser("rotate", help="a new key; the old retires after the window").add_argument(
        "guild_id", type=int
    )
    sub.add_parser("prune", help="drop the retired keys from the ring").add_argument(
        "guild_id", type=int
    )

    opened = sub.add_parser("open", help="decrypt a sealed blob")
    opened.add_argument("seal")
    opened.add_argument("--private", default="")
    opened.add_argument("--shares", nargs="*", default=[])

    args = parser.parse_args()
    if args.command == "generate":
        asyncio.run(generate(args.guild_id, tuple(args.shamir) if args.shamir else None))
    elif args.command == "rotate":
        asyncio.run(rotate(args.guild_id))
    elif args.command == "prune":
        asyncio.run(prune(args.guild_id))
    elif args.command == "open":
        open_seal_command(args.seal, args.private, args.shares)


if __name__ == "__main__":
    main()

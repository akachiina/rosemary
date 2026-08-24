"""Central inventory of every customizable message.

Each entry becomes editable in /customize (and in the matching /settings
category). Keys map to i18n labels under ``card.<key>.title`` and to storage
entries in ``cards.json``. Adding a message here is all a feature needs to opt
in — the composer, persistence and resolution are shared infrastructure.
"""

from __future__ import annotations

from rosemary.core.cards import CardSpec, register_cards

BOOST_DMS = [
    "registered",
    "transferred",
    "auto_transferred",
    "invite",
    "accepted",
    "declined",
    "owner_accepted",
    "owner_declined",
    "member_left",
    "owner_member_left",
    "error",
]

BUMP_CARDS = [
    "reminder",
    "thank_you",
    "leaderboard",
    "no_bumps",
    "anti_camping",
]

WINNER_DMS = ["winner_new", "winner_again", "winner_lost"]

MODERATION_ACTIONS = ["warn", "mute", "kick", "ban"]

BOOST_LOGS = [
    "register",
    "remove",
    "transfer",
    "rename",
    "color_change",
    "emoji_change",
    "icon",
    "member_add",
    "member_remove",
    "member_left",
    "invite_sent",
    "invite_cancelled",
    "invite_accepted",
    "invite_expired",
    "orphan_remove",
    "auto_transfer",
]

BUMP_LOGS = [
    "channel_locked",
    "channel_unlocked",
    "reminder_sent",
    "bump_recorded",
    "week_reset",
    "no_winner",
    "no_bumps_posted",
    "leaderboard_posted",
    "week_reset_manual",
    "bumps_added",
]

EVENT_CARDS = ["welcome", "leave", "ban"]
BIRTHDAY_CARDS = ["announce"]


def _build() -> list[CardSpec]:
    specs: list[CardSpec] = []
    specs += [
        CardSpec(key=f"events.{name}", category="events", rich=True)
        for name in EVENT_CARDS
    ]
    specs += [
        CardSpec(key=f"birthdays.{name}", category="birthdays", rich=True)
        for name in BIRTHDAY_CARDS
    ]
    specs += [CardSpec(key=f"boost.dm.{name}", category="boost") for name in BOOST_DMS]
    specs += [
        CardSpec(key="bump.leaderboard", category="bump", rich=True),
        CardSpec(key="bump.no_bumps", category="bump", rich=True),
        CardSpec(key="bump.reminder", category="bump", rich=True),
        CardSpec(key="bump.thank_you", category="bump", rich=True),
        CardSpec(key="bump.anti_camping", category="bump"),
    ]
    specs += [CardSpec(key=f"bump.dm.{name}", category="bump") for name in WINNER_DMS]
    specs += [
        CardSpec(key=f"{action}.dm", category="moderation") for action in MODERATION_ACTIONS
    ]
    specs += [
        CardSpec(key=f"boost.logs.{name}.description", category="boost")
        for name in BOOST_LOGS
    ]
    specs += [
        CardSpec(key=f"bump.logs.{name}.description", category="bump") for name in BUMP_LOGS
    ]
    return specs


register_cards(*_build())

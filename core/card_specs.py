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

INVITES_CARDS = [
    "leaderboard",
    "stats",
    "personal",
    "invited_list",
    "server_stats",
]

INVITES_LOGS = [
    "join",
    "leave",
    "bonus",
    "sync",
    "reset",
]

MODERATION_EXTRA_CARDS = [
    "cleaner.confirm",
    "cleaner.result",
    "anti_invite.warning",
]

MODERATION_EXTRA_LOGS = [
    "cleaner.logs.purged",
    "anti_invite.logs.blocked",
]

TICKETS_CARDS = [
    "panel",
    "created",
]

TICKETS_LOGS = [
    "opened",
    "closed",
    "reopened",
    "deleted",
    "transcript",
]

PARTNERSHIPS_DMS = [
    "added",
    "removed",
    "warning",
    "expired",
]

PARTNERSHIPS_LOGS = [
    "added",
    "removed",
    "renewed",
    "warning_sent",
    "expired",
    "invite_posted",
    "orphans_cleaned",
]

SHARED_EXTRA_LOGS = [
    "reminders.logs.sent",
    "broadcast.logs.started",
    "broadcast.logs.ended",
]


def _build() -> list[CardSpec]:
    specs: list[CardSpec] = []
    specs += [
        CardSpec(key=f"events.{name}", category="events", rich=True, mention_default="single")
        for name in EVENT_CARDS
    ]
    specs += [
        CardSpec(key=f"birthdays.{name}", category="birthdays", rich=True, mention_default="single")
        for name in BIRTHDAY_CARDS
    ]
    specs += [
        CardSpec(key=f"boost.dm.{name}", category="boost", mention_default="single")
        for name in BOOST_DMS
    ]
    specs += [
        CardSpec(key="bump.leaderboard", category="bump", rich=True, mention_default="winner_auto"),
        CardSpec(key="bump.no_bumps", category="bump", rich=True),
        CardSpec(key="bump.reminder", category="bump", rich=True, mention_default="role"),
        CardSpec(key="bump.thank_you", category="bump", rich=True, mention_default="single"),
        CardSpec(key="bump.anti_camping", category="bump"),
    ]
    specs += [
        CardSpec(key=f"bump.dm.{name}", category="bump", mention_default="single")
        for name in WINNER_DMS
    ]
    specs += [
        CardSpec(key=f"{action}.dm", category="moderation", mention_default="single")
        for action in MODERATION_ACTIONS
    ]
    specs += [
        CardSpec(key=f"boost.logs.{name}.description", category="boost")
        for name in BOOST_LOGS
    ]
    specs += [
        CardSpec(key=f"bump.logs.{name}.description", category="bump") for name in BUMP_LOGS
    ]
    specs += [
        CardSpec(
            key=f"invites.{name}",
            category="invites",
            rich=(name == "leaderboard"),
            mention_default="single" if name in ("stats", "personal") else "none",
        )
        for name in INVITES_CARDS
    ]
    specs += [
        CardSpec(key=f"invites.logs.{name}.description", category="invites")
        for name in INVITES_LOGS
    ]
    specs += [
        CardSpec(key=key, category="moderation") for key in MODERATION_EXTRA_CARDS
    ]
    specs += [
        CardSpec(key=f"{key}.description", category="moderation")
        for key in MODERATION_EXTRA_LOGS
    ]
    specs += [
        CardSpec(key=f"{key}.description", category="general")
        for key in SHARED_EXTRA_LOGS
    ]
    specs += [
        CardSpec(key=f"tickets.{name}", category="tickets", rich=(name == "panel"))
        for name in TICKETS_CARDS
    ]
    specs += [
        CardSpec(key=f"tickets.logs.{name}.description", category="tickets")
        for name in TICKETS_LOGS
    ]
    specs += [
        CardSpec(key="partnerships.invite", category="partnerships", rich=True)
    ]
    specs += [
        CardSpec(key=f"partnerships.dm.{name}", category="partnerships", mention_default="single")
        for name in PARTNERSHIPS_DMS
    ]
    specs += [
        CardSpec(key=f"partnerships.logs.{name}.description", category="partnerships")
        for name in PARTNERSHIPS_LOGS
    ]
    specs += [CardSpec(key="updater.confirm", category="general")]
    specs += [
        CardSpec(key=f"updater.logs.{name}.description", category="general")
        for name in ("updating", "updated", "auto_failed")
    ]
    return specs


register_cards(*_build())

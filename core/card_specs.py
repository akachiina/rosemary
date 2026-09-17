"""Central inventory of every customizable message.

Each entry can be overridden per guild through a theme file's ``cards:``
section (see :mod:`rosemary.core.themes`); keys map to i18n labels under
``card.<key>.title``. Adding a message here is all a feature needs to opt in --
resolution, mentions and rendering are shared infrastructure.
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

MODERATION_LOGS = ["ban", "kick", "mute", "warn"]

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
    "schedule_opened",
    "schedule_closed",
]

EVENT_CARDS = ["welcome", "leave", "ban"]
BIRTHDAY_CARDS = ["announce"]

#: Registry cards: one per monitored event. Send sites pass exactly these
#: variables (test_variables sweeps the contract).
AUDIT_CARDS = [
    ("ban", True),
    ("unban", True),
    ("message_delete", True),
    ("message_edit", True),
    ("bulk_delete", False),
    ("nickname", True),
    ("avatar", True),
    ("roles", True),
    ("timeout", True),
    ("voice_join", True),
    ("voice_leave", True),
]

#: Placeholder contract for every audit card. The ``body`` of each default is
#: supplied by ``cogs.audit`` default builders from per-event template keys
#: (``card.audit.<event>.body``); the shared frame carries the identity.
AUDIT_VARIABLES = (
    "user", "user_name", "user_avatar", "server", "timestamp",
    "moderator", "reason", "message_author", "message_author_name", "message",
    "new_message", "message_link", "channel", "count", "file_url", "old_name",
    "new_name", "old_avatar", "new_avatar", "role", "duration",
)

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

#: Cards assembled in feature code that are now first-class CardSpecs --
#: "everything the bot sends is theme-customizable" (user mandate).
CONTENT_CARDS = {
    "starboard.card": (
        "user", "user_name", "user_avatar", "stars", "title", "body", "image_url", "timestamp",
    ),
    "utility.ping": ("ms",),
    "utility.serverinfo": (
        "server", "owner", "members", "roles", "channels", "text_channels", "voice_channels",
        "boosts", "emojis", "stickers", "verification", "description", "server_icon",
        "banner_url", "created_at", "created_rel", "server_id", "title", "body",
    ),
    "birthdays.list": ("body",),
    "partnerships.list": ("title", "body"),
    "partnerships.audit": ("title", "body"),
    "moderation.warnings": ("user", "title", "body"),
    "bump.stats": ("title", "body"),
    # Interactive menus expose only their heading block: selects and buttons
    # are code (Discord needs registered callbacks), so a theme can restyle
    # the heading: color, title text, footer: but not generate components.
    "settings.title": (),
    "themes.title": (),
    "debug.title": (),
    "boost.home": (),
}


#: Placeholder contract per card: the variable names the send site provides.
#: The editor preview, lint and hint derive from this map; send sites must
#: pass exactly these (enforced by tests).
VARIABLES_BY_KEY: dict[str, tuple[str, ...]] = {
    "events.welcome": ("user", "user_name", "server", "count", "user_avatar", "inviter"),
    "events.leave": ("user", "user_name", "server", "count", "inviter", "user_avatar"),
    "events.ban": ("user", "user_name", "server", "count", "inviter", "user_avatar"),
    "birthdays.announce": ("user", "server", "count"),
    "boost.dm.registered": ("mention", "role_name", "server_name", "members", "max_members"),
    "boost.dm.transferred": ("mention", "role_name", "server_name", "members", "max_members"),
    "boost.dm.auto_transferred": ("mention", "role_name", "server_name"),
    "boost.dm.invite": (
        "mention", "user", "role_name", "server_name", "inviter_mention", "expires"
    ),
    "boost.dm.accepted": ("role_name",),
    "boost.dm.declined": ("role_name",),
    "boost.dm.owner_accepted": ("member_mention", "role_name"),
    "boost.dm.owner_declined": ("member_mention", "role_name"),
    "boost.dm.member_left": ("role_name",),
    "boost.dm.owner_member_left": ("member_mention", "role_name"),
    "boost.dm.error": ("body",),
    "boost.preview": ("role_name", "owner", "count", "max_members"),
    "bump.leaderboard": (
        "winner_mention", "winner_count", "role_name", "leaderboard_list",
        "start_timestamp", "end_timestamp", "next_reset_timestamp",
    ),
    "bump.no_bumps": (),
    "bump.reminder": ("cooldown",),
    "bump.thank_you": ("mention", "cooldown", "next_bump_timestamp"),
    "bump.anti_camping": (),
    "bump.schedule.open": ("ping_role",),
    "bump.schedule.close": ("ping_role",),
    "bump.dm.winner_new": ("mention", "bump_count", "role_name", "max_members"),
    "bump.dm.winner_again": ("mention", "bump_count", "role_name", "total_wins"),
    "bump.dm.winner_lost": ("mention", "bump_count", "winner_mention"),
    "warn.dm": ("guild", "moderator", "reason"),
    "mute.dm": ("guild", "moderator", "reason", "duration"),
    "kick.dm": ("guild", "moderator", "reason"),
    "ban.dm": ("guild", "moderator", "reason"),
    "invites.leaderboard": ("title", "body"),
    "invites.stats": ("user", "total", "regular", "bonus", "fake", "left", "rank", "title", "body"),
    "invites.personal": ("user", "link", "total", "regular", "bonus", "title", "body"),
    "invites.invited_list": ("user", "title", "body"),
    "invites.server_stats": ("joins", "regular", "bonus", "fake", "left", "title", "body"),
    "invites.invited_by": ("user", "inviter", "code", "when", "status", "title", "body"),
    "cleaner.confirm": (),
    "cleaner.result": ("count", "word"),
    "anti_invite.warning": ("user",),
    "tickets.panel": (),
    "tickets.created": ("user", "type"),
    "partnerships.invite": ("server",),
    "partnerships.dm.added": ("server",),
    "partnerships.dm.removed": ("server",),
    "partnerships.dm.warning": ("days", "server"),
    "partnerships.dm.expired": ("server",),
    "updater.confirm": ("target",),
    "about.card": ("version", "channel"),
}

for _name, _rich in AUDIT_CARDS:
    VARIABLES_BY_KEY[f"audit.{_name}"] = AUDIT_VARIABLES

#: Log bodies render from these message-template variables.
LOG_VARIABLES_BY_KEY: dict[str, tuple[str, ...]] = {
    "boost.logs.register.description": ("actor", "owner", "role"),
    "boost.logs.remove.description": ("actor", "deleted", "owner", "role"),
    "boost.logs.transfer.description": ("actor", "new_owner", "old_owner", "role"),
    "boost.logs.rename.description": ("actor", "new_name", "old_name", "role"),
    "boost.logs.color_change.description": ("actor", "new_color", "role"),
    "boost.logs.emoji_change.description": ("actor", "emoji", "role"),
    "boost.logs.icon.description": ("actor", "role"),
    "boost.logs.member_add.description": ("actor", "member", "role"),
    "boost.logs.member_remove.description": ("actor", "member", "role"),
    "boost.logs.member_left.description": ("member", "owner", "role"),
    "boost.logs.invite_sent.description": ("expires", "invitee", "inviter", "role"),
    "boost.logs.invite_cancelled.description": ("actor", "invitee", "role"),
    "boost.logs.invite_accepted.description": ("inviter", "member", "role"),
    "boost.logs.invite_expired.description": ("invitee", "inviter", "role"),
    "boost.logs.orphan_remove.description": ("owner", "role"),
    "boost.logs.auto_transfer.description": ("new_owner", "old_owner", "role"),
    "bump.logs.channel_locked.description": ("channel",),
    "bump.logs.channel_unlocked.description": ("channel",),
    "bump.logs.schedule_opened.description": ("channel",),
    "bump.logs.schedule_closed.description": ("channel",),
    "bump.logs.reminder_sent.description": ("channel",),
    "bump.logs.bump_recorded.description": ("channel", "user"),
    "bump.logs.week_reset.description": ("winner", "bumps"),
    "bump.logs.no_winner.description": (),
    "bump.logs.no_bumps_posted.description": ("channel",),
    "bump.logs.leaderboard_posted.description": ("channel",),
    "bump.logs.week_reset_manual.description": ("author",),
    "bump.logs.bumps_added.description": ("author", "count", "total", "user"),
    "moderation.logs.ban.description": (
        "member", "moderator", "reason", "member_label", "moderator_label", "reason_label",
    ),
    "moderation.logs.kick.description": (
        "member", "moderator", "reason", "member_label", "moderator_label", "reason_label",
    ),
    "moderation.logs.mute.description": (
        "member", "moderator", "reason", "duration",
        "member_label", "moderator_label", "reason_label", "duration_label",
    ),
    "moderation.logs.warn.description": (
        "member", "moderator", "reason", "member_label", "moderator_label", "reason_label",
    ),
    "invites.logs.join.description": ("user", "code", "inviter", "label", "flags"),
    "invites.logs.leave.description": ("user", "inviter"),
    "invites.logs.bonus.description": ("moderator", "user", "amount", "total"),
    "invites.logs.sync.description": ("codes", "moderator"),
    "invites.logs.reset.description": ("moderator",),
    "cleaner.logs.purged.description": ("moderator", "word", "count"),
    "anti_invite.logs.blocked.description": ("user", "channel", "code"),
    "reminders.logs.sent.description": ("moderator", "name", "sent", "failed"),
    "broadcast.logs.started.description": ("moderator", "title", "channel"),
    "broadcast.logs.ended.description": ("moderator", "relayed", "sent", "failed"),
    "tickets.logs.opened.description": ("user", "type", "channel"),
    "tickets.logs.closed.description": ("channel",),
    "tickets.logs.reopened.description": ("channel",),
    "tickets.logs.deleted.description": ("channel",),
    "tickets.logs.transcript.description": ("channel", "moderator"),
    "partnerships.logs.added.description": ("user", "author"),
    "partnerships.logs.removed.description": ("user", "author"),
    "partnerships.logs.renewed.description": ("user",),
    "partnerships.logs.warning_sent.description": ("days", "user"),
    "partnerships.logs.expired.description": ("user", "ping"),
    "partnerships.logs.invite_posted.description": ("author",),
    "partnerships.logs.orphans_cleaned.description": ("author", "count"),
    "updater.logs.updating.description": ("moderator", "target", "previous"),
    "updater.logs.updated.description": ("previous", "current"),
    "updater.logs.auto_failed.description": ("reason",),
    "settings.logs.language.description": ("old", "new", "author"),
    "debug.logs.test.description": (),
}


def _spec(key: str, category: str, **kwargs) -> CardSpec:
    """Build a spec with its variable contract from the maps above."""
    variables = VARIABLES_BY_KEY.get(key, LOG_VARIABLES_BY_KEY.get(key, ()))
    return CardSpec(key=key, category=category, variables=variables, **kwargs)


def _build() -> list[CardSpec]:
    specs: list[CardSpec] = []
    specs += [
        _spec(f"events.{name}", "events", rich=True, mention_default="single")
        for name in EVENT_CARDS
    ]
    specs += [
        _spec(f"audit.{name}", "audit", rich=rich)
        for name, rich in AUDIT_CARDS
    ]
    specs += [
        _spec(f"birthdays.{name}", "birthdays", rich=True, mention_default="single")
        for name in BIRTHDAY_CARDS
    ]
    specs += [
        _spec(f"boost.dm.{name}", "boost", mention_default="single")
        for name in BOOST_DMS
    ]
    specs += [_spec("boost.preview", "boost", rich=True)]
    specs += [
        _spec("bump.leaderboard", "bump", rich=True, mention_default="winner_auto"),
        _spec("bump.no_bumps", "bump", rich=True),
        _spec("bump.reminder", "bump", rich=True, mention_default="role"),
        _spec("bump.thank_you", "bump", rich=True, mention_default="single"),
        _spec("bump.anti_camping", "bump"),
        _spec("bump.schedule.open", "bump", mention_default="role"),
        _spec("bump.schedule.close", "bump"),
    ]
    specs += [
        _spec(f"bump.dm.{name}", "bump", mention_default="single")
        for name in WINNER_DMS
    ]
    specs += [
        _spec(f"{action}.dm", "moderation", mention_default="single")
        for action in MODERATION_ACTIONS
    ]
    specs += [
        _spec(f"moderation.logs.{action}.description", "moderation")
        for action in MODERATION_LOGS
    ]
    specs += [
        _spec(f"boost.logs.{name}.description", "boost")
        for name in BOOST_LOGS
    ]
    specs += [
        _spec(f"bump.logs.{name}.description", "bump") for name in BUMP_LOGS
    ]
    specs += [
        _spec(
            f"invites.{name}",
            "invites",
            rich=(name == "leaderboard"),
            mention_default="single" if name in ("stats", "personal") else "none",
        )
        for name in INVITES_CARDS
    ]
    specs += [_spec("invites.invited_by", "invites")]
    specs += [
        _spec(f"invites.logs.{name}.description", "invites")
        for name in INVITES_LOGS
    ]
    specs += [
        _spec(key, "moderation") for key in MODERATION_EXTRA_CARDS
    ]
    specs += [
        _spec(f"{key}.description", "moderation")
        for key in MODERATION_EXTRA_LOGS
    ]
    specs += [
        _spec(f"{key}.description", "general")
        for key in SHARED_EXTRA_LOGS
    ]
    specs += [
        _spec(f"tickets.{name}", "tickets", rich=(name == "panel"))
        for name in TICKETS_CARDS
    ]
    specs += [
        _spec(f"tickets.logs.{name}.description", "tickets")
        for name in TICKETS_LOGS
    ]
    specs += [
        _spec("partnerships.invite", "partnerships", rich=True)
    ]
    specs += [
        _spec(f"partnerships.dm.{name}", "partnerships", mention_default="single")
        for name in PARTNERSHIPS_DMS
    ]
    specs += [
        _spec(f"partnerships.logs.{name}.description", "partnerships")
        for name in PARTNERSHIPS_LOGS
    ]
    specs += [_spec("updater.confirm", "general")]
    specs += [
        _spec(f"updater.logs.{name}.description", "general")
        for name in ("updating", "updated", "auto_failed")
    ]
    specs += [_spec("about.card", "general", rich=True)]
    specs += [_spec("settings.logs.language.description", "general")]
    specs += [_spec("debug.logs.test.description", "general")]
    specs += [
        CardSpec(key=key, category="general", rich=True, variables=variables)
        for key, variables in CONTENT_CARDS.items()
    ]
    return specs


register_cards(*_build())

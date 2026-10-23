"""Central inventory of every customizable message.

Each entry can be overridden per guild through a theme file's ``cards:``
section (see :mod:`rosemary.core.themes`); keys map to i18n labels under
``card.<key>.title``. Adding a message here is all a feature needs to opt in:
resolution, mentions and rendering are shared infrastructure.
"""

from __future__ import annotations

import rosemary.core.bump_cards  # noqa: F401  (registers the bump card builders)
import rosemary.core.debug_cards  # noqa: F401  (registers the /debug panel builder)
import rosemary.core.moderation_cards  # noqa: F401  (registers the moderation builders)
from rosemary.core.cards import CardSpec, register_cards
from rosemary.core.log_cards import register_log_builders
from rosemary.core.log_specs import LOG_SPECS

BOOST_DMS = [
    "registered",
    "transferred",
    "auto_transferred",
    "auto_created",
    "auto_restored",
    "unboost_deleted",
    "unboost_removed",
    "unboost_kept",
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
    ("trap", True),
]

#: Placeholder contract for every audit card. The ``body`` of each default is
#: supplied by ``cogs.audit`` default builders from per-event template keys
#: (``card.audit.<event>.body``); the shared frame carries the identity.
AUDIT_VARIABLES = (
    "user", "user_name", "user_id", "user_avatar", "server", "timestamp",
    "moderator", "reason", "message_author", "message_author_name", "message",
    "new_message", "message_link", "channel", "count", "file_url", "old_name",
    "new_name", "old_avatar", "new_avatar", "role", "duration",
    "attachment_lines", "image_url", "chat_image",
)

INVITES_CARDS = [
    "leaderboard",
    "stats",
    "personal",
    "invited_list",
    "server_stats",
]

MODERATION_EXTRA_CARDS = [
    "cleaner.confirm",
    "cleaner.result",
    "anti_invite.warning",
    "trap.dm",
    "trap.notice",
]

TICKETS_CARDS = [
    "panel",
    "created",
]

PARTNERSHIPS_DMS = [
    "added",
    "removed",
    "rep_changed",
    "warning",
    "expired",
    "expired_admin",
]

#: Cards assembled in feature code that are now first-class CardSpecs
#: ("everything the bot sends is theme-customizable", user mandate).
CONTENT_CARDS = {
    "starboard.card": (
        "user", "user_name", "user_avatar", "stars", "title", "body", "image_url",
        "message_link", "timestamp",
    ),
    "utility.ping": ("ms",),
    "utility.serverinfo": (
        "server", "owner", "members", "roles", "channels", "text_channels", "voice_channels",
        "boosts", "emojis", "stickers", "verification", "description", "server_icon",
        "banner_url", "created_at", "created_rel", "server_id", "title", "body",
    ),
    "utility.userinfo": (
        "user", "user_name", "user_avatar", "banner_url", "nickname", "created_at",
        "created_rel", "joined_at", "joined_rel", "roles", "top_role", "boosting_since",
        "timeout", "is_bot", "user_id", "title", "body",
    ),
    "utility.emojiinfo": (
        "emoji", "emoji_name", "emoji_id", "animated", "emoji_url", "source",
        "created_at", "created_rel", "title", "body",
    ),
    "utility.stickerinfo": (
        "sticker_name", "sticker_id", "description", "format", "tags", "uploader",
        "sticker_url", "created_at", "created_rel", "title", "body",
    ),
    "utility.roleinfo": (
        "role", "role_name", "role_color", "members", "position", "mentionable", "hoist",
        "role_icon", "role_id", "created_at", "created_rel", "title", "body",
    ),
    "utility.channelinfo": (
        "channel", "channel_name", "channel_type", "topic", "category", "nsfw", "slowmode",
        "position", "bitrate", "user_limit", "channel_id", "created_at", "created_rel",
        "title", "body",
    ),
    "birthdays.list": ("body",),
    "partnerships.list": ("title", "body"),
    "partnerships.audit": ("title", "body"),
    "moderation.warnings": (
        "user", "user_avatar", "has_warnings", "title", "body", "timestamp",
    ),
    "bump.stats": ("title", "body"),
    # Interactive menus expose only their heading block: selects and buttons
    # are code (Discord needs registered callbacks), so a theme can restyle
    # the heading: color, title text, footer: but not generate components.
    "settings.title": (),
    "themes.title": (),
    "debug.panel": (
        "guilds", "members", "latency", "uptime", "version", "theme",
        "logging", "audit", "timestamp",
    ),
    "cleaner.panel": (),
    "boost.home": (),
}


#: Placeholder contract per card: the variable names the send site provides.
#: The editor preview, lint and hint derive from this map; send sites must
#: pass exactly these (enforced by tests).
VARIABLES_BY_KEY: dict[str, tuple[str, ...]] = {
    "events.welcome": ("user", "user_name", "user_id", "server", "count", "user_avatar", "inviter"),
    "events.leave": ("user", "user_name", "user_id", "server", "count", "inviter", "user_avatar"),
    "events.ban": ("user", "user_name", "user_id", "server", "count", "inviter", "user_avatar"),
    "birthdays.announce": ("user", "server", "count", "user_avatar", "timestamp"),
    "boost.dm.registered": ("mention", "role_name", "server_name", "members", "max_members"),
    "boost.dm.transferred": ("mention", "role_name", "server_name", "members", "max_members"),
    "boost.dm.auto_transferred": ("mention", "role_name", "server_name"),
    "boost.dm.auto_created": ("mention", "role_name", "server_name", "max_members"),
    "boost.dm.auto_restored": ("mention", "role_name", "server_name", "max_members"),
    "boost.dm.unboost_deleted": ("mention", "role_name", "server_name"),
    "boost.dm.unboost_removed": ("mention", "role_name", "server_name"),
    "boost.dm.unboost_kept": ("mention", "role_name", "server_name"),
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
        "winner_mention", "winner_count", "winner_avatar", "role_name",
        "leaderboard_list", "start_timestamp", "end_timestamp", "next_reset_timestamp",
        "user", "timestamp",
    ),
    "bump.no_bumps": (),
    "bump.reminder": ("cooldown",),
    "bump.thank_you": (
        "mention", "cooldown", "next_bump_timestamp", "user_avatar", "user", "timestamp",
    ),
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
    "cleaner.result": ("count", "matched", "criterion"),
    "anti_invite.warning": ("user",),
    "trap.dm": ("server", "action"),
    "trap.notice": ("server", "action"),
    "roleplay.card": (
        "actor", "target", "actor_avatar", "action", "gif_url", "timestamp",
        "title", "body",
    ),
    "tickets.panel": (),
    "tickets.created": ("user", "type"),
    "suggestions.card": (
        "user", "user_name", "user_avatar", "suggestion_number", "title", "body",
        "image_url", "upvotes", "downvotes", "score", "message_link", "timestamp",
        "status",
    ),
    "suggestions.dm.vote": (
        "voter", "suggestion_number", "body", "message_link", "vote", "side",
    ),
    "suggestions.dm.approved": (
        "suggestion_number", "body", "message_link", "moderator", "reason",
        "status",
    ),
    "suggestions.dm.rejected": (
        "suggestion_number", "body", "message_link", "moderator", "reason",
        "status",
    ),
    "confessions.review": (
        "confession_number", "title", "body", "image_url", "author_line",
        "timestamp",
    ),
    "confessions.card": (
        "confession_number", "title", "body", "image_url", "color_hex",
        "timestamp",
    ),
    "confessions.response": (
        "confession_number", "reply_index", "title", "body", "parent_line",
        "color_hex", "image_url",
    ),
    "confessions.dm.approved": ("confession_number", "body", "message_link"),
    "confessions.dm.rejected": ("confession_number", "body", "reason"),
    "confessions.dm.response": (
        "confession_number", "reply_index", "body", "message_link",
    ),
    "partnerships.invite": ("server",),
    "partnerships.dm.added": ("server", "mention"),
    "partnerships.dm.removed": ("server", "mention"),
    "partnerships.dm.rep_changed": ("server", "mention"),
    "partnerships.dm.warning": ("days", "server", "mention"),
    "partnerships.dm.expired": ("server", "mention"),
    "partnerships.dm.expired_admin": ("rep", "server", "days", "invite", "mention"),
    "updater.confirm": ("target",),
    "about.card": ("version", "channel"),
    # List/manager cards promoted during the UI audit: the send site passes
    # the rendered title/body plus card-specific variables (title/body seed).
    "colors.list": ("title", "body"),
    "colors.manager": ("count", "max"),
    "invites.codes": ("title", "body"),
    "invites.blacklist": ("title", "body"),
    "bump.status": ("title", "body"),
    "reminders.list": ("title", "body"),
    "roleplay.manager": ("action",),
}

for _name, _rich in AUDIT_CARDS:
    VARIABLES_BY_KEY[f"audit.{_name}"] = AUDIT_VARIABLES

def _spec(key: str, category: str, **kwargs) -> CardSpec:
    """Build a spec with its variable contract from the map above."""
    variables = VARIABLES_BY_KEY.get(key, ())
    return CardSpec(key=key, category=category, variables=variables, **kwargs)


def _log_spec(event: str, spec) -> CardSpec:
    """Promote a log registry entry to a first-class CardSpec."""
    return CardSpec(
        key=event,
        category=spec.category,
        rich=True,
        mention_default=spec.mention_default,
        variables=spec.variables,
    )


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
        _spec(key, "moderation") for key in MODERATION_EXTRA_CARDS
    ]
    specs += [
        _spec(f"tickets.{name}", "tickets", rich=(name == "panel"))
        for name in TICKETS_CARDS
    ]
    specs += [_spec("suggestions.card", "suggestions", rich=True)]
    specs += [_spec("suggestions.dm.vote", "suggestions")]
    specs += [_spec("suggestions.dm.approved", "suggestions")]
    specs += [_spec("suggestions.dm.rejected", "suggestions")]
    specs += [_spec("confessions.review", "confessions", rich=True)]
    specs += [_spec("confessions.card", "confessions", rich=True)]
    specs += [_spec("confessions.response", "confessions", rich=True)]
    specs += [_spec("confessions.dm.approved", "confessions")]
    specs += [_spec("confessions.dm.rejected", "confessions")]
    specs += [_spec("confessions.dm.response", "confessions")]
    specs += [
        _spec("partnerships.invite", "partnerships", rich=True, mention_default="role")
    ]
    specs += [
        _spec(f"partnerships.dm.{name}", "partnerships", mention_default="single")
        for name in PARTNERSHIPS_DMS
    ]
    specs += [_spec("updater.confirm", "general")]
    # Cards promoted during the UI audit (title/body seeds + card variables).
    specs += [_spec("colors.list", "colors", rich=True)]
    specs += [_spec("colors.manager", "colors")]
    specs += [_spec("invites.codes", "invites", rich=True)]
    specs += [_spec("invites.blacklist", "invites", rich=True)]
    specs += [_spec("bump.status", "bump", rich=True)]
    specs += [_spec("reminders.list", "general", rich=True)]
    specs += [_spec("about.card", "general", rich=True)]
    specs += [_log_spec(event, spec) for event, spec in LOG_SPECS.items()]
    specs += [
        CardSpec(key=key, category="general", rich=True, variables=variables)
        for key, variables in CONTENT_CARDS.items()
    ]
    specs += [
        CardSpec(
            key="roleplay.card",
            category="roleplay",
            rich=True,
            variables=VARIABLES_BY_KEY["roleplay.card"],
            mention_default="single",
        )
    ]
    specs += [_spec("roleplay.manager", "roleplay")]
    return specs


register_log_builders()
register_cards(*_build())

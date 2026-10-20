"""Metadata for every staff log event.

A :class:`LogSpec` is *not* a renderer: it declares the catalog keys, severity,
layout and field contract of one event. The default card is assembled in
:mod:`rosemary.core.log_cards` and rendered through the same Components V2
pipeline as every other card (:func:`rosemary.core.card_service.render_card_message`).

The public key is the event itself (``moderation.logs.ban``). Legacy themes
that customized the old body card (``moderation.logs.ban.description``) keep
working: the builder reads that key as a body override, never as a full card.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

Layout = Literal["sentence", "fields", "media"]
Severity = Literal["info", "success", "warning", "danger"]
FooterMode = Literal["actor", "system", "none"]

#: Public event for one setting change. Exported so UI code can pass the event
#: without a literal that looks like a catalog setting key.
SETTINGS_LOG_EVENT = "settings.log"


@dataclass(frozen=True)
class LogField:
    """One label/value row rendered by the ``fields`` layout."""

    label: str
    variable: str
    optional: bool = False


@dataclass(frozen=True)
class LogSpec:
    """Everything the log pipeline needs to know about one event.

    ``title_key`` and ``body_key`` point at existing catalog copy. The body is
    the fallback for the ``sentence`` layout and the template used by a legacy
    per-body theme override. ``fields`` drives the ``fields`` layout; when a
    legacy body override exists it wins over the fields, so old guild themes
    keep their exact copy.
    """

    event: str
    category: str
    severity: Severity = "info"
    layout: Layout = "sentence"
    title_key: str = ""
    body_key: str = ""
    fields: tuple[LogField, ...] = ()
    mention_default: str = "none"
    actor_var: str | None = None
    footer: FooterMode = "system"
    legacy_key: str | None = None
    legacy_labels: tuple[tuple[str, str], ...] = ()
    variables: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.title_key:
            object.__setattr__(self, "title_key", f"{self.event}.title")
        if not self.body_key:
            object.__setattr__(self, "body_key", f"{self.event}.description")
        if self.legacy_key is None:
            object.__setattr__(self, "legacy_key", self.body_key)


def _f(
    variable: str,
    label: str | None = None,
    *,
    optional: bool = False,
) -> LogField:
    return LogField(label=f"log.field.{label or variable}", variable=variable, optional=optional)


#: Old body-card variable contracts. Kept as the source of truth for the
#: placeholder contract (theme previews and the legacy body overrides still
#: receive every one of these names). Event keys strip the ``.description``.
_LEGACY_VARIABLES: dict[str, tuple[str, ...]] = {
    "boost.logs.register": ("actor", "owner", "role"),
    "boost.logs.remove": ("actor", "deleted", "owner", "role"),
    "boost.logs.transfer": ("actor", "new_owner", "old_owner", "role"),
    "boost.logs.rename": ("actor", "new_name", "old_name", "role"),
    "boost.logs.color_change": ("actor", "new_color", "role"),
    "boost.logs.emoji_change": ("actor", "emoji", "role"),
    "boost.logs.icon": ("actor", "role"),
    "boost.logs.member_add": ("actor", "member", "role"),
    "boost.logs.member_remove": ("actor", "member", "role"),
    "boost.logs.member_left": ("member", "owner", "role"),
    "boost.logs.invite_sent": ("expires", "invitee", "inviter", "role"),
    "boost.logs.invite_cancelled": ("actor", "invitee", "role"),
    "boost.logs.invite_accepted": ("inviter", "member", "role"),
    "boost.logs.invite_declined": ("inviter", "member", "role"),
    "boost.logs.invite_expired": ("invitee", "inviter", "role"),
    "boost.logs.orphan_remove": ("owner", "role"),
    "boost.logs.auto_transfer": ("new_owner", "old_owner", "role"),
    "bump.logs.channel_locked": ("channel",),
    "bump.logs.channel_unlocked": ("channel",),
    "bump.logs.schedule_opened": ("channel",),
    "bump.logs.schedule_closed": ("channel",),
    "bump.logs.reminder_sent": ("channel",),
    "bump.logs.bump_recorded": ("channel", "user"),
    "bump.logs.week_reset": ("winner", "bumps", "author"),
    "bump.logs.no_winner": ("author",),
    "bump.logs.no_bumps_posted": ("channel",),
    "bump.logs.leaderboard_posted": ("channel",),
    "bump.logs.week_reset_manual": ("author",),
    "bump.logs.bumps_added": ("author", "count", "total", "user"),
    "moderation.logs.ban": (
        "member",
        "member_id",
        "moderator",
        "reason",
        "member_label",
        "member_id_label",
        "moderator_label",
        "reason_label",
    ),
    "moderation.logs.kick": (
        "member",
        "member_id",
        "moderator",
        "reason",
        "member_label",
        "member_id_label",
        "moderator_label",
        "reason_label",
    ),
    "moderation.logs.mute": (
        "member",
        "member_id",
        "moderator",
        "reason",
        "duration",
        "member_label",
        "member_id_label",
        "moderator_label",
        "reason_label",
        "duration_label",
    ),
    "moderation.logs.warn": (
        "member",
        "member_id",
        "moderator",
        "reason",
        "member_label",
        "member_id_label",
        "moderator_label",
        "reason_label",
    ),
    "invites.logs.join": ("user", "code", "inviter", "label", "flags"),
    "invites.logs.leave": ("user", "inviter"),
    "invites.logs.bonus": ("moderator", "user", "amount", "total"),
    "invites.logs.sync": ("codes", "moderator"),
    "invites.logs.reset": ("moderator",),
    "cleaner.logs.purged": ("moderator", "criterion", "count", "deleted", "matched"),
    "anti_invite.logs.blocked": ("user", "channel", "code"),
    "trap.logs.failed": (
        "user",
        "action",
        "reason",
        "user_label",
        "action_label",
        "reason_label",
    ),
    "reminders.logs.sent": ("moderator", "name", "sent", "failed"),
    "broadcast.logs.started": ("moderator", "title", "channel"),
    "broadcast.logs.ended": ("moderator", "relayed", "sent", "failed"),
    "tickets.logs.opened": ("user", "type", "channel"),
    "tickets.logs.closed": ("channel",),
    "tickets.logs.reopened": ("channel",),
    "tickets.logs.deleted": ("channel",),
    "tickets.logs.transcript": ("channel", "moderator"),
    "suggestions.logs.created": ("user", "suggestion_number", "channel"),
    "suggestions.logs.approved": ("moderator", "user", "suggestion_number", "reason"),
    "suggestions.logs.rejected": ("moderator", "user", "suggestion_number", "reason"),
    "confessions.logs.approved": ("moderator", "confession_number"),
    "confessions.logs.denied": ("moderator", "confession_number", "reason"),
    "confessions.logs.replied": ("confession_number", "reply_index"),
    "confessions.logs.banned": ("moderator", "user", "reason"),
    "partnerships.logs.added": ("user", "author"),
    "partnerships.logs.removed": ("user", "author"),
    "partnerships.logs.renewed": ("user",),
    "partnerships.logs.warning_sent": ("days", "user"),
    "partnerships.logs.expired": ("user", "ping"),
    "partnerships.logs.invite_posted": ("author",),
    "partnerships.logs.orphans_cleaned": ("author", "count"),
    "updater.logs.updating": ("moderator", "target", "previous"),
    "updater.logs.updated": ("previous", "current"),
    "updater.logs.auto_failed": ("reason",),
    "settings.log": ("setting", "category", "old", "new", "author"),
    "settings.logs.language": ("old", "new", "author"),
    "debug.trace": ("path", "origin"),
    "debug.logs.test": (),
}

#: Legacy body-variable labels get injected by ``send_log`` so a guild theme
#: that customized the old body card still renders its labels, even though the
#: new send sites no longer pass them.
_MODERATION_LABELS = (
    ("member_label", "log.field.target"),
    ("member_id_label", "log.field.member_id"),
    ("moderator_label", "log.field.moderator"),
    ("reason_label", "log.field.reason"),
)
_MUTE_LABELS = _MODERATION_LABELS + (("duration_label", "log.field.duration"),)
_TRAP_LABELS = (
    ("user_label", "log.field.target"),
    ("action_label", "log.field.consequence"),
    ("reason_label", "log.field.reason"),
)


def _spec(
    event: str,
    category: str,
    *,
    severity: Severity = "info",
    layout: Layout = "sentence",
    fields: tuple[LogField, ...] = (),
    actor_var: str | None = None,
    footer: FooterMode | None = None,
    title_key: str | None = None,
    body_key: str | None = None,
    legacy_key: str | None = None,
    legacy_labels: tuple[tuple[str, str], ...] = (),
) -> LogSpec:
    """Build one spec, appending the auto-injected footer variables."""
    base = list(_LEGACY_VARIABLES.get(event, ()))
    if "timestamp" not in base:
        base.append("timestamp")
    if actor_var is not None and "actor" not in base:
        base.append("actor")
    return LogSpec(
        event=event,
        category=category,
        severity=severity,
        layout=layout,
        title_key=title_key or f"{event}.title",
        body_key=body_key or f"{event}.description",
        fields=fields,
        actor_var=actor_var,
        footer=footer or ("actor" if actor_var else "system"),
        legacy_key=legacy_key,
        legacy_labels=legacy_labels,
        variables=tuple(base),
    )


#: Events whose default card already names the actor in its fields or body.
#: A second "by @actor" footer would duplicate the mention; these keep the
#: timestamp-only footer and the actor remains visible in the content.
_ACTOR_IN_CONTENT = frozenset(
    {
        # moderation: the moderator is a labelled field.
        "moderation.logs.ban",
        "moderation.logs.kick",
        "moderation.logs.mute",
        "moderation.logs.warn",
        # boost: every actionable event has an actor/inviter/member field.
        "boost.logs.register",
        "boost.logs.remove",
        "boost.logs.transfer",
        "boost.logs.rename",
        "boost.logs.color_change",
        "boost.logs.emoji_change",
        "boost.logs.icon",
        "boost.logs.member_add",
        "boost.logs.member_remove",
        "boost.logs.invite_sent",
        "boost.logs.invite_cancelled",
        "boost.logs.invite_accepted",
        "boost.logs.invite_declined",
        # sentence cards that start with the actor.
        "bump.logs.bump_recorded",
        "bump.logs.week_reset_manual",
        "bump.logs.bumps_added",
        "invites.logs.bonus",
        "invites.logs.sync",
        "invites.logs.reset",
        "reminders.logs.sent",
        "tickets.logs.opened",
        "tickets.logs.transcript",
        "suggestions.logs.created",
        "suggestions.logs.approved",
        "suggestions.logs.rejected",
        "confessions.logs.approved",
        "confessions.logs.denied",
        "confessions.logs.banned",
        "partnerships.logs.added",
        "partnerships.logs.removed",
        "partnerships.logs.invite_posted",
        "partnerships.logs.orphans_cleaned",
        # action panels with an explicit moderator/author field.
        "cleaner.logs.purged",
        "broadcast.logs.started",
        "broadcast.logs.ended",
        "updater.logs.updating",
        "settings.log",
        "settings.logs.language",
    }
)


def _build_specs() -> dict[str, LogSpec]:
    specs: list[LogSpec] = []

    specs += [
        _spec(
            "moderation.logs.ban",
            "moderation",
            severity="danger",
            layout="fields",
            fields=(
                _f("member", "target"),
                _f("member_id"),
                _f("moderator"),
                _f("reason"),
            ),
            actor_var="moderator",
            title_key="moderation.logs.ban.title",
            legacy_labels=_MODERATION_LABELS,
        ),
        _spec(
            "moderation.logs.kick",
            "moderation",
            severity="danger",
            layout="fields",
            fields=(
                _f("member", "target"),
                _f("member_id"),
                _f("moderator"),
                _f("reason"),
            ),
            actor_var="moderator",
            title_key="moderation.logs.kick.title",
            legacy_labels=_MODERATION_LABELS,
        ),
        _spec(
            "moderation.logs.mute",
            "moderation",
            severity="danger",
            layout="fields",
            fields=(
                _f("member", "target"),
                _f("member_id"),
                _f("moderator"),
                _f("duration", optional=True),
                _f("reason"),
            ),
            actor_var="moderator",
            title_key="moderation.logs.mute.title",
            legacy_labels=_MUTE_LABELS,
        ),
        _spec(
            "moderation.logs.warn",
            "moderation",
            severity="warning",
            layout="fields",
            fields=(
                _f("member", "target"),
                _f("member_id"),
                _f("moderator"),
                _f("reason"),
            ),
            actor_var="moderator",
            title_key="moderation.logs.warn.title",
            legacy_labels=_MODERATION_LABELS,
        ),
    ]

    boost_fields = {
        "register": (_f("role"), _f("owner"), _f("actor")),
        "remove": (_f("role"), _f("owner"), _f("actor"), _f("deleted")),
        "transfer": (_f("role"), _f("old_owner"), _f("new_owner"), _f("actor")),
        "rename": (_f("role"), _f("old_name"), _f("new_name"), _f("actor")),
        "color_change": (_f("role"), _f("new_color"), _f("actor")),
        "emoji_change": (_f("role"), _f("emoji"), _f("actor")),
        "icon": (_f("role"), _f("actor")),
        "member_add": (_f("role"), _f("member"), _f("actor")),
        "member_remove": (_f("role"), _f("member"), _f("actor")),
        "member_left": (_f("role"), _f("member"), _f("owner")),
        "invite_sent": (_f("role"), _f("invitee"), _f("inviter"), _f("expires")),
        "invite_cancelled": (_f("role"), _f("invitee"), _f("actor")),
        "invite_accepted": (_f("role"), _f("member"), _f("inviter")),
        "invite_declined": (_f("role"), _f("member"), _f("inviter")),
        "invite_expired": (_f("role"), _f("invitee"), _f("inviter")),
        "orphan_remove": (_f("role"), _f("owner")),
        "auto_transfer": (_f("role"), _f("old_owner"), _f("new_owner")),
    }
    boost_actor = {
        "register": "actor",
        "remove": "actor",
        "transfer": "actor",
        "rename": "actor",
        "color_change": "actor",
        "emoji_change": "actor",
        "icon": "actor",
        "member_add": "actor",
        "member_remove": "actor",
        "invite_sent": "inviter",
        "invite_cancelled": "actor",
        "invite_accepted": "inviter",
        "invite_declined": "member",
    }
    boost_severity: dict[str, Severity] = {
        "register": "success",
        "remove": "danger",
        "transfer": "info",
        "rename": "info",
        "color_change": "info",
        "emoji_change": "info",
        "icon": "info",
        "member_add": "success",
        "member_remove": "warning",
        "member_left": "warning",
        "invite_sent": "info",
        "invite_cancelled": "warning",
        "invite_accepted": "success",
        "invite_declined": "warning",
        "invite_expired": "warning",
        "orphan_remove": "danger",
        "auto_transfer": "warning",
    }
    for name, fields in boost_fields.items():
        specs.append(
            _spec(
                f"boost.logs.{name}",
                "boost",
                severity=boost_severity[name],
                layout="fields",
                fields=fields,
                actor_var=boost_actor.get(name),
            )
        )

    bump_specs = [
        ("channel_locked", "warning", "sentence", None, ()),
        ("channel_unlocked", "success", "sentence", None, ()),
        ("schedule_opened", "info", "sentence", None, ()),
        ("schedule_closed", "info", "sentence", None, ()),
        ("reminder_sent", "info", "sentence", None, ()),
        ("bump_recorded", "success", "sentence", "user", ()),
        ("week_reset", "success", "fields", "author", (_f("winner"), _f("bumps"))),
        ("no_winner", "info", "sentence", "author", ()),
        ("no_bumps_posted", "warning", "sentence", None, ()),
        ("leaderboard_posted", "info", "sentence", None, ()),
        ("week_reset_manual", "warning", "sentence", "author", ()),
        ("bumps_added", "info", "sentence", "author", ()),
    ]
    for name, severity, layout, actor, fields in bump_specs:
        specs.append(
            _spec(
                f"bump.logs.{name}",
                "bump",
                severity=severity,
                layout=layout,
                fields=fields,
                actor_var=actor,
            )
        )
    # The "winner role is missing" reset shares the no-winner title but has its
    # own error body and severity. It must not inherit the no-winner body
    # override: the copy is a different outcome.
    specs.append(
        _spec(
            "bump.logs.no_role",
            "bump",
            severity="danger",
            title_key="bump.logs.no_winner.title",
            body_key="bump.error_role_not_found",
            legacy_key="",
            actor_var="author",
        )
    )

    specs += [
        _spec("invites.logs.join", "invites", severity="success"),
        _spec("invites.logs.leave", "invites", severity="info"),
        _spec("invites.logs.bonus", "invites", severity="info", actor_var="moderator"),
        _spec("invites.logs.sync", "invites", severity="info", actor_var="moderator"),
        _spec("invites.logs.reset", "invites", severity="warning", actor_var="moderator"),
        _spec(
            "cleaner.logs.purged",
            "moderation",
            severity="warning",
            layout="fields",
            fields=(
                _f("moderator"),
                _f("matched"),
                _f("deleted"),
                _f("criterion"),
            ),
            actor_var="moderator",
        ),
        _spec("anti_invite.logs.blocked", "moderation", severity="warning"),
        _spec(
            "trap.logs.failed",
            "moderation",
            severity="danger",
            layout="fields",
            fields=(
                _f("user", "target"),
                _f("action", "consequence"),
                _f("reason"),
            ),
            legacy_labels=_TRAP_LABELS,
        ),
        _spec("reminders.logs.sent", "general", severity="info", actor_var="moderator"),
        _spec(
            "broadcast.logs.started",
            "general",
            severity="info",
            layout="fields",
            fields=(_f("moderator"), _f("title"), _f("channel")),
            actor_var="moderator",
        ),
        _spec(
            "broadcast.logs.ended",
            "general",
            severity="success",
            layout="fields",
            fields=(_f("moderator"), _f("relayed"), _f("sent"), _f("failed")),
            actor_var="moderator",
        ),
    ]

    specs += [
        _spec("tickets.logs.opened", "tickets", severity="success", actor_var="user"),
        _spec("tickets.logs.closed", "tickets", severity="info"),
        _spec("tickets.logs.reopened", "tickets", severity="info"),
        _spec("tickets.logs.deleted", "tickets", severity="warning"),
        _spec("tickets.logs.transcript", "tickets", severity="info", actor_var="moderator"),
        _spec("suggestions.logs.created", "suggestions", severity="info", actor_var="user"),
        _spec(
            "suggestions.logs.approved",
            "suggestions",
            severity="success",
            actor_var="moderator",
        ),
        _spec(
            "suggestions.logs.rejected",
            "suggestions",
            severity="danger",
            actor_var="moderator",
        ),
        _spec(
            "confessions.logs.approved",
            "confessions",
            severity="success",
            actor_var="moderator",
        ),
        _spec(
            "confessions.logs.denied",
            "confessions",
            severity="warning",
            actor_var="moderator",
        ),
        _spec("confessions.logs.replied", "confessions", severity="info"),
        _spec(
            "confessions.logs.banned",
            "confessions",
            severity="danger",
            actor_var="moderator",
        ),
    ]

    partnership_specs = [
        ("added", "success", "author"),
        ("removed", "warning", "author"),
        ("renewed", "success", None),
        ("warning_sent", "warning", None),
        ("expired", "danger", None),
        ("invite_posted", "info", "author"),
        ("orphans_cleaned", "warning", "author"),
    ]
    for name, severity, actor in partnership_specs:
        specs.append(
            _spec(
                f"partnerships.logs.{name}",
                "partnerships",
                severity=severity,
                actor_var=actor,
            )
        )

    specs += [
        _spec(
            "updater.logs.updating",
            "general",
            severity="warning",
            layout="fields",
            fields=(_f("moderator"), _f("target"), _f("previous")),
            actor_var="moderator",
        ),
        _spec("updater.logs.updated", "general", severity="success"),
        _spec("updater.logs.auto_failed", "general", severity="danger"),
        _spec(
            "settings.log",
            "general",
            severity="info",
            layout="fields",
            fields=(
                _f("setting"),
                _f("category"),
                _f("old"),
                _f("new"),
                _f("author", "actor"),
            ),
            actor_var="author",
            title_key="settings.log.title",
            body_key="settings.log.description",
        ),
        _spec(
            "settings.logs.language",
            "general",
            severity="info",
            layout="fields",
            fields=(_f("old"), _f("new"), _f("author", "actor")),
            actor_var="author",
            title_key="settings.logs.language.title",
        ),
        _spec(
            "debug.logs.test",
            "general",
            severity="info",
            title_key="debug.logs.test.title",
        ),
        _spec(
            "debug.trace",
            "general",
            severity="info",
            title_key="debug.trace.title",
            body_key="debug.trace.card_path",
            legacy_key="",
            footer="none",
        ),
    ]
    return {
        spec.event: replace(spec, footer="system")
        if spec.event in _ACTOR_IN_CONTENT
        else spec
        for spec in specs
    }


LOG_SPECS: dict[str, LogSpec] = _build_specs()


def log_spec(event: str) -> LogSpec | None:
    """Look up one event, ``None`` for an unknown key (never raises)."""
    return LOG_SPECS.get(event)


__all__ = ["LOG_SPECS", "LogField", "LogSpec", "log_spec"]

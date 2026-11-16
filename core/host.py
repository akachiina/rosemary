"""Host configuration: the options only the person running the process edits.

The per-guild settings live in ``settings/*.yaml`` and are edited through
``/settings``. The host options live in ``host.yaml`` (git-ignored) and are
read once at boot: who may run the host commands, whether the release
watcher runs, and the conversion limits. The example file is tracked; the
real one is a copy the host fills in and never commits.

Nothing here is a security boundary: a host who edits the bot's code can do
anything. The host commands also check the user id at runtime, because the
Discord permission overrides are a hint (a server admin can change them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: The host file at the repository root (git-ignored).
HOST_FILE = Path(__file__).resolve().parents[1] / "host.yaml"


def _as_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 10**9) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _as_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    ids: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in ids:
            ids.append(number)
    return tuple(ids)


@dataclass(frozen=True)
class ThemeAssetsConfig:
    """Which custom-theme assets the host allows.

    ``media`` (images, fonts, video, audio) is allowed by default: a theme
    bundling a logo or a background is the normal case. ``emojis`` is off by
    default: every theme emoji is uploaded as a **global** application emoji
    (namespaced), so it consumes the bot's application emoji quota.
    """

    media: bool = True
    emojis: bool = False


@dataclass(frozen=True)
class HostConfig:
    """The host options read from ``host.yaml`` (defaults when absent)."""

    owner_ids: tuple[int, ...] = ()
    identity_message: str = ""
    update_command: bool = False
    restart_command: bool = False
    update_auto: bool = False
    update_channel: str = "stable"
    update_branch: str = "main"
    update_check_hours: int = 24
    update_backup: bool = True
    update_keep_backups: int = 5
    update_restart_after: bool = True
    update_sync_after: bool = True
    convert_max_mb: int = 25
    convert_timeout: int = 120
    theme_assets: ThemeAssetsConfig = field(default_factory=ThemeAssetsConfig)

    def is_owner(self, user_id: int | None) -> bool:
        """Whether the user id is one of the trusted hosts."""
        return bool(user_id) and int(user_id) in self.owner_ids

    def primary_mention(self) -> str:
        """The first host as a mention (empty when none is configured)."""
        return f"<@{self.owner_ids[0]}>" if self.owner_ids else ""

    def hosts_mention(self) -> str:
        """Every host as a comma-separated mention list."""
        return ", ".join(f"<@{user_id}>" for user_id in self.owner_ids)

    def identity_line(self) -> str:
        """The host message with ``{primary_host}``/``{hosts}`` resolved."""
        if not self.identity_message:
            return ""
        return (
            self.identity_message.replace("{primary_host}", self.primary_mention())
            .replace("{hosts}", self.hosts_mention())
            .strip()
        )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def load_host_config(path: Path | str | None = None) -> HostConfig:
    """Read ``host.yaml`` (missing/invalid values keep the defaults)."""
    target = Path(path) if path is not None else HOST_FILE
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return HostConfig()
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Could not read %s: %s", target, exc)
        return HostConfig()
    if not isinstance(raw, dict):
        log.warning("%s must be a mapping; using the defaults", target)
        return HostConfig()

    host = _section(raw, "host")
    update = _section(raw, "update")
    convert = _section(raw, "convert")
    themes = _section(raw, "themes")
    assets = _section(themes, "assets")
    channel = str(update.get("channel") or "stable").strip().lower()
    return HostConfig(
        owner_ids=_as_ids(host.get("owner_ids")),
        identity_message=str(host.get("identity_message") or "").strip(),
        update_command=_as_bool(update.get("command"), False),
        restart_command=_as_bool(update.get("restart"), False),
        update_auto=_as_bool(update.get("auto"), False),
        update_channel=channel if channel in ("stable", "git") else "stable",
        update_branch=str(update.get("branch") or "main").strip() or "main",
        update_check_hours=_as_int(update.get("check_hours"), 24, minimum=1, maximum=168),
        update_backup=_as_bool(update.get("backup"), True),
        update_keep_backups=_as_int(update.get("keep_backups"), 5, minimum=0, maximum=100),
        update_restart_after=_as_bool(update.get("restart_after"), True),
        update_sync_after=_as_bool(update.get("sync_after"), True),
        convert_max_mb=_as_int(convert.get("max_mb"), 25, minimum=1, maximum=512),
        convert_timeout=_as_int(convert.get("timeout_seconds"), 120, minimum=5, maximum=900),
        theme_assets=ThemeAssetsConfig(
            media=_as_bool(assets.get("media"), True),
            emojis=_as_bool(assets.get("emojis"), False),
        ),
    )


__all__ = ["HOST_FILE", "HostConfig", "ThemeAssetsConfig", "load_host_config"]

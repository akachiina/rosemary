"""The host configuration: defaults, parsing and clamps."""

from __future__ import annotations

from rosemary.core.host import load_host_config


def test_defaults_when_the_file_is_missing(tmp_path):
    config = load_host_config(tmp_path / "missing.yaml")
    assert config.owner_ids == ()
    assert config.identity_message == ""
    assert config.update_command is False
    assert config.restart_command is False
    assert config.update_auto is False
    assert config.update_channel == "stable"
    assert config.update_branch == "main"
    assert config.update_check_hours == 24
    assert config.update_backup is True
    assert config.update_keep_backups == 5
    assert config.convert_max_mb == 25
    assert config.convert_timeout == 120


def test_reads_and_clamps_the_yaml(tmp_path):
    path = tmp_path / "host.yaml"
    path.write_text(
        """
host:
  owner_ids: [1, "2", 0, 1]
  identity_message: "Host: {primary_host}"
update:
  command: true
  auto: true
  channel: git
  branch: dev
  check_hours: 999
  keep_backups: -3
convert:
  max_mb: 0
  timeout_seconds: 1
""",
        encoding="utf-8",
    )
    config = load_host_config(path)
    assert config.owner_ids == (1, 2)
    assert config.identity_line() == "Host: <@1>"
    assert config.update_command is True
    assert config.update_auto is True
    assert config.update_channel == "git"
    assert config.update_branch == "dev"
    assert config.update_check_hours == 168
    assert config.update_keep_backups == 0
    assert config.convert_max_mb == 1
    assert config.convert_timeout == 5


def test_unknown_channel_falls_back_to_stable(tmp_path):
    path = tmp_path / "host.yaml"
    path.write_text("update:\n  channel: nightly\n", encoding="utf-8")
    assert load_host_config(path).update_channel == "stable"


def test_is_owner_only_for_the_configured_ids(tmp_path):
    path = tmp_path / "host.yaml"
    path.write_text("host:\n  owner_ids: [7, 8]\n", encoding="utf-8")
    config = load_host_config(path)
    assert config.is_owner(7) is True
    assert config.is_owner(9) is False
    assert config.is_owner(None) is False


def test_theme_assets_default_to_media_on_emojis_off(tmp_path):
    config = load_host_config(tmp_path / "missing.yaml")
    assert config.theme_assets.media is True
    assert config.theme_assets.emojis is False


def test_theme_assets_read_the_themes_section(tmp_path):
    path = tmp_path / "host.yaml"
    path.write_text(
        "themes:\n  assets:\n    media: false\n    emojis: true\n",
        encoding="utf-8",
    )
    config = load_host_config(path)
    assert config.theme_assets.media is False
    assert config.theme_assets.emojis is True

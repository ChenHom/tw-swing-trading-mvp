"""Discord 告警客戶端單元測試。"""
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from src.notification.discord_alert import DiscordAlertConfig, DiscordNotifier


def test_discord_config_from_env(monkeypatch):
    """環境變數優先讀取。"""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-123")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "987654321")

    cfg = DiscordAlertConfig()
    assert cfg.bot_token == "test-token-123"
    assert cfg.channel_id == "987654321"
    assert cfg.is_configured() is True


def test_discord_config_from_local_file(tmp_path, monkeypatch):
    """本地 YAML/JSON 檔案讀取（環境變數未設）。"""
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)

    # 建立本地設定檔（JSON 格式）
    config_file = tmp_path / "alert.local.yaml"
    config_file.write_text(
        json.dumps({"discord": {"channel_id": "123456789"}})
    )

    cfg = DiscordAlertConfig(str(config_file))
    assert cfg.channel_id == "123456789"
    assert cfg.is_configured() is False  # 因為無 token


def test_discord_config_not_configured():
    """未配置時 is_configured 回 False。"""
    cfg = DiscordAlertConfig()
    cfg.bot_token = None
    cfg.channel_id = None
    assert cfg.is_configured() is False


@patch("httpx.post")
def test_send_alert_success(mock_post):
    """成功發送告警。"""
    mock_post.return_value.status_code = 204

    cfg = DiscordAlertConfig()
    cfg.bot_token = "test-token"
    cfg.channel_id = "123456"

    notifier = DiscordNotifier(cfg)
    result = notifier.send_alert("Test alert message", "Test Title")

    assert result is True
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "https://discord.com/api/channels/123456/messages" in call_args[0][0]
    assert call_args[1]["headers"]["Authorization"] == "Bot test-token"
    assert "embeds" in call_args[1]["json"]
    assert call_args[1]["json"]["embeds"][0]["title"] == "Test Title"


@patch("httpx.post")
def test_send_alert_http_error(mock_post):
    """HTTP 錯誤時回 False（不外拋）。"""
    mock_post.return_value.status_code = 401

    cfg = DiscordAlertConfig()
    cfg.bot_token = "bad-token"
    cfg.channel_id = "123456"

    notifier = DiscordNotifier(cfg)
    result = notifier.send_alert("Test", "Title")

    assert result is False


@patch("httpx.post")
def test_send_alert_timeout_exception(mock_post):
    """逾時例外被吞掉，回 False。"""
    import httpx

    mock_post.side_effect = httpx.TimeoutException("connection timeout")

    cfg = DiscordAlertConfig()
    cfg.bot_token = "test-token"
    cfg.channel_id = "123456"

    notifier = DiscordNotifier(cfg)
    result = notifier.send_alert("Test", "Title")

    assert result is False


def test_send_alert_not_configured():
    """未配置時靜默回 False。"""
    cfg = DiscordAlertConfig()
    cfg.bot_token = None
    cfg.channel_id = None

    notifier = DiscordNotifier(cfg)
    result = notifier.send_alert("Test", "Title")

    assert result is False


@patch("httpx.post")
def test_send_alert_with_markdown(mock_post):
    """支援 markdown 格式訊息。"""
    mock_post.return_value.status_code = 200

    cfg = DiscordAlertConfig()
    cfg.bot_token = "token"
    cfg.channel_id = "123"

    notifier = DiscordNotifier(cfg)
    markdown_msg = "**Bold** and *italic*\n`code`"
    notifier.send_alert(markdown_msg, "Formatted")

    call_json = mock_post.call_args[1]["json"]
    assert call_json["embeds"][0]["description"] == markdown_msg

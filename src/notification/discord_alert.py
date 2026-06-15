"""Discord webhook / API 告警發送客戶端。

密鑰存放慣例（絕不入 git）：
  DISCORD_BOT_TOKEN: 走 ~/.openclaw/.env，dotenv 自動載入
  channel_id: 走 config/alert.local.yaml（gitignored），JSON 或 YAML 解析

本模組獨立於 AppSettings，純環境變數 + 本地檔讀取。
告警失敗必不外拋、不阻擋主流程（用於 cron，需極端穩定）。
"""
import os
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None


class DiscordAlertConfig:
    """讀取 Discord 告警配置（環境變數優先）。"""

    def __init__(
        self,
        config_file: str = "config/alert.local.yaml",
        openclaw_env_path: str = "~/.openclaw/.env",
    ):
        # 環境變數：優先級最高
        self.bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.channel_id = os.getenv("DISCORD_CHANNEL_ID")

        # token 次優先來源：~/.openclaw/.env（不進 os.environ，避免污染全域狀態）
        if not self.bot_token:
            try:
                from dotenv import dotenv_values

                openclaw_env = dotenv_values(os.path.expanduser(openclaw_env_path))
                self.bot_token = openclaw_env.get("DISCORD_BOT_TOKEN")
            except Exception:
                pass

        # 本地檔案（若環境變數未設）：支援 YAML 與 JSON
        if not self.channel_id and Path(config_file).exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # 簡易 YAML/JSON 解析
                    try:
                        cfg = json.loads(content)
                    except json.JSONDecodeError:
                        # 嘗試簡易 YAML（只支援 key: value 格式）
                        cfg = {}
                        for line in content.split("\n"):
                            line = line.strip()
                            if line.startswith("discord:"):
                                continue
                            if ":" in line and not line.startswith("#"):
                                k, v = line.split(":", 1)
                                cfg[k.strip()] = v.strip().strip("'\"")
                    self.channel_id = cfg.get("channel_id") or cfg.get(
                        ("discord", "channel_id"), None
                    )
                    if isinstance(cfg, dict) and "discord" in cfg:
                        self.channel_id = cfg["discord"].get("channel_id")
            except Exception:
                pass

    def is_configured(self) -> bool:
        """是否有有效配置（token 與 channel_id 都存在）。"""
        return bool(self.bot_token and self.channel_id)


class DiscordNotifier:
    """發送 Discord 告警的客戶端。"""

    def __init__(self, config: Optional[DiscordAlertConfig] = None):
        self.config = config or DiscordAlertConfig()

    def send_alert(self, message: str, title: str = "系統告警") -> bool:
        """發送告警至 Discord channel。

        Args:
            message: 告警訊息（支援 markdown）
            title: 告警標題

        Returns:
            True 若發送成功，False 若配置缺失或任何錯誤（不外拋）
        """
        if not self.config.is_configured():
            return False

        if httpx is None:
            print(
                "[DISCORD_ALERT_ERROR] httpx not installed (install requirements-web.txt)",
                file=sys.stderr,
            )
            return False

        try:
            url = f"https://discord.com/api/channels/{self.config.channel_id}/messages"
            headers = {"Authorization": f"Bot {self.config.bot_token}"}

            # Embed 格式，紅色告警
            payload = {
                "embeds": [
                    {
                        "title": title,
                        "description": message,
                        "color": 15746887,  # 紅色
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                ]
            }

            resp = httpx.post(url, json=payload, headers=headers, timeout=5)
            return 200 <= resp.status_code < 300
        except Exception as e:
            # 任何例外都吞掉，不外拋（cron 環境必須極端穩定）
            print(f"[DISCORD_ALERT_ERROR] {type(e).__name__}: {e}", file=sys.stderr)
            return False


def main():
    """CLI 模式：直接發送訊息。用法：python -m src.notification.discord_alert "訊息"。"""
    if len(sys.argv) < 2:
        print("用法: python -m src.notification.discord_alert <訊息> [標題]")
        sys.exit(1)

    message = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "系統告警"

    notifier = DiscordNotifier()
    if not notifier.config.is_configured():
        print("Discord 未配置 (缺 DISCORD_BOT_TOKEN 或 channel_id)", file=sys.stderr)
        sys.exit(1)

    success = notifier.send_alert(message, title)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

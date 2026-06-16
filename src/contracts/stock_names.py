"""股票代碼 → 中文名稱對照（共用參考資料）。

放在 contracts 層（與 decision_codes 並列），讓 CLI 與 service/web 皆可乾淨 import，
避免 service 層反向依賴 cli。查無對照時 `stock_name` 回空字串（呼叫端自行決定退場顯示）。

名稱來源優先序：
  1. config/stock_names.yaml（使用者自訂，可覆蓋內建）
  2. 下方 _BUILTIN_NAMES（universe + 常用標的，隨程式碼更新）
新增標的在 config/stock_names.yaml 補充即可，無需改程式碼。
"""
from __future__ import annotations

from pathlib import Path

_BUILTIN_NAMES: dict[str, str] = {
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2308": "台達電",
    "2382": "廣達",
    "2881": "富邦金",
    "2882": "國泰金",
    "2301": "光寶科",
    "2324": "仁寶",
    "3231": "緯創",
    "2357": "華碩",
    "2891": "中信金",
    "2886": "兆豐金",
    "2603": "長榮",
    "2609": "陽明",
    "00400A": "主動國泰動能高息",
    "00981A": "主動統一台股增長",
    "00994A": "主動第一金台股優",
    "2327": "國巨",
    "2360": "致茂",
    "3090": "日電貿",
    "3691": "碩禾",
    "6805": "富世達",
    "TSE": "加權指數",
}


def _load_user_names() -> dict[str, str]:
    """載入 config/stock_names.yaml，找不到檔案時回傳空 dict。"""
    yaml_path = Path(__file__).parent.parent.parent / "config" / "stock_names.yaml"
    try:
        import yaml  # PyYAML，專案已有相依
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


STOCK_NAMES: dict[str, str] = {**_BUILTIN_NAMES, **_load_user_names()}


def stock_name(symbol: str) -> str:
    """回傳代碼對應中文名；查無回空字串。"""
    return STOCK_NAMES.get(symbol, "")

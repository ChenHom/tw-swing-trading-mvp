"""股票代碼 → 中文名稱對照（共用參考資料）。

放在 contracts 層（與 decision_codes 並列），讓 CLI 與 service/web 皆可乾淨 import，
避免 service 層反向依賴 cli。查無對照時 `stock_name` 回空字串（呼叫端自行決定退場顯示）。

名稱來源優先序（後者覆蓋前者）：
  1. 下方 _BUILTIN_NAMES（universe + 常用標的，隨程式碼更新）
  2. config/stock_names_auto.yaml（建倉時自 Shioaji 自動補名，見 ensure_stock_name）
  3. config/stock_names.yaml（使用者自訂，可覆蓋自動值）
新增標的可在 config/stock_names.yaml 補充，或建倉時由 ensure_stock_name 自動補進 auto 檔。
查表會依檔案 mtime 自動重載，web 不必重啟即可看到新名稱。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

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

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
_AUTO_PATH = _CONFIG_DIR / "stock_names_auto.yaml"
_USER_PATH = _CONFIG_DIR / "stock_names.yaml"


def _load_yaml(path: Path) -> dict[str, str]:
    """載入代碼→名稱 YAML，找不到檔案或格式不符時回傳空 dict。"""
    try:
        import yaml  # PyYAML，專案已有相依
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _build() -> dict[str, str]:
    return {**_BUILTIN_NAMES, **_load_yaml(_AUTO_PATH), **_load_yaml(_USER_PATH)}


# 記住兩個 yaml 的 mtime；變了才重建。stat 兩檔成本可忽略（持倉數量小）。
_cached_mtimes: tuple[float, float] = (_mtime(_AUTO_PATH), _mtime(_USER_PATH))
STOCK_NAMES: dict[str, str] = _build()


def _refresh_if_changed() -> None:
    global STOCK_NAMES, _cached_mtimes
    current = (_mtime(_AUTO_PATH), _mtime(_USER_PATH))
    if current != _cached_mtimes:
        STOCK_NAMES = _build()
        _cached_mtimes = current


def stock_name(symbol: str) -> str:
    """回傳代碼對應中文名；查無回空字串。"""
    _refresh_if_changed()
    return STOCK_NAMES.get(symbol, "")


def ensure_stock_name(symbol: str, resolver: Callable[[str], str]) -> str:
    """確保 symbol 有名稱：已有則回傳；無則用 resolver 解析後寫進 auto 檔。

    resolver（如 ShioajiMarketDataProvider.resolve_name）注入，使本 helper 不直接相依
    Shioaji（可測）。resolver 回空字串（下市/指數/查無）→ 維持空白、不寫檔。
    """
    existing = stock_name(symbol)
    if existing:
        return existing

    name = resolver(symbol)
    if not name:
        return ""

    import yaml
    data = _load_yaml(_AUTO_PATH)
    data[str(symbol)] = name
    _AUTO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_AUTO_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True)
    _refresh_if_changed()
    return name

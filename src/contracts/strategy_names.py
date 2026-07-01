"""策略 strategy_id → 中文名稱與執行說明對照。

放在 contracts 層（與 stock_names / reason_codes 並列），CLI 與 service/web 共用。
查無對照時 strategy_name 回原 id、strategy_desc 回空字串。
"""
from __future__ import annotations

STRATEGY_LABELS: dict[str, dict[str, str]] = {
    "trend_breakout": {
        "name": "趨勢帶量突破",
        "desc": "進場：收盤創 20 日新高 + 成交量 >1.5 倍均量 + 站上 60MA，且大盤站上 60MA 才放行。"
                "出場：交由 risk_exit 依固定停損／移動停利／均線失效／時間停損管理。",
    },
    "pullback_rebound": {
        "name": "回檔轉強",
        "desc": "進場：均線回踩後 K 線轉強低接（含大盤 60MA 濾網、已持有不加碼）。"
                "出場：交由 risk_exit 管理。",
    },
    "trend_rider": {
        "name": "順勢交易者",
        "desc": "進場：站上上升 60MA + 創 60 日新高 + 大盤 60MA 濾網（確立中期趨勢）。"
                "出場「讓贏家跑」：寬移動停利 -25% / 長均線跌破 / 停用時間停損，不提早砍贏家。",
    },
    "trend_pullback": {
        "name": "趨勢回檔（退役）",
        "desc": "舊策略，已退役、不再進場；存量倉位出場由 risk_exit 管理。",
    },
    "mtf_resonance": {
        "name": "多週期共振",
        "desc": "進場：週 K 定方向（MACD DIF>0）＋ 日 K 觸發（MACD 金叉、零軸上），多框共振才放行。"
                "出場：交由 risk_exit 以寬移動停利『讓贏家跑』管理。",
    },
    "risk_exit": {
        "name": "風險出場",
        "desc": "固定停損／移動停利／均線失效／時間停損，依各策略 exit 參數對持倉部位產生 SELL。",
    },
    "MANUAL": {
        "name": "手動",
        "desc": "手動錄入的成交（record-fill），結構性排除於 risk_exit 自動出場之外。",
    },
    "MULTI": {
        "name": "多策略",
        "desc": "每日 orchestrator 單一 run，整合各策略 bundle 後依固定管線順序執行。",
    },
}


def strategy_name(strategy_id: str) -> str:
    """回傳策略中文名；查無回原 id。"""
    return STRATEGY_LABELS.get(strategy_id, {}).get("name", strategy_id)


def strategy_desc(strategy_id: str) -> str:
    """回傳策略執行說明（供 tooltip）；查無回空字串。"""
    return STRATEGY_LABELS.get(strategy_id, {}).get("desc", "")

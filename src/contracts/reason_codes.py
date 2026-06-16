"""交易訊號 reason_code 的可讀（中文）對照。

訊號層只攜帶機器代碼（如 TREND_BREAKOUT_ENTRY），UI / 報告需要人看得懂的句子。
此處集中提供對照，仿 src/application/services/dashboard.py 的 EVENT_TYPE_LABELS 模式。

對照表為純靜態事實；signal_reason_text() 預留 llm_explanation 參數作為日後「以 LLM
補強策略執行理由」的掛載點——有 LLM 說明就優先用，否則退回靜態句子，再退回原代碼。
"""

# reason_code -> 可讀句子
REASON_CODE_LABELS: dict[str, str] = {
    # 進場（ENTRY）
    "TREND_BREAKOUT_ENTRY": "趨勢帶量突破：站上 20 日新高且帶量、符合多頭結構",
    "PULLBACK_REBOUND_ENTRY": "回檔轉強：均線回踩後 K 線轉強，低接進場",
    "TREND_PULLBACK_ENTRY": "趨勢回檔進場（trend_pullback，已退役）",
    # 風險出場（RISK_EXIT）
    "FIXED_STOP_EXIT": "固定停損：收盤跌破停損價",
    "TRAILING_STOP_EXIT": "移動停利：自持有後最高收盤回落達停利幅度",
    "MA_BREAK_EXIT": "均線失效：連續數日收盤跌破均線（含緩衝）",
    "TIME_STOP_EXIT": "時間停損：持有達上限天數且報酬未達門檻",
    # 退役 trend_pullback 的出場代碼（存量倉位出清前仍可能出現）
    "STOP_LOSS_EXIT": "停損出場（trend_pullback，已退役）",
    "TAKE_PROFIT_EXIT": "獲利了結（trend_pullback，已退役）",
    "TREND_PULLBACK_EXIT": "趨勢回檔出場（trend_pullback，已退役）",
}


# 委託被擋的原因 token（block-reason 字串冒號前的代碼）-> 精簡中文
# 來源：src/trading/allocator.py 與 src/application/execution/engine.py 的 BUY 閘門。
BLOCK_REASON_LABELS: dict[str, str] = {
    "INSUFFICIENT_CASH": "資金不足",
    "NETTING_SUPPRESSED": "同標反向訊號互抵，未送單",
    "APPROVAL_NOT_FOUND": "查無有效授權",
    "APPROVAL_INVALID": "授權無效（過期/模式不符）",
    "APPROVAL_ID_MISMATCH": "授權 ID 不符",
    "PARAMS_HASH_MISMATCH": "策略參數與授權不符",
    "DAILY_BUY_LIMIT_EXCEEDED": "達策略每日買入限額",
    "GLOBAL_DAILY_BUY_LIMIT_EXCEEDED": "達全局每日買入限額",
    "MAX_OPEN_POSITIONS_EXCEEDED": "達策略持倉上限",
    "GLOBAL_MAX_OPEN_POSITIONS_EXCEEDED": "達全帳戶持倉上限",
    "DAILY_NEW_BUY_LIMIT_EXCEEDED": "達每日新建倉上限",
}


def block_reason_text(raw: str) -> str:
    """把委託被擋字串（如 'INSUFFICIENT_CASH: cash 2111, ...'）humanize 成精簡中文；
    無對照則回原字串。"""
    if not raw:
        return ""
    token = raw.split(":")[0].strip()
    return BLOCK_REASON_LABELS.get(token, raw)


def signal_reason_text(reason_code: str, llm_explanation: str = None) -> str:
    """把 reason_code 轉成可讀句子。

    llm_explanation：日後「以 LLM 補強策略執行理由」的掛載點。屆時由訊號層（例如
    signal_items / order_intents 新增的 llm_explanation 欄位）讀出後傳入；目前無人傳。
    優先序：LLM 說明 > 靜態對照 > 原代碼。
    """
    if llm_explanation:
        return llm_explanation
    return REASON_CODE_LABELS.get(reason_code, reason_code)

"""還原權息序列自建（A1）：raw 日 K + 股利事件 → back-adjusted 'adj' 序列。

現用 FinMind token 層級無 TaiwanStockPriceAdj，adjusted 必須自建（見 finmind_provider 檔頭）。
Back-adjust 慣例：最新價 = raw，除權息日**之前**的所有 bar 乘上該事件的調整比例——
訊號/停損看 adj 序列時，除息缺口消失（息值內含於報酬），不再假觸發停損。

  現金股利：ratio = (ex 日前一交易日 raw close − 每股息) / ex 日前一交易日 raw close
  股票股利：ratio = 1 / (1 + stock_ratio)

volume/amount 保持 raw 不調整（MACD/MA/停損只用價格；配股比例通常 <10%，
量能濾網誤差可忽略）。ponytail: 若日後要精確量能還原再乘回 1/ratio。
"""
from datetime import date

from src.contracts.models import MarketBar


def build_adjusted_bars(raw_bars: list, actions: list) -> list:
    """raw_bars：單一 symbol、依 trade_date 遞增、price_basis='raw'。
    actions：CorporateAction 形狀 dict（action_type/ex_date/cash_per_share/stock_ratio），
    cash_per_share 為元×10000。回傳等長 'adj' MarketBar list（同日期序）。

    事件 ex_date 無對應 bar（停牌/資料缺口）時仍以最近一個 ex_date 前的 bar 收盤當基準；
    完全找不到前一日 bar 的事件跳過（上市前的歷史事件）。
    """
    if not raw_bars:
        return []

    dates = [b.trade_date for b in raw_bars]

    # 每個事件對「ex_date 之前」的 bar 貢獻一個乘法因子
    factors = [1.0] * len(raw_bars)
    for action in sorted(actions, key=lambda a: str(a["ex_date"])):
        ex_date = action["ex_date"]
        if isinstance(ex_date, str):
            ex_date = date.fromisoformat(ex_date)
        # ex 日前一個有 bar 的日子（bisect 語意：最後一個 < ex_date）
        prev_idx = None
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] < ex_date:
                prev_idx = i
                break
        if prev_idx is None:
            continue  # 事件早於資料窗起點

        if action["action_type"] == "CASH_DIVIDEND" and action.get("cash_per_share"):
            prev_close = raw_bars[prev_idx].close
            if prev_close <= 0 or action["cash_per_share"] >= prev_close:
                continue  # 髒資料防線：息值 ≥ 股價不可信
            ratio = (prev_close - action["cash_per_share"]) / prev_close
        elif action["action_type"] == "STOCK_DIVIDEND" and action.get("stock_ratio"):
            ratio = 1.0 / (1.0 + action["stock_ratio"])
        else:
            continue

        for i in range(prev_idx + 1):
            factors[i] *= ratio

    adj_bars = []
    for bar, factor in zip(raw_bars, factors):
        adj_bars.append(bar.model_copy(update={
            "open": int(round(bar.open * factor)),
            "high": int(round(bar.high * factor)),
            "low": int(round(bar.low * factor)),
            "close": int(round(bar.close * factor)),
            "price_basis": "adj",
            "adjustment_factor": factor,
            "source": f"{bar.source}+adj",
        }))
    return adj_bars

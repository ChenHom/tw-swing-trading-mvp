import uuid
from datetime import date
from src.market_data.repository import SqliteMarketBarRepository

# 零股撮合時段流動性薄（無逐筆委託簿資料可校準），以加重滑價模擬折損
ODD_LOT_SLIPPAGE_MULTIPLIER = 3
# 台股漲跌停：2015-06-01 起 ±10%、此前 ±7%——多年期歷史回測不分制度會漏判鎖死日。
# 偵測門檻各留 0.5% 容忍 tick 捨入。
PRICE_LIMIT_REGIME_CHANGE = date(2015, 6, 1)
LIMIT_LOCK_THRESHOLD_PCT = 0.095
LIMIT_LOCK_THRESHOLD_PCT_PRE_2015 = 0.065


class FakeBroker:
    def __init__(self, repository: SqliteMarketBarRepository):
        self.repository = repository

    def execute_orders(
        self, orders: list[dict], execution_date: date, slippage_bps: int = 10,
        require_all_bars: bool = True
    ) -> tuple[list[dict], str, list[dict]]:
        """require_all_bars=True（live/daily）：任一檔缺當日 bar → 整批 WAITING_MARKET_DATA，
        交由 run-daily 的 WAITING 重試機制處理（資料晚到）。
        require_all_bars=False（回測）：歷史資料缺檔＝該檔當天停牌等既成事實，該單記
        UNFILLED_NO_BAR、其餘照常成交——整批 WAITING 在回測會讓當天所有訂單靜默消失。"""
        if not orders:
            return [], "FILLED", []

        if require_all_bars:
            for order in orders:
                symbol = order["symbol"]
                if not self.repository.find(symbol, execution_date):
                    return [], "WAITING_MARKET_DATA", []

        fills = []
        unfilled = []
        for order in orders:
            symbol = order["symbol"]
            action = order["action"]
            quantity = order["quantity"]

            if "long" in action:
                if "open" in action or "increase" in action:
                    side = "BUY"
                elif "close" in action:
                    side = "SELL"
                else:
                    raise ValueError(f"Unknown action: {action}")
            else:
                raise ValueError(f"Unknown action: {action}")

            bar = self.repository.find(symbol, execution_date)
            if bar is None:
                unfilled.append({**order, "side": side, "reason": "UNFILLED_NO_BAR"})
                continue

            reason = self._unfilled_reason(symbol, bar, side, execution_date)
            if reason:
                unfilled.append({**order, "side": side, "reason": reason})
                continue

            bps = slippage_bps * ODD_LOT_SLIPPAGE_MULTIPLIER if order.get("is_odd_lot") else slippage_bps
            open_price = bar.open
            if side == "BUY":
                fill_price = int(round(open_price * (1 + bps / 10000)))
            else:
                fill_price = int(round(open_price * (1 - bps / 10000)))

            fill_id = f"fill-{uuid.uuid4().hex[:8]}"
            fills.append({
                "fill_id": fill_id,
                "signal_id": order["signal_id"],
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": fill_price,
                "filled_at": f"{execution_date.isoformat()}T09:00:00+08:00",
                "status": "FILLED",
                # 帶回拆單標記：同一 signal 拆整張+零股兩筆 fill，上游 execution_key 需以此消歧義
                "is_odd_lot": bool(order.get("is_odd_lot")),
            })

        return fills, "FILLED", unfilled

    def _unfilled_reason(self, symbol: str, bar, side: str, execution_date: date) -> str | None:
        """漲跌停鎖死／零量 → UNFILLED（順延至下一交易日由上層 order_intents 重新評估，屬 Phase 3 範圍）。

        同日停損+停利保守序：risk_exit 既有優先序（固定停損→移動停利→均線失效→時間停損，
        見 risk_exit.py explain_exit）已是單一 bar 同時觸發多條件時的保守解——目前系統
        沒有與停損並存的停利訊號需要仲裁，故不在此另建機制。
        """
        if bar.volume == 0:
            return "UNFILLED_ZERO_VOLUME"

        history = self.repository.as_of(execution_date).history(symbol, limit=2)
        if len(history) < 2 or bar.high != bar.low:
            return None  # 無前一日收盤可比對，或當日有成交區間（非鎖死）

        prev_close = history[0].close
        if prev_close <= 0:
            return None

        threshold = (
            LIMIT_LOCK_THRESHOLD_PCT_PRE_2015
            if execution_date < PRICE_LIMIT_REGIME_CHANGE
            else LIMIT_LOCK_THRESHOLD_PCT
        )
        if side == "BUY" and bar.close >= prev_close * (1 + threshold):
            return "UNFILLED_LIMIT_UP_LOCKED"
        if side == "SELL" and bar.close <= prev_close * (1 - threshold):
            return "UNFILLED_LIMIT_DOWN_LOCKED"
        return None

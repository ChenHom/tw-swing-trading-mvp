import uuid
from datetime import date
from src.market_data.repository import SqliteMarketBarRepository

# 零股撮合時段流動性薄（無逐筆委託簿資料可校準），以加重滑價模擬折損
ODD_LOT_SLIPPAGE_MULTIPLIER = 3
# 台股漲跌停為前日收盤 ±10%；偵測門檻取 9.5% 容忍 tick 捨入
LIMIT_LOCK_THRESHOLD_PCT = 0.095


class FakeBroker:
    def __init__(self, repository: SqliteMarketBarRepository):
        self.repository = repository

    def execute_orders(self, orders: list[dict], execution_date: date, slippage_bps: int = 10) -> tuple[list[dict], str, list[dict]]:
        if not orders:
            return [], "FILLED", []

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

            bar = self.repository.find(symbol, execution_date)
            assert bar is not None  # 已於前一輪確認存在

            if "long" in action:
                if "open" in action or "increase" in action:
                    side = "BUY"
                elif "close" in action:
                    side = "SELL"
                else:
                    raise ValueError(f"Unknown action: {action}")
            else:
                raise ValueError(f"Unknown action: {action}")

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
                "status": "FILLED"
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

        if side == "BUY" and bar.close >= prev_close * (1 + LIMIT_LOCK_THRESHOLD_PCT):
            return "UNFILLED_LIMIT_UP_LOCKED"
        if side == "SELL" and bar.close <= prev_close * (1 - LIMIT_LOCK_THRESHOLD_PCT):
            return "UNFILLED_LIMIT_DOWN_LOCKED"
        return None

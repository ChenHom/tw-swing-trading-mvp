"""單筆部位出場試算（dry-run）。

對選定持倉套用某策略的 exit 規則（固定停損／移動停利／均線失效／時間停損）跑一次，
回報各條件目前數值與是否觸發。**純唯讀**：不寫入、不產生 SELL 訊號、不碰每日執行路徑。

與每日 risk_exit 共用同一套評估邏輯（RiskExitEngine.explain_exit），確保試算結果與
實際引擎一致。MANUAL／長期部位本就被 risk_exit 結構性排除，本工具可用來「假設若交由
某策略管理會如何」，但這些部位沒有累積的移動停利最高價水位（watermark），明細會標註。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from src.calendar.calendar import TradingCalendar, ExchangeCalendarsTradingCalendar
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.strategy.registry import StrategyDefinition
from src.strategy.risk_exit import RiskExitEngine


def dry_run_exit(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    definition: StrategyDefinition,
    as_of_date: date,
    calendar: Optional[TradingCalendar] = None,
    market_repo: Optional[SqliteMarketBarRepository] = None,
) -> dict:
    """回傳出場試算結果 dict。

    status：
      - NO_EXIT_BLOCK：該策略 YAML 無 exit: 區塊，無法套用出場規則。
      - NO_POSITION：該帳戶查無此 symbol 持倉。
      - NOT_EVALUABLE：有持倉但無行情/無效成本，無法評估。
      - OK：明細於 detail（含 reason；reason=None 表示未觸發任何出場）。
    """
    strategy_id = definition.strategy_id
    if definition.exit_params is None:
        return {
            "status": "NO_EXIT_BLOCK",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "message": f"策略 '{strategy_id}' 無 exit: 區塊，無出場規則可試算。",
        }

    projection = PortfolioProjection(conn)
    market_repo = market_repo or SqliteMarketBarRepository(conn)
    calendar = calendar or ExchangeCalendarsTradingCalendar()

    # 含長期/MANUAL：試算對象不限於 risk_exit 監控集合。
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    pos = next((p for (sid, sym), p in positions.items() if sym == symbol), None)
    if pos is None:
        return {
            "status": "NO_POSITION",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "message": f"帳戶 '{account_id}' 查無 {symbol} 持倉。",
        }

    market_data = market_repo.as_of(as_of_date)
    engine = RiskExitEngine({strategy_id: definition}, projection, calendar)
    detail = engine.explain_exit(as_of_date, account_id, pos, definition.exit_params, market_data)

    if not detail.get("evaluable"):
        return {
            "status": "NOT_EVALUABLE",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "as_of_date": as_of_date.isoformat(),
            "position": _position_summary(pos),
            "message": f"{symbol} 於 {as_of_date.isoformat()} 無可用收盤行情或成本無效，無法試算。",
        }

    return {
        "status": "OK",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "as_of_date": as_of_date.isoformat(),
        "position": _position_summary(pos),
        "detail": detail,
    }


def _position_summary(pos: dict) -> dict:
    return {
        "strategy_id": pos["strategy_id"],
        "quantity": pos["quantity"],
        "is_long_term": pos["is_long_term"],
        "first_acquired_at": pos["first_acquired_at"],
    }

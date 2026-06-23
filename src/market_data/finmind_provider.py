"""FinMind 歷史日線 + 公司行動事件來源（P0 深歷史回補主力）。

token 載入優先序（絕不入 git，仿 src/notification/discord_alert.py 慣例）：
  環境變數 FINMIND_API_TOKEN > ~/.openclaw/.env > 無 token（走未驗證/低限速免費層）。

已現場驗證（2026-06-22）：
  - TaiwanStockPrice：上市股/上櫃股/ETF/指數(TAIEX) 皆可取得，欄位 open/max/min/close 為元（float），
    Trading_Volume 為股、Trading_money 為元。
  - TaiwanStockPriceAdj：現用 token 等級為 402（需付費/贊助層級）——不可用，因此 adjusted 序列
    必須走「raw + TaiwanStockDividend 自建」路徑（見計畫 Phase 0 附錄的價格/CA 合法性規則）。
  - TaiwanStockDividend 配股(StockEarningsDistribution)比例換算公式未找到非零實例驗證，
    使用前應先核對欄位語意。
"""
import hashlib
import os
import time
from datetime import date, datetime, timezone
from typing import Optional

import requests

from src.contracts.models import MarketBar

FINMIND_BASE_URL = "https://api.finmindtrade.com/api/v4/data"

# 內部標的代碼 → FinMind data_id 對映：加權指數內部代碼用 "TSE"（與 live app.db / 策略
# index 濾網一致），但 FinMind 的 TaiwanStockPrice 把它鍵為 "TAIEX"。抓取時換 id、落帳仍存 "TSE"。
FINMIND_DATA_ID_ALIAS = {"TSE": "TAIEX"}


def load_finmind_token(openclaw_env_path: str = "~/.openclaw/.env") -> Optional[str]:
    token = os.getenv("FINMIND_API_TOKEN")
    if token:
        return token
    try:
        from dotenv import dotenv_values
        return dotenv_values(os.path.expanduser(openclaw_env_path)).get("FINMIND_API_TOKEN") or None
    except Exception:
        return None


class FinMindProvider:
    def __init__(self, token: Optional[str] = None, base_url: str = FINMIND_BASE_URL):
        self.token = token if token is not None else load_finmind_token()
        self.base_url = base_url

    def _get(self, dataset: str, data_id: str, start_date: date, end_date: date, max_retries: int = 5) -> list[dict]:
        params = {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if self.token:
            params["token"] = self.token

        backoff = 1.0
        for attempt in range(max_retries):
            resp = requests.get(self.base_url, params=params, timeout=15)
            if resp.status_code == 429:
                if attempt == max_retries - 1:
                    return []
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 402:
                # dataset 對此 token 等級不可得（如 TaiwanStockPriceAdj 需付費層）；
                # 非暫時性錯誤，不重試，回空讓上游走 DATA_INVALID/自建調整序列路徑。
                return []
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("data", []) or []
        return []

    def fetch_raw_price(
        self, symbol: str, start_date: date, end_date: date,
        exchange: str, instrument_type: str = "STOCK"
    ) -> list[MarketBar]:
        data_id = FINMIND_DATA_ID_ALIAS.get(symbol, symbol)
        rows = self._get("TaiwanStockPrice", data_id, start_date, end_date)
        return [self._row_to_bar(row, symbol, exchange, instrument_type) for row in rows]

    def _row_to_bar(self, row: dict, symbol: str, exchange: str, instrument_type: str) -> MarketBar:
        checksum = hashlib.sha256(repr(sorted(row.items())).encode("utf-8")).hexdigest()[:16]
        return MarketBar(
            symbol=symbol,
            exchange=exchange,
            instrument_type=instrument_type,
            trade_date=date.fromisoformat(row["date"]),
            open=round(row["open"] * 10000),
            high=round(row["max"] * 10000),
            low=round(row["min"] * 10000),
            close=round(row["close"] * 10000),
            volume=int(row["Trading_Volume"]),
            amount=int(row["Trading_money"]),
            source="finmind:TaiwanStockPrice",
            source_fetched_at=datetime.now(timezone.utc).isoformat(),
            raw_payload_checksum=checksum,
            price_basis="raw",
            adjustment_factor=1.0,
        )

    def fetch_dividend_events(self, symbol: str, start_date: date, end_date: date) -> list[dict]:
        """回傳 CorporateAction 形狀的 dict list（現金股利/股票股利各自獨立事件；忽略全 0 列）。
        known_at = 公告日時（PIT 閘的依據）；effective_date = 除權息交易日。
        """
        rows = self._get("TaiwanStockDividend", symbol, start_date, end_date)
        actions = []
        for row in rows:
            known_at = row.get("AnnouncementDate") or row.get("date")
            if known_at and row.get("AnnouncementTime"):
                known_at = f"{known_at}T{row['AnnouncementTime']}+08:00"

            cash = row.get("CashEarningsDistribution") or 0
            cash_ex_date = row.get("CashExDividendTradingDate")
            if cash > 0 and cash_ex_date:
                actions.append({
                    "action_id": f"finmind-{symbol}-CASH-{cash_ex_date}",
                    "symbol": symbol,
                    "action_type": "CASH_DIVIDEND",
                    "ex_date": cash_ex_date,
                    "cash_per_share": round(cash * 10000),
                    "stock_ratio": None,
                    "source": "finmind:TaiwanStockDividend",
                    "effective_date": cash_ex_date,
                    "known_at": known_at,
                })

            stock = row.get("StockEarningsDistribution") or 0
            stock_ex_date = row.get("StockExDividendTradingDate")
            if stock > 0 and stock_ex_date:
                actions.append({
                    "action_id": f"finmind-{symbol}-STOCK-{stock_ex_date}",
                    "symbol": symbol,
                    "action_type": "STOCK_DIVIDEND",
                    "ex_date": stock_ex_date,
                    "cash_per_share": None,
                    "stock_ratio": stock / 1000.0,
                    "source": "finmind:TaiwanStockDividend",
                    "effective_date": stock_ex_date,
                    "known_at": known_at,
                })
        return actions

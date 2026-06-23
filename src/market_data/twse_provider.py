"""證交所 STOCK_DAY 端點：上市股票回補/交叉驗證來源（FinMind 之外的第二來源）。

一檔一月一請求，需限速；ROC（民國年）日期格式。

範圍說明（已現場驗證 2026-06-22）：本專案目前 universe 全數標 exchange=TSE（無上櫃股），
故未實作 TPEX 對應 provider——其歷史端點（st43_result.php 等舊路徑）已回 404，現行
openapi.tpex.org.tw 只提供當日快照、無逐月歷史查詢，重建成本不對等於目前零 OTC 需求；
FinMind 的 TaiwanStockPrice 已驗證可覆蓋上櫃股，故上櫃資料單靠 FinMind 即可。
"""
import hashlib
import time
from datetime import date, datetime, timezone

import requests

from src.contracts.models import MarketBar

TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"


def roc_to_ad(roc_date: str) -> date:
    """民國年/月/日 -> date。例：'111/01/03' -> 2022-01-03。"""
    roc_year, month, day = roc_date.split("/")
    return date(int(roc_year) + 1911, int(month), int(day))


def _num(raw) -> float:
    s = str(raw).replace(",", "").strip()
    if s in ("", "--", "X"):
        return 0.0
    return float(s)


class TwseProvider:
    def __init__(self, sleep_seconds: float = 1.2):
        self.sleep_seconds = sleep_seconds

    def fetch_month(
        self, symbol: str, year: int, month: int,
        exchange: str = "TSE", instrument_type: str = "STOCK"
    ) -> list[MarketBar]:
        resp = requests.get(
            TWSE_STOCK_DAY_URL,
            params={"response": "json", "date": f"{year}{month:02d}01", "stockNo": symbol},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("stat") != "OK":
            return []  # 該月無資料（上市前/已下市/休市整月）

        bars = []
        for row in payload.get("data", []):
            try:
                bars.append(self._row_to_bar(row, symbol, exchange, instrument_type))
            except (ValueError, IndexError):
                continue  # 單列格式異常（如全月停牌列）：跳過該日，不中止整月
        return bars

    def fetch_range(
        self, symbol: str, start_date: date, end_date: date,
        exchange: str = "TSE", instrument_type: str = "STOCK"
    ) -> list[MarketBar]:
        bars = []
        y, m = start_date.year, start_date.month
        first = True
        while (y, m) <= (end_date.year, end_date.month):
            if not first:
                time.sleep(self.sleep_seconds)
            first = False
            bars.extend(self.fetch_month(symbol, y, m, exchange, instrument_type))
            m += 1
            if m > 12:
                m = 1
                y += 1
        return [b for b in bars if start_date <= b.trade_date <= end_date]

    def _row_to_bar(self, row: list, symbol: str, exchange: str, instrument_type: str) -> MarketBar:
        # fields: 日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數, 註記
        trade_date = roc_to_ad(row[0])
        checksum = hashlib.sha256(repr(row).encode("utf-8")).hexdigest()[:16]
        return MarketBar(
            symbol=symbol, exchange=exchange, instrument_type=instrument_type,
            trade_date=trade_date,
            open=round(_num(row[3]) * 10000),
            high=round(_num(row[4]) * 10000),
            low=round(_num(row[5]) * 10000),
            close=round(_num(row[6]) * 10000),
            volume=int(_num(row[1])),
            amount=int(_num(row[2])),
            source="twse:STOCK_DAY",
            source_fetched_at=datetime.now(timezone.utc).isoformat(),
            raw_payload_checksum=checksum,
            price_basis="raw",
            adjustment_factor=1.0,
        )

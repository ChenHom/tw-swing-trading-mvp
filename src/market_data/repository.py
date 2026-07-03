import sqlite3
from datetime import date
from typing import Optional, Protocol
from src.contracts.models import MarketBar

class PointInTimeMarketData(Protocol):
    @property
    def as_of_date(self) -> date:
        ...

    def history(self, symbol: str, limit: int) -> list[MarketBar]:
        ...

    def latest(self, symbol: str) -> Optional[MarketBar]:
        ...

class MarketDataRepository(Protocol):
    def as_of(self, value: date) -> PointInTimeMarketData:
        ...

class SqlitePointInTimeMarketData:
    def __init__(self, conn: sqlite3.Connection, as_of_date: date, price_basis: str = "raw"):
        self.conn = conn
        self._as_of_date = as_of_date
        # A1：同庫可存 raw 與 adj 兩種基準（UNIQUE(symbol,trade_date,price_basis)）；
        # 查詢必須鎖定單一 basis，否則序列混基準（過去靠「只存 raw」苟活）。
        self._price_basis = price_basis

    @property
    def as_of_date(self) -> date:
        return self._as_of_date

    def history(self, symbol: str, limit: int) -> list[MarketBar]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount,
                   source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at
            FROM market_bars
            WHERE symbol = ? AND trade_date <= ? AND is_complete = 1 AND price_basis = ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (symbol, self._as_of_date.isoformat(), self._price_basis, limit)
        )
        rows = cursor.fetchall()
        
        bars = []
        for r in rows:
            bars.append(
                MarketBar(
                    symbol=r["symbol"],
                    exchange=r["exchange"],
                    instrument_type=r["instrument_type"],
                    trade_date=date.fromisoformat(r["trade_date"]),
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    amount=r["amount"],
                    source=r["source"],
                    source_timezone=r["source_timezone"],
                    is_complete=r["is_complete"],
                    source_fetched_at=r["source_fetched_at"],
                    raw_payload_checksum=r["raw_payload_checksum"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"]
                )
            )
        # Return in chronological order (ascending)
        bars.reverse()
        return bars

    def latest(self, symbol: str) -> Optional[MarketBar]:
        history = self.history(symbol, limit=1)
        return history[0] if history else None

class SqliteMarketBarRepository:
    def __init__(self, conn: sqlite3.Connection, price_basis: str = "raw"):
        self.conn = conn
        self.price_basis = price_basis

    def as_of(self, value: date) -> PointInTimeMarketData:
        return SqlitePointInTimeMarketData(self.conn, value, price_basis=self.price_basis)

    def upsert(self, bar: MarketBar) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO market_bars (
                symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount,
                source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(symbol, exchange, trade_date, source) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                amount=excluded.amount,
                is_complete=excluded.is_complete,
                source_fetched_at=excluded.source_fetched_at,
                raw_payload_checksum=excluded.raw_payload_checksum,
                updated_at=datetime('now')
            """,
            (
                bar.symbol,
                bar.exchange,
                bar.instrument_type,
                bar.trade_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.source,
                bar.source_timezone,
                bar.is_complete,
                bar.source_fetched_at,
                bar.raw_payload_checksum
            )
        )
        self.conn.commit()

    def upsert_canonical(self, bar: MarketBar) -> None:
        """研究用寫入路徑：canonical bar 不變式＝(symbol, trade_date, price_basis) 唯一。

        僅供 research.db 使用（其 market_bars 以此三欄為 PK）；對既有 app.db（PK 仍是
        (symbol, exchange, trade_date, source)）呼叫會因 ON CONFLICT 目標不符實際唯一索引而報錯——
        不可用於 live 路徑，避免與 upsert() 的語意混用。
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO market_bars (
                symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount,
                source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum,
                price_basis, adjustment_factor, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(symbol, trade_date, price_basis) DO UPDATE SET
                exchange=excluded.exchange,
                instrument_type=excluded.instrument_type,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                amount=excluded.amount,
                source=excluded.source,
                is_complete=excluded.is_complete,
                source_fetched_at=excluded.source_fetched_at,
                raw_payload_checksum=excluded.raw_payload_checksum,
                adjustment_factor=excluded.adjustment_factor,
                updated_at=datetime('now')
            """,
            (
                bar.symbol,
                bar.exchange,
                bar.instrument_type,
                bar.trade_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.source,
                bar.source_timezone,
                bar.is_complete,
                bar.source_fetched_at,
                bar.raw_payload_checksum,
                bar.price_basis,
                bar.adjustment_factor,
            )
        )
        self.conn.commit()

    def find_by_basis(self, symbol: str, trade_date: date, price_basis: str) -> Optional[MarketBar]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount,
                   source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum,
                   price_basis, adjustment_factor, created_at, updated_at
            FROM market_bars
            WHERE symbol = ? AND trade_date = ? AND price_basis = ?
            """,
            (symbol, trade_date.isoformat(), price_basis)
        )
        r = cursor.fetchone()
        if not r:
            return None
        return MarketBar(
            symbol=r["symbol"],
            exchange=r["exchange"],
            instrument_type=r["instrument_type"],
            trade_date=date.fromisoformat(r["trade_date"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            amount=r["amount"],
            source=r["source"],
            source_timezone=r["source_timezone"],
            is_complete=r["is_complete"],
            source_fetched_at=r["source_fetched_at"],
            raw_payload_checksum=r["raw_payload_checksum"],
            price_basis=r["price_basis"],
            adjustment_factor=r["adjustment_factor"],
            created_at=r["created_at"],
            updated_at=r["updated_at"]
        )

    def find(self, symbol: str, trade_date: date) -> Optional[MarketBar]:
        # 與 history() 同口徑（C10）：只回完整 bar、鎖定本 repo 的 price_basis——
        # 撮合/估值不得用到訊號端看不見的列。
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount,
                   source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at
            FROM market_bars
            WHERE symbol = ? AND trade_date = ? AND is_complete = 1 AND price_basis = ?
            """,
            (symbol, trade_date.isoformat(), self.price_basis)
        )
        r = cursor.fetchone()
        if not r:
            return None
        return MarketBar(
            symbol=r["symbol"],
            exchange=r["exchange"],
            instrument_type=r["instrument_type"],
            trade_date=date.fromisoformat(r["trade_date"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            amount=r["amount"],
            source=r["source"],
            source_timezone=r["source_timezone"],
            is_complete=r["is_complete"],
            source_fetched_at=r["source_fetched_at"],
            raw_payload_checksum=r["raw_payload_checksum"],
            created_at=r["created_at"],
            updated_at=r["updated_at"]
        )

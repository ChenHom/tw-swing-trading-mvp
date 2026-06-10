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
    def __init__(self, conn: sqlite3.Connection, as_of_date: date):
        self.conn = conn
        self._as_of_date = as_of_date

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
            WHERE symbol = ? AND trade_date <= ? AND is_complete = 1
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (symbol, self._as_of_date.isoformat(), limit)
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
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def as_of(self, value: date) -> PointInTimeMarketData:
        return SqlitePointInTimeMarketData(self.conn, value)

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

    def find(self, symbol: str, trade_date: date) -> Optional[MarketBar]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, 
                   source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at
            FROM market_bars
            WHERE symbol = ? AND trade_date = ?
            """,
            (symbol, trade_date.isoformat())
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

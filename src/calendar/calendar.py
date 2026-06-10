import datetime
from datetime import date
from typing import Protocol, Sequence, Set
import exchange_calendars as xc

class TradingCalendar(Protocol):
    def is_trading_day(self, value: date) -> bool:
        """Return True if the date is a trading day, False otherwise."""
        ...

    def sessions_between(self, start: date, end: date) -> Sequence[date]:
        """Return all trading days between start and end (inclusive)."""
        ...

    def next_trading_day(self, value: date) -> date:
        """Return the next trading day after the given date."""
        ...

    def previous_trading_day(self, value: date) -> date:
        """Return the previous trading day before the given date."""
        ...

class ExchangeCalendarsTradingCalendar:
    def __init__(self, open_dates: Sequence[date] = (), closed_dates: Sequence[date] = ()):
        self.open_dates: Set[date] = set(open_dates)
        self.closed_dates: Set[date] = set(closed_dates)
        # Load the XTAI (Taiwan Stock Exchange) calendar
        self.xtai = xc.get_calendar("XTAI")

    def is_trading_day(self, value: date) -> bool:
        if value in self.open_dates:
            return True
        if value in self.closed_dates:
            return False
        # exchange_calendars is_session expects a date-like type or string
        try:
            return self.xtai.is_session(value.isoformat())
        except Exception:
            return False

    def sessions_between(self, start: date, end: date) -> Sequence[date]:
        curr = start
        sessions = []
        while curr <= end:
            if self.is_trading_day(curr):
                sessions.append(curr)
            curr += datetime.timedelta(days=1)
        return sessions

    def next_trading_day(self, value: date) -> date:
        curr = value + datetime.timedelta(days=1)
        # Limit search to prevent infinite loop in case of bad calendar state
        limit = 0
        while not self.is_trading_day(curr):
            curr += datetime.timedelta(days=1)
            limit += 1
            if limit > 365:
                raise ValueError(f"Could not find next trading day after {value} within a year")
        return curr

    def previous_trading_day(self, value: date) -> date:
        curr = value - datetime.timedelta(days=1)
        limit = 0
        while not self.is_trading_day(curr):
            curr -= datetime.timedelta(days=1)
            limit += 1
            if limit > 365:
                raise ValueError(f"Could not find previous trading day before {value} within a year")
        return curr

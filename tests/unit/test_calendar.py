import pytest
from datetime import date
from src.calendar.calendar import ExchangeCalendarsTradingCalendar

def test_trading_calendar_basic():
    calendar = ExchangeCalendarsTradingCalendar()
    # 2026-06-08 is a Monday (regular trading day in Taiwan)
    assert calendar.is_trading_day(date(2026, 6, 8)) is True
    # 2026-06-06 is a Saturday (weekend)
    assert calendar.is_trading_day(date(2026, 6, 6)) is False

def test_trading_calendar_overrides():
    # Force Saturday to be open and Monday to be closed
    calendar = ExchangeCalendarsTradingCalendar(
        open_dates=[date(2026, 6, 6)],
        closed_dates=[date(2026, 6, 8)]
    )
    # Saturday is now open
    assert calendar.is_trading_day(date(2026, 6, 6)) is True
    # Monday is now closed
    assert calendar.is_trading_day(date(2026, 6, 8)) is False

def test_sessions_between():
    calendar = ExchangeCalendarsTradingCalendar()
    # 2026-06-05 (Friday) to 2026-06-08 (Monday)
    # Weekend should be skipped, so should return Friday & Monday
    sessions = calendar.sessions_between(date(2026, 6, 5), date(2026, 6, 8))
    assert list(sessions) == [date(2026, 6, 5), date(2026, 6, 8)]

def test_next_and_previous_trading_day():
    calendar = ExchangeCalendarsTradingCalendar()
    # Next trading day after Friday 2026-06-05 is Monday 2026-06-08
    assert calendar.next_trading_day(date(2026, 6, 5)) == date(2026, 6, 8)
    # Previous trading day before Monday 2026-06-08 is Friday 2026-06-05
    assert calendar.previous_trading_day(date(2026, 6, 8)) == date(2026, 6, 5)

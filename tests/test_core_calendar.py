from datetime import date, datetime, timezone

from gexlab.engine.calendar import (SECONDS_PER_YEAR, TradingCalendar, expiry_kind, settlement_dt,
                                    time_to_expiry)

UTC = timezone.utc


def test_pm_settlement_is_1600_et():
    # 2026-10-19 is EDT (UTC-4): 16:00 ET = 20:00Z
    assert settlement_dt(date(2026, 10, 19), "PM") == datetime(2026, 10, 19, 20, 0, tzinfo=UTC)


def test_am_settlement_is_0930_et():
    assert settlement_dt(date(2026, 10, 16), "AM") == datetime(2026, 10, 16, 13, 30, tzinfo=UTC)


def test_T_am_vs_pm_same_day():
    as_of = datetime(2026, 10, 16, 13, 0, tzinfo=UTC)          # 09:00 ET
    t_am = time_to_expiry(as_of, date(2026, 10, 16), "AM")
    t_pm = time_to_expiry(as_of, date(2026, 10, 16), "PM")
    assert abs(t_am * SECONDS_PER_YEAR - 1800) < 1e-6           # 30 minutes
    assert abs(t_pm * SECONDS_PER_YEAR - 7 * 3600) < 1e-6       # 7 hours


def test_am_expired_after_0930():
    as_of = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)          # 10:00 ET
    assert time_to_expiry(as_of, date(2026, 10, 16), "AM") < 0
    assert time_to_expiry(as_of, date(2026, 10, 16), "PM") > 0


def test_early_close_calendar_aware():
    cal = TradingCalendar.from_alpaca([{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])
    as_of = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)          # 10:00 EST (UTC-5)
    t = time_to_expiry(as_of, date(2026, 11, 27), "PM", cal)
    assert abs(t * SECONDS_PER_YEAR - 3 * 3600) < 1e-6          # to 13:00, not 16:00
    assert settlement_dt(date(2026, 11, 27), "PM", cal) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_dst_boundary_uses_ny_offset():
    # 2026-11-02 is after the DST change (EST, UTC-5)
    assert settlement_dt(date(2026, 11, 2), "PM") == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)


def test_expiry_kind():
    assert expiry_kind(date(2026, 10, 16)) == "monthly"   # third Friday
    assert expiry_kind(date(2026, 10, 23)) == "weekly"
    assert expiry_kind(date(2026, 10, 20)) == "daily"


def test_calendar_sessions_and_prev():
    cal = TradingCalendar.weekdays(date(2026, 10, 1), date(2026, 10, 31))
    assert not cal.is_session(date(2026, 10, 17))
    assert cal.prev_session(date(2026, 10, 19)) == date(2026, 10, 16)
    assert cal.latest_session_on_or_before(date(2026, 10, 18)) == date(2026, 10, 16)
    assert cal.market_open(datetime(2026, 10, 16, 15, 0, tzinfo=UTC))
    assert not cal.market_open(datetime(2026, 10, 16, 21, 0, tzinfo=UTC))

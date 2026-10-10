from __future__ import annotations

from datetime import date, datetime

import pymysql
import pytest

from utils import holiday_util
from utils.holiday_util import HolidayDataError, HolidayUtil

HOLIDAYS = {
    2026: [(date(2026, 10, 5), "개천절(대체휴일)"), (date(2026, 10, 9), "한글날"), (date(2026, 12, 31), "연말휴장일")],
    2027: [(date(2027, 1, 1), "신정")],
}


class FakeCursor:
    def __init__(self, conn: FakeConn, cursor_class) -> None:
        self.conn = conn
        self.cursor_class = cursor_class
        self.rows: list = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params) -> None:
        self.conn.queries.append((sql, params, self.cursor_class))
        self.rows = HOLIDAYS.get(params[0].year, [])

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self) -> None:
        self.queries: list = []
        self.closed = False

    def cursor(self, cursor_class=None) -> FakeCursor:
        return FakeCursor(self, cursor_class)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


def test_is_trading_day_checks_weekend_and_holiday(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    assert util.is_trading_day(date(2026, 10, 8)) is True       # 목요일
    assert util.is_trading_day(date(2026, 10, 9)) is False      # 한글날
    assert util.is_trading_day(date(2026, 10, 10)) is False     # 토요일
    assert util.is_holiday(date(2026, 10, 10)) is False         # 주말은 휴장일 목록에 없음


def test_accepts_string_and_datetime(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    assert util.is_trading_day("20261009") is False
    assert util.is_trading_day("2026-10-09") is False
    assert util.is_trading_day(datetime(2026, 10, 8, 15, 30)) is True
    with pytest.raises(ValueError, match="날짜 형식"):
        util.is_trading_day("2026/10/09")


def test_get_holiday_name(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    assert util.get_holiday_name("20261231") == "연말휴장일"
    assert util.get_holiday_name("20261230") is None


def test_previous_and_next_trading_day_skip_holidays_and_weekends(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    assert util.get_previous_trading_day("20261012") == date(2026, 10, 8)   # 월 -> 일/토/금(한글날) 건너 목
    assert util.get_next_trading_day("20261008") == date(2026, 10, 12)
    assert util.get_next_trading_day("20261230") == date(2027, 1, 4)        # 연말휴장일, 신정, 주말 건너뜀


def test_default_base_date_is_today_kst(conn: FakeConn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holiday_util, "today_kst", lambda: date(2026, 10, 12))
    util = HolidayUtil(conn)

    assert util.is_today_trading_day() is True
    assert util.get_previous_trading_day() == date(2026, 10, 8)


def test_missing_year_raises_instead_of_assuming_open(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    with pytest.raises(HolidayDataError, match="2025년"):
        util.is_trading_day("20250102")
    with pytest.raises(HolidayDataError):
        util.get_previous_trading_day("20260101")


def test_queries_each_year_once_with_tuple_cursor_and_keeps_given_conn_open(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    util.is_trading_day("20261008")
    util.get_holiday_name("20261009")
    util.get_holidays(2026)

    assert len(conn.queries) == 1
    sql, params, cursor_class = conn.queries[0]
    assert "FROM mqway.krx_holiday" in sql
    assert params == (date(2026, 1, 1), date(2026, 12, 31))
    assert cursor_class is pymysql.cursors.Cursor
    assert conn.closed is False


def test_get_holidays_returns_copy(conn: FakeConn) -> None:
    util = HolidayUtil(conn)

    util.get_holidays(2026).clear()

    assert util.is_holiday("20261009") is True


def test_connects_from_env_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[FakeConn] = []
    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        opened.append(FakeConn())
        return opened[-1]

    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MYSQL_HOST", "db.example")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    monkeypatch.setenv("MYSQL_USER", "u")
    monkeypatch.setenv("MYSQL_PASSWORD", "p")
    monkeypatch.setattr(holiday_util.pymysql, "connect", fake_connect)

    assert HolidayUtil().is_holiday("20261009") is True
    assert captured["host"] == "db.example" and captured["port"] == 3307 and "database" not in captured
    assert opened[0].closed is True


def test_missing_db_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DB_HOST", "DB_USER", "MYSQL_HOST", "MYSQL_USER"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(HolidayDataError, match="접속 정보"):
        HolidayUtil().is_holiday("20261009")

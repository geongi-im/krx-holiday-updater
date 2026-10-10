from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import calendar_db
from calendar_db import CalendarRow, DbConfig, build_calendar_rows, upsert_rows

KST = timezone(timedelta(hours=9))


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self.conn.executed.append(sql)
        self.conn.params.append(params)

    def fetchone(self):
        return (self.conn.count,)

    def executemany(self, sql, params) -> None:
        if self.conn.fail:
            raise RuntimeError("db error")
        self.conn.many.append((sql, list(params)))


class FakeConn:
    def __init__(self, fail: bool = False, count: int = 0) -> None:
        self.fail = fail
        self.count = count
        self.executed: list[str] = []
        self.params: list = []
        self.many: list[tuple[str, list]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_build_calendar_rows_covers_every_day() -> None:
    rows = build_calendar_rows(2026, {})

    assert len(rows) == 365
    assert rows[0].cal_date == date(2026, 1, 1)
    assert rows[-1].cal_date == date(2026, 12, 31)
    assert build_calendar_rows(2028, {})[-1].cal_date == date(2028, 12, 31)
    assert len(build_calendar_rows(2028, {})) == 366


def test_build_calendar_rows_marks_weekend_holiday_and_open_day() -> None:
    holidays = {date(2026, 12, 31): "연말휴장일"}
    by_date = {row.cal_date: row for row in build_calendar_rows(2026, holidays)}

    assert by_date[date(2026, 12, 31)] == CalendarRow(date(2026, 12, 31), False, "연말휴장일")
    assert by_date[date(2026, 10, 10)] == CalendarRow(date(2026, 10, 10), False, "주말")  # 토요일
    assert by_date[date(2026, 10, 12)] == CalendarRow(date(2026, 10, 12), True, None)


def test_build_calendar_rows_prefers_krx_name_on_weekend() -> None:
    by_date = {row.cal_date: row for row in build_calendar_rows(2026, {date(2026, 3, 1): "삼일절"})}

    assert by_date[date(2026, 3, 1)].holiday_name == "삼일절"


def test_upsert_rows_commits_with_naive_kst_timestamp() -> None:
    conn = FakeConn()
    rows = [CalendarRow(date(2026, 1, 1), False, "신정"), CalendarRow(date(2026, 1, 2), True, None)]
    updated_at = datetime(2026, 10, 10, 7, 0, tzinfo=KST)

    assert upsert_rows(conn, rows, updated_at) == 2
    sql, params = conn.many[0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params == [
        (date(2026, 1, 1), 0, "신정", datetime(2026, 10, 10, 7, 0)),
        (date(2026, 1, 2), 1, None, datetime(2026, 10, 10, 7, 0)),
    ]
    assert conn.commits == 1


def test_upsert_rows_rolls_back_on_error() -> None:
    conn = FakeConn(fail=True)

    with pytest.raises(RuntimeError):
        upsert_rows(conn, [CalendarRow(date(2026, 1, 1), False, "신정")], datetime.now(KST))
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_ensure_table_creates_table() -> None:
    conn = FakeConn()

    calendar_db.ensure_table(conn)

    assert "CREATE TABLE IF NOT EXISTS krx_calendar" in conn.executed[0]
    assert conn.commits == 1


def test_db_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {"DB_HOST": "localhost", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "mqway", "DB_PORT": "3307"}.items():
        monkeypatch.setenv(key, value)

    assert DbConfig.from_env() == DbConfig("localhost", 3307, "u", "p", "mqway")


def test_db_config_from_env_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DB_HOST", "localhost")

    with pytest.raises(ValueError, match="DB_USER, DB_PASSWORD, DB_NAME"):
        DbConfig.from_env()


def test_count_krx_holidays_queries_year_without_weekend() -> None:
    conn = FakeConn(count=17)

    assert calendar_db.count_krx_holidays(conn, 2026) == 17
    assert "is_open = 0" in conn.executed[0]
    assert conn.params[0] == (date(2026, 1, 1), date(2026, 12, 31), "주말")

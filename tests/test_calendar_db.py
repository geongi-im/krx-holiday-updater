from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import calendar_db
from calendar_db import DbConfig, count_holidays, replace_year_holidays

KST = timezone(timedelta(hours=9))


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self.conn.executed.append((sql, params))

    def executemany(self, sql, params) -> None:
        if self.conn.fail:
            raise RuntimeError("db error")
        self.conn.many.append((sql, list(params)))

    def fetchone(self):
        return (self.conn.count,)


class FakeConn:
    def __init__(self, fail: bool = False, count: int = 0) -> None:
        self.fail = fail
        self.count = count
        self.executed: list[tuple[str, object]] = []
        self.many: list[tuple[str, list]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_replace_year_holidays_deletes_year_then_inserts_sorted() -> None:
    conn = FakeConn()
    holidays = {date(2026, 12, 31): "연말휴장일", date(2026, 1, 1): "신정"}
    updated_at = datetime(2026, 10, 10, 7, 0, tzinfo=KST)

    assert replace_year_holidays(conn, 2026, holidays, updated_at) == 2

    sql, params = conn.executed[0]
    assert sql.startswith("DELETE FROM krx_holiday")
    assert params == (date(2026, 1, 1), date(2026, 12, 31))
    insert_sql, rows = conn.many[0]
    assert insert_sql.startswith("INSERT INTO krx_holiday")
    assert rows == [
        (date(2026, 1, 1), "신정", datetime(2026, 10, 10, 7, 0)),
        (date(2026, 12, 31), "연말휴장일", datetime(2026, 10, 10, 7, 0)),
    ]
    assert conn.commits == 1


def test_replace_year_holidays_rolls_back_on_error() -> None:
    conn = FakeConn(fail=True)

    with pytest.raises(RuntimeError):
        replace_year_holidays(conn, 2026, {date(2026, 1, 1): "신정"}, datetime.now(KST))
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_replace_year_holidays_rejects_other_year() -> None:
    conn = FakeConn()

    with pytest.raises(ValueError, match="2027-01-01"):
        replace_year_holidays(conn, 2026, {date(2027, 1, 1): "신정"}, datetime.now(KST))
    assert conn.executed == []


def test_count_holidays_queries_year() -> None:
    conn = FakeConn(count=17)

    assert count_holidays(conn, 2026) == 17
    sql, params = conn.executed[0]
    assert sql.startswith("SELECT COUNT(*) FROM krx_holiday")
    assert params == (date(2026, 1, 1), date(2026, 12, 31))


def test_ensure_table_creates_holiday_only_table() -> None:
    conn = FakeConn()

    calendar_db.ensure_table(conn)

    sql = conn.executed[0][0]
    assert "CREATE TABLE IF NOT EXISTS krx_holiday" in sql
    assert "is_open" not in sql
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

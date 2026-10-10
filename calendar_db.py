"""
krx_calendar 테이블 정의와 저장 로직.

공휴일만 넣으면 "데이터 없음"과 "개장일"을 구분할 수 없으므로 연도의 모든 날짜를 개장 여부와 함께 저장한다.
조회하는 쪽에서 행이 없으면 "모름"으로 보고 대체 수단을 쓰면 된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pymysql

TABLE_NAME = "krx_calendar"
WEEKEND_NAME = "주말"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    cal_date     DATE        NOT NULL COMMENT '날짜',
    is_open      TINYINT(1)  NOT NULL COMMENT '개장 여부 (1: 개장, 0: 휴장)',
    holiday_name VARCHAR(50) NULL     COMMENT '휴장 사유 (개장일은 NULL, 주말은 주말)',
    updated_at   DATETIME    NOT NULL COMMENT '마지막 갱신 시각 (KST)',
    PRIMARY KEY (cal_date),
    KEY idx_is_open_date (is_open, cal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='KRX 개장/휴장 달력'
"""

UPSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (cal_date, is_open, holiday_name, updated_at)
VALUES (%s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    is_open = VALUES(is_open),
    holiday_name = VALUES(holiday_name),
    updated_at = VALUES(updated_at)
"""

COUNT_KRX_HOLIDAYS_SQL = f"""
SELECT COUNT(*) FROM {TABLE_NAME}
WHERE cal_date BETWEEN %s AND %s AND is_open = 0 AND holiday_name <> %s
"""


@dataclass(frozen=True)
class CalendarRow:
    """krx_calendar 한 행."""

    cal_date: date
    is_open: bool
    holiday_name: str | None


@dataclass(frozen=True)
class DbConfig:
    """MariaDB 접속 정보."""

    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> DbConfig:
        """환경변수에서 접속 정보를 읽는다.

        Returns:
            DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME 으로 만든 설정입니다.

        Raises:
            ValueError: 필수 환경변수가 비어 있을 때 발생합니다.
        """
        values = {key: os.getenv(key, "").strip() for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError(f"DB 환경변수가 비어 있습니다: {', '.join(missing)}")
        return cls(
            host=values["DB_HOST"],
            port=int(os.getenv("DB_PORT", "3306") or 3306),
            user=values["DB_USER"],
            password=values["DB_PASSWORD"],
            database=values["DB_NAME"],
        )


def build_calendar_rows(year: int, holidays: dict[date, str]) -> list[CalendarRow]:
    """연도의 모든 날짜에 대해 개장 여부를 계산한다.

    Args:
        year: 대상 연도입니다.
        holidays: KRX 휴장일 날짜와 사유입니다. 주말은 들어 있지 않아도 됩니다.

    Returns:
        1월 1일부터 12월 31일까지 날짜순으로 정렬된 행 목록입니다.
    """
    rows: list[CalendarRow] = []
    day = date(year, 1, 1)
    while day.year == year:
        if day in holidays:
            rows.append(CalendarRow(day, False, holidays[day]))
        elif day.weekday() >= 5:
            rows.append(CalendarRow(day, False, WEEKEND_NAME))
        else:
            rows.append(CalendarRow(day, True, None))
        day += timedelta(days=1)
    return rows


def connect(config: DbConfig) -> pymysql.connections.Connection:
    """MariaDB 에 접속한다. 자동 커밋은 끄고 호출하는 쪽에서 커밋한다.

    Args:
        config: 접속 정보입니다.

    Returns:
        pymysql 연결 객체입니다.
    """
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
    )


def ensure_table(conn: Any) -> None:
    """krx_calendar 테이블이 없으면 만든다.

    Args:
        conn: pymysql 연결 객체입니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)
    conn.commit()


def count_krx_holidays(conn: Any, year: int) -> int:
    """이미 저장된 해당 연도의 KRX 휴장일(주말 제외 휴장) 수를 센다.

    Args:
        conn: pymysql 연결 객체입니다.
        year: 대상 연도입니다.

    Returns:
        KRX 휴장일 수입니다. 저장된 적 없는 연도면 0 입니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(COUNT_KRX_HOLIDAYS_SQL, (date(year, 1, 1), date(year, 12, 31), WEEKEND_NAME))
        (count,) = cursor.fetchone()
    return int(count)


def upsert_rows(conn: Any, rows: list[CalendarRow], updated_at: datetime) -> int:
    """행 목록을 한 트랜잭션으로 저장한다. 실패하면 전부 되돌린다.

    연도의 모든 날짜를 덮어쓰므로 KRX 가 휴장일을 취소한 경우에도 개장일로 바로잡힌다.

    Args:
        conn: pymysql 연결 객체입니다.
        rows: 저장할 행 목록입니다.
        updated_at: updated_at 컬럼에 넣을 시각입니다.

    Returns:
        저장한 행 수입니다.
    """
    params = [(row.cal_date, int(row.is_open), row.holiday_name, updated_at.replace(tzinfo=None)) for row in rows]
    try:
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT_SQL, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(params)

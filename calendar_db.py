"""
krx_holiday 테이블 정의와 저장 로직.

KRX 휴장일만 저장한다. 주말은 넣지 않으므로 조회하는 쪽에서 "주말이 아니고 테이블에 없으면 개장일"로 판단한다.
연도 단위로 기존 휴장일을 지우고 새로 받은 목록을 넣으므로 KRX 가 휴장일을 취소하면 다음 실행 때 사라진다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pymysql

TABLE_NAME = "krx_holiday"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    cal_date     DATE        NOT NULL COMMENT '휴장일',
    holiday_name VARCHAR(50) NOT NULL COMMENT '휴장 사유',
    updated_at   DATETIME    NOT NULL COMMENT '마지막 갱신 시각 (KST)',
    PRIMARY KEY (cal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='KRX 휴장일 (주말 제외)'
"""

DELETE_YEAR_SQL = f"DELETE FROM {TABLE_NAME} WHERE cal_date BETWEEN %s AND %s"

INSERT_SQL = f"INSERT INTO {TABLE_NAME} (cal_date, holiday_name, updated_at) VALUES (%s, %s, %s)"

COUNT_YEAR_SQL = f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE cal_date BETWEEN %s AND %s"


@dataclass(frozen=True)
class DbConfig:
    """MySQL 접속 정보."""

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


def connect(config: DbConfig) -> pymysql.connections.Connection:
    """MySQL 에 접속한다. 자동 커밋은 끄고 호출하는 쪽에서 커밋한다.

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
    """krx_holiday 테이블이 없으면 만든다.

    Args:
        conn: pymysql 연결 객체입니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)
    conn.commit()


def year_range(year: int) -> tuple[date, date]:
    """연도의 첫날과 마지막 날을 돌려준다."""
    return date(year, 1, 1), date(year, 12, 31)


def count_holidays(conn: Any, year: int) -> int:
    """이미 저장된 해당 연도의 휴장일 수를 센다.

    Args:
        conn: pymysql 연결 객체입니다.
        year: 대상 연도입니다.

    Returns:
        휴장일 수입니다. 저장된 적 없는 연도면 0 입니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(COUNT_YEAR_SQL, year_range(year))
        (count,) = cursor.fetchone()
    return int(count)


def replace_year_holidays(conn: Any, year: int, holidays: dict[date, str], updated_at: datetime) -> int:
    """해당 연도의 휴장일을 지우고 새 목록으로 넣는다. 한 트랜잭션이라 실패하면 기존 데이터가 그대로 남는다.

    Args:
        conn: pymysql 연결 객체입니다.
        year: 대상 연도입니다.
        holidays: 저장할 휴장일 날짜와 사유입니다. 모두 year 에 속해야 합니다.
        updated_at: updated_at 컬럼에 넣을 시각입니다.

    Returns:
        저장한 휴장일 수입니다.

    Raises:
        ValueError: year 가 아닌 날짜가 섞여 있을 때 발생합니다.
    """
    others = sorted(day.isoformat() for day in holidays if day.year != year)
    if others:
        raise ValueError(f"{year}년이 아닌 날짜가 있습니다: {', '.join(others)}")
    stamp = updated_at.replace(tzinfo=None)
    params = [(day, name, stamp) for day, name in sorted(holidays.items())]
    try:
        with conn.cursor() as cursor:
            cursor.execute(DELETE_YEAR_SQL, year_range(year))
            cursor.executemany(INSERT_SQL, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(params)

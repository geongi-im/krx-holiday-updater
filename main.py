#!/usr/bin/env python3
"""
KRX Holiday Updater

KRX 휴장일 목록을 받아 MySQL krx_holiday 테이블에 연도 단위로 교체 저장한다. 주말은 저장하지 않는다.
- 기본 대상은 올해와 내년(KST 기준)이다.
- 이미 저장된 연도는 KRX 휴장일이 크게 줄어든 응답이면 저장하지 않는다(--force 로 무시).
- 실패하면 텔레그램으로 알리고 종료 코드 1 로 끝난다.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import calendar_db
from krx_holiday import KrxHolidayError, fetch_holidays
from utils.logger_util import LoggerUtil
from utils.telegram_util import TelegramUtil

KST = timezone(timedelta(hours=9))
APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "logs"
LOG_RETENTION_DAYS = 90

# 이미 저장된 연도에서 KRX 휴장일이 이보다 많이 줄어든 응답은 잘린 응답으로 보고 저장하지 않는다.
# 휴장일 취소는 드물고 한 해에 한두 건이므로 그 이상 줄면 KRX 쪽 이상일 가능성이 크다.
MAX_HOLIDAY_DROP = 2

# LoggerUtil 이 핸들러를 붙이는 로거다. import 만으로 로그 파일이 생기지 않도록 핸들러 설정은 main() 에서 한다.
logger = logging.getLogger("MQLogger")


def cleanup_old_logs(today: date | None = None) -> None:
    """LOG_RETENTION_DAYS 보다 오래된 logs/YYYY-MM-DD_log.log 파일을 지운다.

    Args:
        today: 기준 날짜입니다. None 이면 오늘(KST)을 씁니다.
    """
    cutoff = (today or datetime.now(KST).date()) - timedelta(days=LOG_RETENTION_DAYS)
    for path in LOG_DIR.glob("????-??-??_log.log"):
        try:
            log_date = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if log_date < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("오래된 로그 삭제 실패 %s: %s", path.name, exc)


def default_years(now: datetime | None = None) -> list[int]:
    """기본 갱신 대상 연도(올해, 내년)를 KST 기준으로 계산한다.

    Args:
        now: 기준 시각입니다. None 이면 현재 KST 시각을 씁니다.

    Returns:
        [올해, 내년] 목록입니다.
    """
    current = (now or datetime.now(KST)).astimezone(KST)
    return [current.year, current.year + 1]


def send_telegram_alert(text: str) -> None:
    """공통 TelegramUtil 로 알림을 보낸다. 설정이 없거나 전송이 실패해도 예외를 내지 않는다.

    TelegramUtil 은 parse_mode=html 로 보내므로 오류 메시지에 섞인 < > & 때문에 전송이 거부되지 않도록 이스케이프한다.

    Args:
        text: 보낼 메시지입니다.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("텔레그램 설정이 없어 알림을 건너뜁니다.")
        return
    try:
        TelegramUtil().send_message(html.escape(text, quote=False))
    except Exception as exc:
        # 요청 URL 에 봇 토큰이 들어가므로 예외 메시지에 섞여 있으면 가린다.
        logger.error("텔레그램 알림 전송 실패: %s", str(exc).replace(token, "***"))


def check_holiday_drop(year: int, existing: int, received: int) -> str | None:
    """이미 저장된 휴장일 수와 비교해 새로 받은 목록이 비정상적으로 줄었는지 확인한다.

    Args:
        year: 대상 연도입니다.
        existing: DB 에 저장된 휴장일 수입니다.
        received: 이번에 KRX 에서 받은 휴장일 수입니다.

    Returns:
        저장하면 안 되는 경우 그 이유, 괜찮으면 None 입니다.
    """
    if existing - received > MAX_HOLIDAY_DROP:
        return (
            f"{year}년 KRX 휴장일이 {existing}일 -> {received}일로 줄어 저장하지 않았습니다. "
            "KRX 응답을 확인하고 정상이면 --force 로 다시 실행하세요."
        )
    return None


def run(years: Sequence[int], *, dry_run: bool = False, force: bool = False) -> list[str]:
    """연도별로 KRX 휴장일을 받아 저장한다. 한 연도가 실패해도 나머지 연도는 계속 진행한다.

    Args:
        years: 갱신할 연도 목록입니다.
        dry_run: True 면 DB 에 쓰지 않고 받은 결과만 로그로 남깁니다.
        force: True 면 휴장일이 크게 줄어든 응답도 저장합니다.

    Returns:
        실패 내용 목록입니다. 비어 있으면 모두 성공입니다.
    """
    failures: list[str] = []
    holidays_by_year: dict[int, dict[date, str]] = {}

    with requests.Session() as session:
        for year in years:
            try:
                holidays = fetch_holidays(year, session=session)
            except KrxHolidayError as exc:
                logger.error("%s", exc)
                failures.append(str(exc))
                continue
            holidays_by_year[year] = holidays
            logger.info("%d년 KRX 휴장일 %d일 수신", year, len(holidays))
            for day, name in sorted(holidays.items()):
                logger.info("  %s %s", day.isoformat(), name)

    if dry_run:
        logger.info("dry-run 이므로 DB 저장을 건너뜁니다.")
        return failures
    if not holidays_by_year:
        return failures

    try:
        conn = calendar_db.connect(calendar_db.DbConfig.from_env())
    except Exception as exc:
        message = f"DB 접속 실패: {exc}"
        logger.error("%s", message)
        return failures + [message]

    try:
        calendar_db.ensure_table(conn)
        updated_at = datetime.now(KST)
        for year, holidays in holidays_by_year.items():
            try:
                problem = check_holiday_drop(year, calendar_db.count_holidays(conn, year), len(holidays))
                if problem and not force:
                    logger.error("%s", problem)
                    failures.append(problem)
                    continue
                if problem:
                    logger.warning("--force 이므로 저장합니다: %s", problem)
                saved = calendar_db.replace_year_holidays(conn, year, holidays, updated_at)
                logger.info("%d년 휴장일 %d일 저장 완료", year, saved)
            except Exception as exc:
                message = f"{year}년 DB 저장 실패: {exc}"
                logger.error("%s", message)
                failures.append(message)
    except Exception as exc:
        message = f"{calendar_db.TABLE_NAME} 테이블 준비 실패: {exc}"
        logger.error("%s", message)
        failures.append(message)
    finally:
        conn.close()

    return failures


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """명령행 인자를 읽는다."""
    parser = argparse.ArgumentParser(description="KRX 휴장일을 받아 krx_holiday 테이블을 갱신합니다.")
    parser.add_argument("--years", type=int, nargs="+", help="갱신할 연도 목록 (기본: 올해와 내년)")
    parser.add_argument("--dry-run", action="store_true", help="DB 에 저장하지 않고 받은 결과만 출력")
    parser.add_argument("--force", action="store_true", help="저장된 데이터보다 휴장일이 크게 줄어도 저장")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """진입점. 성공하면 0, 하나라도 실패하면 1 을 반환한다."""
    load_dotenv(APP_DIR / ".env")
    LoggerUtil()
    cleanup_old_logs()
    args = parse_args(argv)
    years = sorted(set(args.years)) if args.years else default_years()
    options = "".join(f" ({name})" for name, on in (("dry-run", args.dry_run), ("force", args.force)) if on)
    logger.info("=== KRX 휴장일 갱신 시작: %s%s ===", years, options)

    failures = run(years, dry_run=args.dry_run, force=args.force)

    if failures:
        logger.error("=== KRX 휴장일 갱신 실패 %d건 ===", len(failures))
        if not args.dry_run:
            send_telegram_alert("[krx-holiday-updater] 휴장일 갱신 실패\n\n" + "\n".join(failures))
        return 1
    logger.info("=== KRX 휴장일 갱신 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

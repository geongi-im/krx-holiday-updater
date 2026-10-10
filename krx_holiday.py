"""
KRX 정보데이터시스템(open.krx.co.kr) 휴장일 화면에서 연도별 휴장일 목록을 받아온다.

공식 Open API 가 아니라 휴장일 화면이 내부적으로 쓰는 요청(OTP 발급 -> 조회)을 재현한다.
응답 형식이 바뀌면 빈 목록을 정상 결과로 착각하지 않도록 검증을 엄격하게 한다.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

import requests

OTP_URL = "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx"
DATA_URL = "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx"
HOLIDAY_BLD = "MKD/01/0110/01100305/mkd01100305_01"
PAGE_PATH = "/contents/MKD/01/0110/01100305/MKD01100305.jsp"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": f"https://open.krx.co.kr{PAGE_PATH}",
}

# 평일 휴장일은 매년 13일 이상이다. 공휴일이 주말과 많이 겹치는 해도 있으므로 여유를 두고 8일 미만이면
# 응답이 잘렸거나 형식이 바뀐 것으로 본다. 덜 잘린 응답은 저장 단계에서 기존 데이터와 비교해 막는다.
MIN_HOLIDAYS_PER_YEAR = 8

# 일시적인 네트워크 오류로 바로 실패 알림이 가지 않도록 다시 시도한다.
DEFAULT_RETRIES = 2
DEFAULT_RETRY_WAIT = 5.0

logger = logging.getLogger("MQLogger")


class KrxHolidayError(RuntimeError):
    """KRX 휴장일 조회 또는 응답 검증에 실패했을 때 발생한다."""


def fetch_holidays(
    year: int,
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
    retries: int = DEFAULT_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT,
) -> dict[date, str]:
    """KRX 에서 해당 연도의 휴장일 목록을 받아온다. 실패하면 retries 번까지 다시 시도한다.

    Args:
        year: 조회할 연도입니다.
        session: 재사용할 requests 세션입니다. None 이면 새로 만들고 끝나면 닫습니다.
        timeout: 요청별 타임아웃(초)입니다.
        retries: 실패했을 때 다시 시도할 횟수입니다.
        retry_wait: 다시 시도하기 전 기다릴 시간(초)입니다.

    Returns:
        휴장일 날짜를 키, 휴장 사유를 값으로 하는 dict 입니다. 주말은 KRX 목록에 없으므로 포함되지 않습니다.

    Raises:
        KrxHolidayError: 모든 시도가 실패했을 때 마지막 오류를 그대로 냅니다.
    """
    http = session or requests.Session()
    try:
        for attempt in range(retries + 1):
            try:
                return _fetch_once(year, http, timeout)
            except KrxHolidayError as exc:
                if attempt == retries:
                    raise
                logger.warning("%s - %.0f초 뒤 다시 시도합니다 (%d/%d)", exc, retry_wait, attempt + 1, retries)
                time.sleep(retry_wait)
        raise AssertionError("unreachable")
    finally:
        if session is None:
            http.close()


def _fetch_once(year: int, http: requests.Session, timeout: float) -> dict[date, str]:
    """OTP 발급과 휴장일 조회를 한 번 수행한다."""
    try:
        otp = http.get(OTP_URL, params={"bld": HOLIDAY_BLD, "name": "form"}, headers=REQUEST_HEADERS, timeout=timeout)
        otp.raise_for_status()
        code = otp.text.strip()
        if not code:
            raise KrxHolidayError("KRX OTP 발급 응답이 비어 있습니다.")

        # 잘못된 요청이면 KRX 가 사이트 검색 페이지로 302 리다이렉트하므로 따라가지 않는다.
        resp = http.post(
            DATA_URL,
            data={"search_bas_yy": str(year), "gridTp": "KRX", "pagePath": PAGE_PATH, "code": code},
            headers=REQUEST_HEADERS,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise KrxHolidayError(f"KRX 휴장일 요청 실패 ({year}): {exc}") from exc

    if resp.status_code != 200:
        raise KrxHolidayError(f"KRX 휴장일 응답 상태 코드 이상 ({year}): {resp.status_code}")
    if not resp.text.strip():
        raise KrxHolidayError(f"KRX 휴장일 응답이 비어 있습니다 ({year}). OTP 가 거부되었을 수 있습니다.")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise KrxHolidayError(f"KRX 휴장일 응답이 JSON 이 아닙니다 ({year}): {resp.text[:200]!r}") from exc

    return parse_holidays(payload, year)


def parse_holidays(payload: object, year: int) -> dict[date, str]:
    """KRX 휴장일 응답 JSON 을 검증하고 날짜별 휴장 사유로 변환한다.

    Args:
        payload: KRX 응답을 json 으로 읽은 값입니다.
        year: 요청한 연도입니다. 다른 연도의 날짜가 섞여 있으면 오류로 봅니다.

    Returns:
        휴장일 날짜를 키, 휴장 사유를 값으로 하는 dict 입니다.

    Raises:
        KrxHolidayError: 응답 구조가 다르거나, 날짜를 읽을 수 없거나, 휴장일 수가 너무 적을 때 발생합니다.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("block1"), list):
        raise KrxHolidayError(f"KRX 휴장일 응답에 block1 목록이 없습니다 ({year}).")

    holidays: dict[date, str] = {}
    for row in payload["block1"]:
        if not isinstance(row, dict):
            raise KrxHolidayError(f"KRX 휴장일 행 형식이 다릅니다 ({year}): {row!r}")
        raw_date = str(row.get("calnd_dd", "")).strip()
        try:
            day = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise KrxHolidayError(f"KRX 휴장일 날짜를 읽을 수 없습니다 ({year}): {raw_date!r}") from exc
        if day.year != year:
            raise KrxHolidayError(f"요청 연도와 다른 날짜가 응답에 있습니다 ({year}): {day}")
        holidays[day] = str(row.get("holdy_nm") or "").strip() or "휴장"

    if len(holidays) < MIN_HOLIDAYS_PER_YEAR:
        raise KrxHolidayError(f"KRX 휴장일이 {len(holidays)}일뿐입니다 ({year}). 응답 형식 변경 여부를 확인하세요.")
    return holidays

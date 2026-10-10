from __future__ import annotations

from datetime import date

import pytest
import requests

from krx_holiday import KrxHolidayError, fetch_holidays, parse_holidays

HOLIDAYS_2026 = [
    ("2026-01-01", "신정"), ("2026-02-16", "설날"), ("2026-02-17", "설날"), ("2026-02-18", "설날"),
    ("2026-03-02", "삼일절(대체휴일)"), ("2026-05-01", "근로자의날"), ("2026-05-05", "어린이날"),
    ("2026-05-25", "석가탄신일(대체휴일)"), ("2026-06-03", "임시공휴일"), ("2026-07-17", "제헌절"),
    ("2026-08-17", "광복절(대체휴일)"), ("2026-09-24", "추석"), ("2026-09-25", "추석"),
    ("2026-10-05", "개천절(대체휴일)"), ("2026-10-09", "한글날"), ("2026-12-25", "성탄절"),
    ("2026-12-31", "연말휴장일"),
]


def make_payload(rows: list[tuple[str, str]]) -> dict:
    return {"block1": [{"calnd_dd": d, "dy_tp_cd": "MON", "calnd_dd_dy": d, "kr_dy_tp": "월요일", "holdy_nm": n} for d, n in rows]}


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: object = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, otp: FakeResponse, *data: FakeResponse | Exception) -> None:
        """data 를 여러 개 주면 POST 마다 차례로 돌려주고, 마지막 값은 계속 반복한다."""
        self.otp = otp
        self.data = list(data)
        self.post_kwargs: dict = {}
        self.posts = 0

    def get(self, url, **kwargs):
        return self.otp

    def post(self, url, **kwargs):
        self.post_kwargs = kwargs
        result = self.data[min(self.posts, len(self.data) - 1)]
        self.posts += 1
        if isinstance(result, Exception):
            raise result
        return result


def test_parse_holidays_returns_date_to_name() -> None:
    result = parse_holidays(make_payload(HOLIDAYS_2026), 2026)

    assert len(result) == 17
    assert result[date(2026, 12, 31)] == "연말휴장일"
    assert result[date(2026, 6, 3)] == "임시공휴일"


def test_parse_holidays_fills_blank_name() -> None:
    rows = HOLIDAYS_2026[:-1] + [("2026-12-31", "")]

    assert parse_holidays(make_payload(rows), 2026)[date(2026, 12, 31)] == "휴장"


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"block1": None}, {"output": []}],
)
def test_parse_holidays_rejects_unexpected_structure(payload: object) -> None:
    with pytest.raises(KrxHolidayError, match="block1"):
        parse_holidays(payload, 2026)


def test_parse_holidays_rejects_too_few_rows() -> None:
    with pytest.raises(KrxHolidayError, match="3일뿐"):
        parse_holidays(make_payload(HOLIDAYS_2026[:3]), 2026)


def test_parse_holidays_rejects_other_year() -> None:
    with pytest.raises(KrxHolidayError, match="다른 날짜"):
        parse_holidays(make_payload(HOLIDAYS_2026 + [("2027-01-01", "신정")]), 2026)


def test_parse_holidays_rejects_bad_date() -> None:
    with pytest.raises(KrxHolidayError, match="날짜를 읽을 수 없습니다"):
        parse_holidays(make_payload(HOLIDAYS_2026 + [("20261231", "x")]), 2026)


def test_fetch_holidays_posts_year_and_otp_without_redirect() -> None:
    session = FakeSession(FakeResponse(text="OTPCODE"), FakeResponse(text="{...}", payload=make_payload(HOLIDAYS_2026)))

    result = fetch_holidays(2026, session=session)

    assert len(result) == 17
    assert session.post_kwargs["data"]["search_bas_yy"] == "2026"
    assert session.post_kwargs["data"]["code"] == "OTPCODE"
    assert session.post_kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    ("otp", "data", "message"),
    [
        (FakeResponse(text=""), FakeResponse(), "OTP"),
        (FakeResponse(text="OTP"), FakeResponse(status_code=302, text="moved"), "302"),
        (FakeResponse(text="OTP"), FakeResponse(text=""), "비어 있습니다"),
        (FakeResponse(text="OTP"), FakeResponse(text="<html>"), "JSON"),
        (FakeResponse(text="OTP"), requests.ConnectionError("down"), "요청 실패"),
    ],
)
def test_fetch_holidays_raises_on_bad_response(otp: FakeResponse, data: FakeResponse | Exception, message: str) -> None:
    session = FakeSession(otp, data)

    with pytest.raises(KrxHolidayError, match=message):
        fetch_holidays(2026, session=session, retries=2, retry_wait=0)
    assert session.posts == (0 if message == "OTP" else 3)


def test_fetch_holidays_retries_until_success() -> None:
    ok = FakeResponse(text="{...}", payload=make_payload(HOLIDAYS_2026))
    session = FakeSession(FakeResponse(text="OTP"), requests.ConnectionError("down"), FakeResponse(text=""), ok)

    result = fetch_holidays(2026, session=session, retries=2, retry_wait=0)

    assert len(result) == 17
    assert session.posts == 3


def test_fetch_holidays_without_retry_fails_once() -> None:
    session = FakeSession(FakeResponse(text="OTP"), requests.ConnectionError("down"))

    with pytest.raises(KrxHolidayError, match="요청 실패"):
        fetch_holidays(2026, session=session, retries=0)
    assert session.posts == 1

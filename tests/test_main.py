from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import logging

import pytest

import calendar_db
import main
from krx_holiday import KrxHolidayError

KST = timezone(timedelta(hours=9))


class FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_log_files(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """테스트가 실제 logs/ 폴더에 로그 파일을 만들지 않도록 한다."""
    monkeypatch.setattr(main, "LoggerUtil", lambda: None)
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"conn": FakeConn(), "saved": {}, "fail_years": set(), "existing": {}}

    def fake_replace(conn, year, holidays, updated_at):
        if year in state["fail_years"]:
            raise RuntimeError("write error")
        state["saved"][year] = holidays
        return len(holidays)

    monkeypatch.setattr(calendar_db.DbConfig, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(calendar_db, "connect", lambda config: state["conn"])
    monkeypatch.setattr(calendar_db, "ensure_table", lambda conn: None)
    monkeypatch.setattr(calendar_db, "replace_year_holidays", fake_replace)
    monkeypatch.setattr(calendar_db, "count_holidays", lambda conn, year: state["existing"].get(year, 0))
    return state


def fake_fetch(failing: set[int], count: int = 1):
    """count 일 만큼 1월 첫 평일들을 휴장일로 돌려준다."""

    def _fetch(year, session=None):
        if year in failing:
            raise KrxHolidayError(f"{year} fail")
        weekdays = [d for d in (date(year, 1, 1) + timedelta(days=i) for i in range(40)) if d.weekday() < 5]
        return {d: "휴장" for d in weekdays[:count]}

    return _fetch


def test_default_years_uses_kst() -> None:
    # UTC 12월 31일 16시는 KST 1월 1일 01시
    assert main.default_years(datetime(2026, 12, 31, 16, 0, tzinfo=timezone(timedelta(0)))) == [2027, 2028]
    assert main.default_years(datetime(2026, 10, 10, 9, 0, tzinfo=KST)) == [2026, 2027]


def test_run_saves_every_year(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set()))

    assert main.run([2026, 2027]) == []
    assert sorted(db["saved"]) == [2026, 2027]
    assert db["conn"].closed


def test_run_continues_after_fetch_failure(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch({2026}))

    failures = main.run([2026, 2027])

    assert failures == ["2026 fail"]
    assert sorted(db["saved"]) == [2027]


def test_run_reports_write_failure(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set()))
    db["fail_years"].add(2027)

    failures = main.run([2026, 2027])

    assert len(failures) == 1 and "2027년 DB 저장 실패" in failures[0]
    assert sorted(db["saved"]) == [2026]


def test_run_dry_run_skips_db(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set()))

    assert main.run([2026], dry_run=True) == []
    assert db["saved"] == {}


def test_run_reports_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set()))
    monkeypatch.setattr(calendar_db.DbConfig, "from_env", classmethod(lambda cls: (_ for _ in ()).throw(ValueError("DB_HOST 없음"))))

    assert main.run([2026]) == ["DB 접속 실패: DB_HOST 없음"]


def test_main_alerts_and_returns_1_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(main, "run", lambda years, dry_run=False, force=False: ["2026 fail"])
    monkeypatch.setattr(main, "send_telegram_alert", alerts.append)

    assert main.main(["--years", "2026"]) == 1
    assert alerts and "2026 fail" in alerts[0]


def test_main_returns_0_without_alert_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(main, "run", lambda years, dry_run=False, force=False: [])
    monkeypatch.setattr(main, "send_telegram_alert", alerts.append)

    assert main.main(["--years", "2026"]) == 0
    assert alerts == []


def test_run_blocks_large_holiday_drop(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set(), count=10))
    db["existing"] = {2026: 17, 2027: 12}

    failures = main.run([2026, 2027])

    assert len(failures) == 1 and "17일 -> 10일" in failures[0] and "--force" in failures[0]
    assert sorted(db["saved"]) == [2027]


def test_run_force_saves_large_holiday_drop(monkeypatch: pytest.MonkeyPatch, db: dict) -> None:
    monkeypatch.setattr(main, "fetch_holidays", fake_fetch(set(), count=10))
    db["existing"] = {2026: 17}

    assert main.run([2026], force=True) == []
    assert sorted(db["saved"]) == [2026]


@pytest.mark.parametrize(("existing", "received", "blocked"), [(0, 10, False), (17, 15, False), (17, 14, True), (10, 17, False)])
def test_check_holiday_drop(existing: int, received: int, blocked: bool) -> None:
    assert (main.check_holiday_drop(2026, existing, received) is not None) is blocked


def test_main_passes_force(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(main, "run", lambda years, **kwargs: calls.append(kwargs) or [])

    assert main.main(["--years", "2026", "--force"]) == 0
    assert calls == [{"dry_run": False, "force": True}]


def test_send_telegram_alert_masks_token_in_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    token = "123456:SECRET-TOKEN"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    class FailingTelegram:
        def send_message(self, message):
            raise OSError(f"failed: https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr(main, "TelegramUtil", FailingTelegram)

    with caplog.at_level(logging.ERROR, logger="MQLogger"):
        main.send_telegram_alert("hi")

    assert "텔레그램 알림 전송 실패" in caplog.text
    assert token not in caplog.text
    assert "/bot***/sendMessage" in caplog.text


def test_send_telegram_alert_skips_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(main, "TelegramUtil", lambda: pytest.fail("전송하면 안 됩니다"))

    main.send_telegram_alert("hi")


def test_send_telegram_alert_escapes_html(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    class FakeTelegram:
        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(main, "TelegramUtil", FakeTelegram)

    main.send_telegram_alert("응답이 JSON 이 아닙니다: '<html>' & <urllib3 object>")

    assert sent == ["응답이 JSON 이 아닙니다: '&lt;html&gt;' &amp; &lt;urllib3 object&gt;"]


def test_cleanup_old_logs_removes_only_old_dated_files(tmp_path) -> None:
    names = ("2026-07-11_log.log", "2026-07-12_log.log", "2026-10-10_log.log", "2026-01-01.log", "cron.log")
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")

    main.cleanup_old_logs(date(2026, 10, 10))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-01-01.log", "2026-07-12_log.log", "2026-10-10_log.log", "cron.log"]

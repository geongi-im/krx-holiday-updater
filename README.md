# krx-holiday-updater

KRX 휴장일을 받아 mqway DB 의 `krx_calendar` 테이블에 저장하는 갱신 전용 프로젝트입니다.
다른 프로젝트는 각자 휴장일을 계산하지 않고 이 테이블을 조회합니다.

## 구성

```
main.py              진입점. 연도별로 받아서 저장하고, 실패하면 텔레그램으로 알림
krx_holiday.py       KRX 휴장일 조회와 응답 검증 (실패 시 재시도)
calendar_db.py       krx_calendar 테이블 정의, 연도 전체 날짜 생성, upsert
utils/logger_util.py 다른 프로젝트와 같은 공통 로그 유틸 (MQLogger)
tests/               단위 테스트 (KRX 와 DB 는 가짜 객체로 대체)
```

## 데이터 출처

KRX 정보데이터시스템 휴장일 화면(`open.krx.co.kr`, 시장정보 > 휴장일)이 내부적으로 쓰는 요청을 그대로 재현합니다.
로그인이나 API 키는 필요 없습니다.

- 공휴일뿐 아니라 연말 휴장일(12/31), 근로자의 날, 선거일, 임시공휴일 등 거래소 휴장일이 모두 들어 있습니다.
- KRX 는 먼 미래 연도도 목록을 돌려주지만, 이후 지정되는 임시공휴일은 KRX 가 목록을 고친 뒤에야 반영됩니다.
  그래서 매일 다시 받아 덮어씁니다.
- 공식 Open API 가 아니므로 형식이 바뀔 수 있습니다. 다음 경우에는 저장하지 않고 실패로 처리합니다(기존 데이터는 그대로 남습니다).
  - 응답 구조가 다르거나 휴장일이 8일 미만일 때
  - 이미 저장된 연도인데 KRX 휴장일이 3일 이상 줄었을 때 (잘린 응답 방지, 확인 후 `--force` 로 저장)
- 요청이 실패하면 5초 간격으로 2번 더 시도합니다.

## 테이블

```sql
CREATE TABLE IF NOT EXISTS krx_calendar (
    cal_date     DATE        NOT NULL COMMENT '날짜',
    is_open      TINYINT(1)  NOT NULL COMMENT '개장 여부 (1: 개장, 0: 휴장)',
    holiday_name VARCHAR(50) NULL     COMMENT '휴장 사유 (개장일은 NULL, 주말은 주말)',
    updated_at   DATETIME    NOT NULL COMMENT '마지막 갱신 시각 (KST)',
    PRIMARY KEY (cal_date),
    KEY idx_is_open_date (is_open, cal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='KRX 개장/휴장 달력';
```

테이블은 첫 실행 때 자동으로 만들어집니다.
공휴일만이 아니라 연도의 **모든 날짜**를 저장합니다. 조회 결과가 없으면 "개장일"이 아니라 "데이터 없음"이라는 뜻이므로,
조회하는 쪽에서는 행이 없을 때 개장일로 가정하지 말고 대체 수단을 쓰거나 실패로 처리해야 합니다.

## 조회 예시

```sql
-- 특정 날짜 개장 여부 (행이 없으면 데이터 없음)
SELECT is_open, holiday_name FROM krx_calendar WHERE cal_date = '2026-12-31';

-- 직전 개장일
SELECT MAX(cal_date) FROM krx_calendar WHERE cal_date < '2026-10-12' AND is_open = 1;

-- 다음 개장일
SELECT MIN(cal_date) FROM krx_calendar WHERE cal_date > '2026-10-08' AND is_open = 1;

-- 마지막 갱신 시각 (갱신이 멈췄는지 확인용)
SELECT MAX(updated_at) FROM krx_calendar;
```

같은 MySQL 서버의 다른 DB(`stock` 등)를 쓰는 프로젝트는 `mqway.krx_calendar` 처럼 DB 이름을 붙여 조회합니다.
해당 계정에 `mqway` 조회 권한이 있어야 합니다. `mqway` 계정은 `mqway` 와 `stock` 모두 권한이 있습니다.

## DB

mqway 프로젝트(`MQ/mqway`)의 Docker Compose 로 띄우는 MySQL 5.7 의 `mqway` DB 를 씁니다.

- 서버: mqway 가 떠 있으면 그대로 씁니다. `DB_HOST=localhost`
- 로컬(Windows): Docker Desktop 을 켜고 mqway 의 mysql 서비스만 올리면 됩니다.

```bash
cd MQ/mqway
docker compose -f docker-compose.local.yml up -d mysql
```

접속 계정과 비밀번호는 mqway `.env` 의 `MYSQL_USER`, `MYSQL_PASSWORD` 와 같습니다.

## 설치

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                            # DB 접속 정보와 텔레그램 설정 입력
```

`.env` 항목:

| 키 | 설명 |
|---|---|
| `DB_HOST`, `DB_PORT` | MySQL 주소 (기본 `localhost`, `3306`) |
| `DB_NAME` | `mqway` |
| `DB_USER`, `DB_PASSWORD` | mqway `.env` 의 MySQL 계정 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 실패 알림용. 비워두면 알림을 건너뜁니다 |

## 실행

```bash
python main.py                       # 올해와 내년(KST 기준) 갱신
python main.py --years 2024 2025     # 지정한 연도만 갱신 (과거 연도 채우기 등)
python main.py --dry-run             # DB 에 쓰지 않고 받은 결과만 출력
python main.py --force               # 휴장일이 크게 줄어든 응답도 저장 (KRX 목록을 직접 확인한 뒤에만)
```

- 연도별로 따로 처리하므로 한 연도가 실패해도 나머지 연도는 저장됩니다.
- 하나라도 실패하면 텔레그램(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)으로 알리고 종료 코드 1 로 끝납니다.
- 로그는 다른 프로젝트와 같은 `utils/logger_util.py` 로 남깁니다. 파일은 `logs/YYYY-MM-DD_log.log`(UTF-8) 이고
  90일이 지난 파일은 실행할 때 지웁니다. 콘솔에도 같은 내용이 출력됩니다.
- 텔레그램 전송이 실패해도 로그에는 봇 토큰을 `***` 로 가려서 남깁니다.

## 자동 실행

다른 프로젝트보다 먼저 돌도록 매일 아침에 실행합니다. 주말에도 실행해서 갱신이 끊기지 않게 합니다.
실행 시각에 mqway 의 MySQL 이 떠 있어야 합니다. 꺼져 있으면 DB 접속 실패로 알림이 갑니다.

```cron
CRON_TZ=Asia/Seoul
0 6 * * * cd /path/to/krx-holiday-updater && .venv/bin/python main.py >> logs/cron.log 2>&1
```

## 테스트

```bash
python -m pytest
```

"""환경설정 (.env 파일에서 읽음)"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _getenv(key, default=None):
    """값 뒤에 붙은 '# 설명' 주석과 공백을 떼고 읽는다 (빈 값 + 주석 대응)"""
    v = _os_getenv(key, default)
    if isinstance(v, str):
        v = re.split(r"(?:^|\s)#", v, maxsplit=1)[0].strip()
    return v


_os_getenv = os.getenv

DART_API_KEY = _getenv("DART_API_KEY", "")
TELEGRAM_BOT_TOKEN = _getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _getenv("TELEGRAM_CHAT_ID", "")          # 팀 단톡방(그룹) chat_id
SEND_DM_TO_ANALYST = _getenv("SEND_DM_TO_ANALYST", "false").lower() == "true"

ANTHROPIC_API_KEY = _getenv("ANTHROPIC_API_KEY", "")         # 선택: 있으면 AI 요약
ANTHROPIC_MODEL = _getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

POLL_INTERVAL_SEC = int(_getenv("POLL_INTERVAL_SEC", "300"))   # KIND 밸류업 목록 조회 주기 (기본 5분)
DART_INTERVAL_SEC = int(_getenv("DART_INTERVAL_SEC", "600"))   # DART 백업 조회 주기 (기본 10분)
NIGHT_INTERVAL_SEC = int(_getenv("NIGHT_INTERVAL_SEC", "600")) # 야간(22~06시) 조회 주기
BACKFILL_DAYS = int(_getenv("BACKFILL_DAYS", "30"))           # 최초 실행 시 알림 없이 불러올 과거 기간
RECHECK_HOURS = int(_getenv("RECHECK_HOURS", "48"))           # 첨부 미확인 건 재확인 기간
# all: 커버리지 외 종목도 리포트에 포함 / coverage: 커버리지 종목만
ALERT_SCOPE = _getenv("ALERT_SCOPE", "all").lower()

# 정기 리포트: 영업일 지정 시각에 직전 리포트 이후 공시를 묶어서 발송
DIGEST_TIMES = [t.strip() for t in _getenv("DIGEST_TIMES", "09:00,15:30").split(",") if t.strip()]
DIGEST_GRACE_MIN = int(_getenv("DIGEST_GRACE_MIN", "180"))  # 서버가 늦게 켜져도 이 시간 안이면 발송
INSTANT_ALERT = _getenv("INSTANT_ALERT", "false").lower() == "true"  # 실시간 개별 알림도 보낼지
PUB_BUTTONS = _getenv("PUB_BUTTONS", "true").lower() == "true"   # 알림에 '계획발간/이행발간' 체크 버튼
PENDING_REMIND = _getenv("PENDING_REMIND", "true").lower() == "true"  # 리포트에 미착수 누적 표시

# 한국투자증권 Open API (시총·종가)
KIS_APP_KEY = _getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = _getenv("KIS_APP_SECRET", "")
KIS_BASE_URL = _getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
# 작성 대상: 발표일 시총 범위 (억원)
CAP_MIN_EOK = float(_getenv("CAP_MIN_EOK", "300"))
CAP_MAX_EOK = float(_getenv("CAP_MAX_EOK", "5000"))

DASHBOARD_USER = _getenv("DASHBOARD_USER", "team")
DASHBOARD_PASSWORD = _getenv("DASHBOARD_PASSWORD", "")        # 비우면 로그인 없음
DASHBOARD_URL = _getenv("DASHBOARD_URL", "")                  # 알림에 넣을 대시보드 주소

DB_PATH = Path(_getenv("DB_PATH", BASE_DIR / "data" / "valueup.db"))
# 커버리지 원본: 공유폴더의 엑셀/CSV 경로 또는 구글시트 'CSV로 게시' URL
COVERAGE_FILE = Path(_getenv("COVERAGE_FILE", BASE_DIR / "data" / "coverage.xlsx"))
COVERAGE_URL = _getenv("COVERAGE_URL", "")
HOLIDAY_FILE = Path(_getenv("HOLIDAY_FILE", BASE_DIR / "data" / "krx_holidays.txt"))
# 상대경로는 프로그램 폴더 기준
COVERAGE_FILE, HOLIDAY_FILE = [p if p.is_absolute() else BASE_DIR / p for p in (COVERAGE_FILE, HOLIDAY_FILE)]
FILE_DIR = Path(_getenv("FILE_DIR", BASE_DIR / "data" / "files"))

DOWNLOAD_FILES = _getenv("DOWNLOAD_FILES", "true").lower() == "true"   # 첨부 PDF 서버 저장 여부
SITE_DIR = Path(_getenv("SITE_DIR", BASE_DIR / "docs"))                 # 정적 대시보드 출력 폴더

# 기업가치 제고 공시로 판단할 공시명 패턴 (공백 무시)
REPORT_KEYWORD = "기업가치제고"
# 인정 첨부파일 확장자
VALID_EXTS = ("pdf", "hwp", "hwpx", "doc", "docx")
# 첨부 목록에 표시할 파일 확장자 (홈페이지·관련공시 링크 제외용)
FILE_EXTS = VALID_EXTS + ("xls", "xlsx", "ppt", "pptx", "zip", "txt")

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

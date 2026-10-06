"""공유 상태 파일(data/pub_status.json): 종목별 발간 체크 + 담당자 수정

형식: {"pub": {종목코드: {plan, impl, by, at}}, "assign": {종목코드: {analyst, by, at}}}
- 사이트에서 수정 → GitHub API로 이 파일을 직접 수정 (각자 토큰 1회 등록)
- 텔레그램 버튼 → 실행 때 반영 후 이 파일에 기록
- 실행할 때 파일 → DB(meta)로 읽어 알림·엑셀·사이트에 사용
"""
import json
from datetime import datetime
from pathlib import Path

from . import config, db

PATH = Path(config.BASE_DIR) / "data" / "pub_status.json"
NONE = "-"   # 담당 '커버리지 외'로 지정


def _norm(d) -> dict:
    d = d if isinstance(d, dict) else {}
    return {"pub": dict(d.get("pub") or {}), "assign": dict(d.get("assign") or {})}


def load_file() -> dict:
    try:
        return _norm(json.loads(PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _norm({})


def state() -> dict:
    try:
        return _norm(json.loads(db.get_meta("shared_state") or "{}"))
    except ValueError:
        return _norm({})


def _save_state(st):
    db.set_meta("shared_state", json.dumps(st, ensure_ascii=False))


def pull():
    """파일 → DB"""
    _save_state(load_file())


def push() -> bool:
    """DB → 파일 (바뀐 경우만)"""
    st = state()
    if st == load_file():
        return False
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return True


def set_pub(code, field, value, by=""):
    st = state()
    cur = st["pub"].get(code, {"plan": False, "impl": False})
    cur[field] = bool(value)
    cur["by"], cur["at"] = by, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["pub"][code] = cur
    _save_state(st)
    return cur


def assigned(code) -> list[str] | None:
    """담당 직접 지정값 (없으면 None → 엑셀 커버리지 사용)"""
    a = state()["assign"].get(code or "")
    if not a or not a.get("analyst"):
        return None
    return [] if a["analyst"] == NONE else [a["analyst"]]


def impl_codes() -> set:
    with db.conn() as c:
        return {r["stock_code"] for r in c.execute(
            "SELECT DISTINCT stock_code FROM filings WHERE valid=1 AND kind_type LIKE '이행%' AND stock_code IS NOT NULL")}


def company_status(code, implset=None) -> str:
    """1) 계획 미체크 → 미발간  2) 계획✓ + 이행 공시 있음 → 이행 미발간
       3) 계획✓ + 이행 공시 없음 → 이행 미공시  4) 계획✓ + 이행✓ → 발간 완료"""
    p = state()["pub"].get(code or "", {})
    if not p.get("plan"):
        return "미발간"
    if p.get("impl"):
        return "발간 완료"
    implset = impl_codes() if implset is None else implset
    return "이행 미발간" if code in implset else "이행 미공시"

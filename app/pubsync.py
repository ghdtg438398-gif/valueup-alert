"""공유 상태 파일(data/pub_status.json): 종목별 발간 체크 + 담당자 수정

형식: {"pub": {종목코드: {plan, impl, by, at}}, "assign": {종목코드: {analyst, by, at}},
       "exclude": {접수번호: {name, by, at, reason}},   ← 이상한 공시를 작성 대상에서 제외 (복원 = 삭제)
       "defer": {종목코드:단계: {year, reason, by, at, name, from}},   ← 보고서 작성 연도 이월 (단계 plan/impl)
       "add": {검색어: {query, by, at, done, result}}}   ← 1년보다 오래된 공시 개별 추가 (다음 실행 때 DART에서 불러옴)
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
    return {"pub": dict(d.get("pub") or {}), "assign": dict(d.get("assign") or {}),
            "exclude": dict(d.get("exclude") or {}), "defer": dict(d.get("defer") or {}),
            "add": dict(d.get("add") or {})}


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


def excluded_ids() -> set:
    """사이트에서 '제외'한 공시 접수번호"""
    return set(state()["exclude"])


def set_exclude(acptno, on=True, by="", name="", reason=""):
    st = state()
    if on:
        st["exclude"][acptno] = {"name": name, "by": by, "reason": reason,
                                 "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    else:
        st["exclude"].pop(acptno, None)
    _save_state(st)


def impl_codes() -> set:
    ex = excluded_ids()
    with db.conn() as c:
        return {r["stock_code"] for r in c.execute(
            "SELECT acptno, stock_code FROM filings WHERE valid=1 AND superseded_by IS NULL "
            "AND kind_type LIKE '이행%' AND stock_code IS NOT NULL") if r["acptno"] not in ex}


def company_status(code, implset=None) -> str:
    """0) 발간 거절 체크 → 발간 거절
       1) 계획 미체크 → 미발간  2) 계획✓ + 이행 공시 있음 → 이행 미발간
       3) 계획✓ + 이행 공시 없음 → 이행 미공시  4) 계획✓ + 이행✓ → 발간 완료"""
    p = state()["pub"].get(code or "", {})
    if p.get("reject"):
        return "발간 거절"
    if not p.get("plan"):
        return "미발간"
    if p.get("impl"):
        return "발간 완료"
    implset = impl_codes() if implset is None else implset
    return "이행 미발간" if code in implset else "이행 미공시"



def merge_into_remote(remote_path) -> dict:
    """저장 직전: 저장소의 최신 상태 파일(사람이 사이트에서 고친 것)을 기준으로,
    이번 실행에서 봇이 바꾼 것(과거 공시 추가 결과)만 얹어서 data/pub_status.json 에 씀.
    → 줄 단위 git 병합으로 JSON이 섞이는 일 방지"""
    try:
        remote = _norm(json.loads(Path(remote_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        remote = load_file()
    mine = state()
    for k, v in mine["add"].items():
        if v.get("done") and k in remote["add"] and not remote["add"][k].get("done"):
            remote["add"][k] = v
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(remote, ensure_ascii=False, indent=1), encoding="utf-8")
    return remote

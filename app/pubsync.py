"""발간 체크 공유 파일(data/pub_status.json) ↔ DB

- 사이트에서 체크 → GitHub API로 이 파일을 직접 수정 (팀원 각자 토큰 1회 등록)
- 텔레그램 버튼 체크 → 실행 때 DB에 반영 후 이 파일에도 기록
- 파일이 기준(마지막에 체크한 값이 이김)
"""
import json
from pathlib import Path

from . import config, db

PATH = Path(config.BASE_DIR) / "data" / "pub_status.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pull() -> int:
    """파일 → DB"""
    n = 0
    for acptno, v in load().items():
        f = db.get_filing(acptno)
        if not f:
            continue
        p, i = int(bool(v.get("plan"))), int(bool(v.get("impl")))
        if (f["pub_plan"] or 0) != p or (f["pub_impl"] or 0) != i:
            db.update_filing(acptno, pub_plan=p, pub_impl=i, pub_by=v.get("by"), pub_at=v.get("at"),
                             status="완료" if (p or i) else "미착수")
            n += 1
    return n


def push() -> bool:
    """DB → 파일 (내용이 바뀐 경우만 기록)"""
    cur = load()
    new = dict(cur)
    with db.conn() as c:
        for r in c.execute("SELECT acptno, pub_plan, pub_impl, pub_by, pub_at FROM filings "
                           "WHERE COALESCE(pub_plan,0)=1 OR COALESCE(pub_impl,0)=1 OR pub_at IS NOT NULL"):
            new[r["acptno"]] = {"plan": bool(r["pub_plan"]), "impl": bool(r["pub_impl"]),
                                "by": r["pub_by"] or "", "at": r["pub_at"] or ""}
    if new == cur:
        return False
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(new, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return True

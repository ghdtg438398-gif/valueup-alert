"""애널리스트별 커버리지(종목 풀) 엑셀/CSV 불러오기

지원 형식
1) 세로형(권장): 열 = 애널리스트 | 종목코드 | 종목명   (종목코드나 종목명 중 하나만 있어도 됨)
2) 가로형: 첫 행 = 애널리스트 이름, 그 아래 = 종목명 또는 종목코드
"""
import json
import re
from pathlib import Path

import pandas as pd

from . import dart, db

A_COLS = ("애널리스트", "담당자", "담당", "analyst", "이름", "작성자")
C_COLS = ("종목코드", "코드", "단축코드", "ticker", "stock_code", "code")
N_COLS = ("종목명", "회사명", "기업명", "name", "corp_name", "종목")


def _norm_code(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip().upper()
    s = re.sub(r"\.0$", "", s)
    s = s.lstrip("A") if re.fullmatch(r"A\d{6}", s) else s
    return s.zfill(6) if s.isdigit() else s


def _pick(cols, cands):
    low = {str(c).strip().lower(): c for c in cols}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    return None


HONOR = re.compile(r"^\s*([가-힣A-Za-z]{2,10})\s*(위원님|팀장님|위원|팀장|연구원님|연구원|님)\s*$")


def load_blocks(path: Path) -> list[tuple] | None:
    """팀 취합 양식: 'OOO 위원님' 머리글 아래 '종목 코드 | … | 기업명 | 시가총액 | Note' 블록이
    가로로 나열된 시트. 블록을 하나도 못 찾으면 None"""
    try:
        frames = pd.read_excel(path, sheet_name=None, dtype=str, header=None)
    except Exception:  # noqa: BLE001
        return None
    rows = []
    for _, df in frames.items():
        vals = df.values
        for r in range(len(vals)):
            for c in range(len(vals[r])):
                m = HONOR.match(str(vals[r][c])) if isinstance(vals[r][c], str) else None
                if not m:
                    continue
                name = m.group(1)
                # 바로 아래 몇 줄 안에서 '종목 코드' 머리글 찾기
                hdr = next((rr for rr in range(r + 1, min(r + 4, len(vals)))
                            if isinstance(vals[rr][c], str) and "종목" in vals[rr][c] and "코드" in vals[rr][c]), None)
                if hdr is None:
                    continue
                heads = {str(vals[hdr][cc]).replace(" ", ""): cc for cc in range(c, min(c + 7, len(vals[hdr])))
                         if isinstance(vals[hdr][cc], str)}
                ncol = next((cc for h, cc in heads.items() if "기업명" in h or "종목명" in h), None)
                notecol = next((cc for h, cc in heads.items() if h.lower().startswith("note") or "비고" in h), None)
                blank = 0
                for rr in range(hdr + 1, len(vals)):
                    code = _norm_code(vals[rr][c])
                    if not re.fullmatch(r"[0-9A-Z]{6}", code):
                        blank = blank + 1 if not code else 0
                        if blank >= 10 or (isinstance(vals[rr][c], str) and HONOR.match(vals[rr][c])):
                            break   # 빈 줄 10개 연속이거나 다음 블록 머리글이면 끝
                        continue
                    blank = 0
                    nm = vals[rr][ncol] if ncol is not None and isinstance(vals[rr][ncol], str) else ""
                    note = vals[rr][notecol] if notecol is not None and isinstance(vals[rr][notecol], str) else ""
                    rows.append((name, code, nm.strip(), note.strip()))
    return rows or None


def load(path: Path, corp_map: dict | None = None) -> tuple[list[tuple], list[str]]:
    """반환: (rows[(analyst, code, name)], warnings)"""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        blocks = load_blocks(path)
        if blocks:
            from .teamcfg import alias
            blocks = [(alias(a), c, n, nt) for a, c, n, nt in blocks]
            rows = list({(a, c): (a, c, n) for a, c, n, _ in blocks}.values())
            notes = {(a, c): nt for a, c, _, nt in blocks if nt}
            db.set_meta("coverage_notes", json.dumps({f"{a}|{c}": v for (a, c), v in notes.items()},
                                                     ensure_ascii=False)) if _db_ready() else None
            return rows, []
    if path.suffix.lower() in (".csv", ".txt"):
        frames = {"csv": pd.read_csv(path, dtype=str, encoding_errors="replace")}
    else:
        frames = pd.read_excel(path, sheet_name=None, dtype=str)
    corp_map = corp_map or {}
    by_code = {v["stock_code"]: v["corp_name"] for v in corp_map.values()}
    rows, warns = [], []

    def resolve(analyst, code, name):
        code, name = _norm_code(code), (name or "").strip() if isinstance(name, str) else ""
        if not (re.fullmatch(r"[0-9A-Z]{6}", code or "") and sum(ch.isdigit() for ch in code) >= 4):
            # 코드 칸에 이름이 들어온 경우
            if code and not name:
                name = code
            code = ""
        if not code and name:
            hit = corp_map.get(dart.norm_name(name))
            if hit:
                code = hit["stock_code"]
            else:
                warns.append(f"[{analyst}] '{name}' 종목코드를 찾지 못함")
                return
        if code:
            rows.append((analyst.strip(), code, name or by_code.get(code, "")))

    for sheet, df in frames.items():
        df = df.dropna(how="all")
        if df.empty:
            continue
        a, c, n = _pick(df.columns, A_COLS), _pick(df.columns, C_COLS), _pick(df.columns, N_COLS)
        if a and (c or n):
            for _, r in df.iterrows():
                an = r.get(a)
                if isinstance(an, str) and an.strip():
                    resolve(an, r.get(c) if c else "", r.get(n) if n else "")
        elif not a and (c or n) and sheet not in ("csv", "Sheet1"):
            # 시트 이름 = 애널리스트
            for _, r in df.iterrows():
                resolve(sheet, r.get(c) if c else "", r.get(n) if n else "")
        else:
            # 가로형: 열 이름 = 애널리스트
            for col in df.columns:
                if str(col).startswith("Unnamed"):
                    continue
                for v in df[col].dropna():
                    v = str(v).strip()
                    if not v:
                        continue
                    if re.fullmatch(r"A?\d{6}(\.0)?|\d{1,5}(\.0)?", v):
                        resolve(str(col), v, "")
                    else:
                        resolve(str(col), "", v)
    # 중복 제거
    rows = list({(r[0], r[1]): r for r in rows}.values())
    return rows, warns


def _db_ready():
    try:
        db.get_meta("x")
        return True
    except Exception:  # noqa: BLE001
        return False


# ======================================================================
# 원본 파일 동기화: 공유폴더 엑셀 / 구글시트 URL → DB
# ======================================================================
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import requests  # noqa: E402

from . import config, db  # noqa: E402

log = logging.getLogger(__name__)
TEMPLATE_ROWS = [["홍길동", "005930", "삼성전자"], ["홍길동", "000660", "SK하이닉스"], ["김철수", "388050", "지투파워"]]


def write_template(path: Path, rows=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows or TEMPLATE_ROWS, columns=["애널리스트", "종목코드", "종목명"])
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="커버리지")
        ws = w.sheets["커버리지"]
        ws.column_dimensions["A"].width, ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 14, 12, 22
        for cell in ws["B"]:
            cell.number_format = "@"   # 종목코드 앞자리 0 유지


def source_label() -> str:
    return config.COVERAGE_URL or str(config.COVERAGE_FILE)


def _fetch_source() -> tuple[bytes | None, str]:
    if config.COVERAGE_URL:
        r = requests.get(config.COVERAGE_URL, timeout=30, headers=config.HTTP_HEADERS)
        r.raise_for_status()
        return r.content, (".xlsx" if r.content[:2] == b"PK" else ".csv")
    p = config.COVERAGE_FILE
    if not p.exists():
        # 처음: DB에 있던 커버리지(또는 예시)로 원본 파일을 만들어 둔다
        rows = [[c["analyst"], c["stock_code"], c["corp_name"]] for c in db.coverage_list()]
        write_template(p, rows or None)
        log.info("커버리지 원본 파일 생성: %s", p)
    return p.read_bytes(), p.suffix.lower() or ".xlsx"


def sync(corp_map: dict | None = None, force: bool = False) -> dict:
    """원본이 바뀌었으면 DB 커버리지를 교체. 반환: 상태 dict"""
    if config.COVERAGE_URL and not force:
        last = db.get_meta("coverage_checked_at")
        if last and datetime.strptime(last, "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(minutes=10):
            return status()
    try:
        raw, suffix = _fetch_source()
    except Exception as e:  # noqa: BLE001
        db.set_meta("coverage_error", f"{db.now()} 원본 읽기 실패: {e}")
        log.warning("커버리지 원본 읽기 실패: %s", e)
        return status()
    db.set_meta("coverage_checked_at", db.now())
    h = hashlib.sha1(raw).hexdigest()
    if h == db.get_meta("coverage_hash") and not force:
        return status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
    try:
        rows, warns = load(Path(tmp.name), corp_map)
    except Exception as e:  # noqa: BLE001
        # 엑셀이 열려 있어 잠긴 경우 등 → 다음 주기에 재시도 (기존 커버리지 유지)
        db.set_meta("coverage_error", f"{db.now()} 파일 해석 실패: {e}")
        return status()
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if not rows:
        db.set_meta("coverage_error", f"{db.now()} 종목이 0개라 반영하지 않음 (형식 확인 필요)")
        return status()
    db.replace_coverage(rows)
    with db.conn() as c:  # 원본에서 빠진 애널리스트 정리
        c.execute("DELETE FROM analysts WHERE name NOT IN (SELECT DISTINCT analyst FROM coverage) "
                  "AND (telegram_chat_id IS NULL OR telegram_chat_id='')")
    # 코드표 없이 이름 매칭에 실패한 행이 있으면 다음 주기에 다시 시도하도록 해시를 비워 둔다
    db.set_meta("coverage_hash", h if (corp_map or not warns) else "")
    db.set_meta("coverage_loaded_at", db.now())
    db.set_meta("coverage_count", len(rows))
    db.set_meta("coverage_warnings", json.dumps(warns, ensure_ascii=False))
    db.set_meta("coverage_error", "")
    log.info("커버리지 반영: %d개 (확인 필요 %d)", len(rows), len(warns))
    return status()


def status() -> dict:
    return {
        "source": source_label(),
        "is_url": bool(config.COVERAGE_URL),
        "loaded_at": db.get_meta("coverage_loaded_at"),
        "checked_at": db.get_meta("coverage_checked_at"),
        "count": db.get_meta("coverage_count"),
        "warnings": json.loads(db.get_meta("coverage_warnings") or "[]"),
        "error": db.get_meta("coverage_error") or "",
    }

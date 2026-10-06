"""SQLite 저장소"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    acptno          TEXT PRIMARY KEY,     -- KIND 접수번호 (YYYYMMDD00xxxx)
    rcept_no        TEXT,                 -- DART 접수번호 (YYYYMMDD90xxxx)
    corp_code       TEXT,
    corp_name       TEXT,
    stock_code      TEXT,
    market          TEXT,                 -- 유가/코스닥/코넥스
    report_nm       TEXT,
    rcept_dt        TEXT,                 -- YYYYMMDD
    disclosed_at    TEXT,                 -- YYYY-MM-DD HH:MM (KIND 공시시각)
    source          TEXT,                 -- kind/dart
    detected_at     TEXT,
    is_correction   INTEGER DEFAULT 0,
    valid           INTEGER DEFAULT 0,    -- 인정 첨부(PDF/HWP/DOC) 존재 여부
    attachments     TEXT DEFAULT '[]',    -- [{name, url, ext, local}]
    plan_name       TEXT,
    main_content    TEXT,
    high_dividend   TEXT,
    pbr_plan        TEXT,
    decision_date   TEXT,
    kind_type       TEXT,                 -- 계획/이행/이행+계획/재공시
    related         TEXT DEFAULT '[]',    -- 본문 '※ 관련공시'
    digest_at       TEXT,                 -- 정기 리포트에 포함된 시각
    body_text       TEXT,
    ai_summary      TEXT,
    alerted_at      TEXT,
    check_count     INTEGER DEFAULT 0,
    last_checked    TEXT,
    last_error      TEXT,
    status          TEXT DEFAULT '미착수', -- 미착수/작성중/완료/제외
    status_by       TEXT,
    status_note     TEXT,
    status_at       TEXT
);
CREATE TABLE IF NOT EXISTS coverage (
    analyst     TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    corp_name   TEXT,
    PRIMARY KEY (analyst, stock_code)
);
CREATE TABLE IF NOT EXISTS analysts (
    name              TEXT PRIMARY KEY,
    telegram_chat_id  TEXT,
    telegram_username TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS krx_daily (basis TEXT, code TEXT, close INTEGER, mktcap INTEGER, shares INTEGER,
                                      PRIMARY KEY (basis, code));
CREATE TABLE IF NOT EXISTS prices (code TEXT, basis TEXT, close INTEGER, dt TEXT, PRIMARY KEY (code, basis));
CREATE TABLE IF NOT EXISTS shares (corp_code TEXT PRIMARY KEY, shares INTEGER, basis TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS digests (slot TEXT PRIMARY KEY, sent_at TEXT, n INTEGER);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def conn():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


MIGRATIONS = {"superseded_by": "TEXT",   # 정정공시로 대체된 원본이면 정정공시 접수번호
              "orig_disclosed_at": "TEXT",
              "ann_shares": "INTEGER", "ann_src": "TEXT",   # 발표일 상장주식수, 시총 출처   # 정정공시면 최초 공시 시각 (발표일 시총 기준)
              "kind_type": "TEXT", "related": "TEXT DEFAULT '[]'", "digest_at": "TEXT",
              # 발표일 시총
              "ann_close": "INTEGER", "ann_cap": "INTEGER", "ann_dt": "TEXT", "ann_final": "INTEGER DEFAULT 0",
              # 발간 체크 (계획/이행)
              "pub_plan": "INTEGER DEFAULT 0", "pub_impl": "INTEGER DEFAULT 0", "pub_by": "TEXT", "pub_at": "TEXT"}


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(filings)")}
        for col, typ in MIGRATIONS.items():
            if col not in cols:
                c.execute(f"ALTER TABLE filings ADD COLUMN {col} {typ}")
        # 예전 방식(종가×현재 주식수)으로 확정된 시총은 새 방식으로 다시 계산
        if not c.execute("SELECT 1 FROM meta WHERE k='cap_v2'").fetchone():
            c.execute("UPDATE filings SET ann_final=0 WHERE ann_src IS NULL")
            c.execute("INSERT OR REPLACE INTO meta VALUES('cap_v2','1')")


# ---------- meta ----------
def get_meta(k, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_meta(k, v):
    with conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------- filings ----------
def filing_exists(acptno) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM filings WHERE acptno=?", (acptno,)).fetchone() is not None


def insert_filing(d: dict):
    cols = ",".join(d.keys())
    qs = ",".join("?" * len(d))
    with conn() as c:
        c.execute(f"INSERT OR IGNORE INTO filings({cols}) VALUES({qs})", tuple(d.values()))


def update_filing(acptno, **fields):
    if not fields:
        return
    for k in ("attachments", "related"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    sets = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE filings SET {sets} WHERE acptno=?", (*fields.values(), acptno))


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["attachments"] = json.loads(d.get("attachments") or "[]")
    d["related"] = json.loads(d.get("related") or "[]")
    d["pub_status"] = pub_status(d)
    d["target"] = target_of(d)
    return d


def pub_status(d) -> str:
    p, i = d.get("pub_plan"), d.get("pub_impl")
    if p and i:
        return "계획·이행 발간"
    if p:
        return "계획 발간완료"
    if i:
        return "이행 발간완료"
    return "미발간"


def target_of(d):
    """작성 대상 여부: True/False, 시총 미확인이면 None"""
    from . import config
    cap = d.get("ann_cap")
    if not cap:
        return None
    return config.CAP_MIN_EOK * 1e8 <= cap <= config.CAP_MAX_EOK * 1e8


def get_filing(acptno):
    with conn() as c:
        return _row(c.execute("SELECT * FROM filings WHERE acptno=?", (acptno,)).fetchone())


def pending_checks(hours: int):
    """첨부 미확인(또는 오류) 상태로 아직 재확인 기간 내인 공시"""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM filings WHERE valid=0 AND detected_at >= datetime('now','localtime',?) ",
            (f"-{hours} hours",),
        ).fetchall()
        return [_row(r) for r in rows]


def list_filings(analyst=None, status=None, valid_only=True, q=None, days=None, limit=500, kind_type=None,
                 since=None, target_only=False):
    """since: 'YYYYMMDD' 이후 공시 / target_only: 발표일 시총 범위 안(미확인 포함)만"""
    sql = "SELECT * FROM filings f WHERE superseded_by IS NULL"
    args = []
    if since:
        sql += " AND rcept_dt >= ?"
        args.append(since)
    if kind_type == "이행":
        sql += " AND kind_type IN ('이행','이행+계획')"
    elif kind_type == "계획":
        sql += " AND kind_type IN ('계획','이행+계획','재공시')"
    elif kind_type:
        sql += " AND kind_type=?"
        args.append(kind_type)
    if valid_only:
        sql += " AND valid=1"
    if status:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND (corp_name LIKE ? OR stock_code LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if days:
        sql += " AND rcept_dt >= strftime('%Y%m%d', 'now', 'localtime', ?)"
        args.append(f"-{int(days)} days")
    if analyst == "__none__":
        sql += " AND stock_code NOT IN (SELECT stock_code FROM coverage)"
    elif analyst:
        sql += " AND stock_code IN (SELECT stock_code FROM coverage WHERE analyst=?)"
        args.append(analyst)
    sql += " ORDER BY rcept_dt DESC, COALESCE(disclosed_at,'') DESC, acptno DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        rows = [_row(r) for r in c.execute(sql, args).fetchall()]
    if target_only:
        rows = [r for r in rows if r["target"] is not False]
    return rows


def set_pub(acptno, field, value, by):
    assert field in ("pub_plan", "pub_impl")
    update_filing(acptno, **{field: 1 if value else 0, "pub_by": by, "pub_at": now()})
    f = get_filing(acptno)
    update_filing(acptno, status="완료" if (f["pub_plan"] or f["pub_impl"]) else "미착수")
    return get_filing(acptno)


def caps_pending(limit=300):
    with conn() as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM filings WHERE valid=1 AND stock_code IS NOT NULL AND COALESCE(ann_final,0)=0 "
            "AND superseded_by IS NULL "
            "ORDER BY rcept_dt DESC LIMIT ?", (limit,))]


def set_status(acptno, status, by, note=""):
    update_filing(acptno, status=status, status_by=by, status_note=note, status_at=now())


# ---------- coverage ----------
def analysts_for(stock_code) -> list[str]:
    from . import pubsync
    ov = pubsync.assigned(stock_code)
    if ov is not None:
        return ov
    with conn() as c:
        return [r["analyst"] for r in c.execute(
            "SELECT analyst FROM coverage WHERE stock_code=? ORDER BY analyst", (stock_code,))]


def replace_coverage(rows: list[tuple[str, str, str]], analyst: str | None = None):
    """rows=(analyst, stock_code, corp_name). analyst 지정 시 해당 애널리스트 분만 교체"""
    with conn() as c:
        if analyst:
            c.execute("DELETE FROM coverage WHERE analyst=?", (analyst,))
        else:
            c.execute("DELETE FROM coverage")
        c.executemany("INSERT OR REPLACE INTO coverage VALUES(?,?,?)", rows)
        for a in {r[0] for r in rows}:
            c.execute("INSERT OR IGNORE INTO analysts(name) VALUES(?)", (a,))


def coverage_summary():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT a.name analyst, a.telegram_chat_id, a.telegram_username, "
            "(SELECT COUNT(*) FROM coverage v WHERE v.analyst=a.name) n "
            "FROM analysts a ORDER BY a.name")]


def coverage_list(analyst=None):
    with conn() as c:
        if analyst:
            rs = c.execute("SELECT * FROM coverage WHERE analyst=? ORDER BY corp_name", (analyst,))
        else:
            rs = c.execute("SELECT * FROM coverage ORDER BY analyst, corp_name")
        return [dict(r) for r in rs]


def analyst_names():
    with conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM analysts ORDER BY name")]


def link_telegram(name, chat_id, username):
    with conn() as c:
        r = c.execute("SELECT 1 FROM analysts WHERE name=?", (name,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE analysts SET telegram_chat_id=?, telegram_username=? WHERE name=?",
                  (str(chat_id), username or "", name))
        return True


def analyst_chats(names):
    if not names:
        return {}
    with conn() as c:
        q = ",".join("?" * len(names))
        return {r["name"]: dict(r) for r in c.execute(
            f"SELECT * FROM analysts WHERE name IN ({q})", names)}


# ---------- 정기 리포트 ----------
def digest_candidates():
    """리포트에 아직 포함되지 않은 인정 공시"""
    with conn() as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM filings WHERE valid=1 AND digest_at IS NULL AND superseded_by IS NULL "
            "ORDER BY disclosed_at, acptno")]


def mark_digested(acptnos, when):
    with conn() as c:
        c.executemany("UPDATE filings SET digest_at=? WHERE acptno=?", [(when, a) for a in acptnos])


def digest_sent(slot) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM digests WHERE slot=?", (slot,)).fetchone() is not None


def record_digest(slot, n):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?)", (slot, now(), n))


def last_digest():
    with conn() as c:
        r = c.execute("SELECT * FROM digests ORDER BY sent_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None


def pending_by_analyst(days=90):
    """담당자별 미착수/작성중 누적"""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT v.analyst, SUM(f.status='미착수') todo, SUM(f.status='작성중') doing "
            "FROM filings f JOIN coverage v ON v.stock_code=f.stock_code "
            "WHERE f.valid=1 AND f.status IN ('미착수','작성중') "
            "AND f.rcept_dt >= strftime('%Y%m%d','now','localtime',?) "
            "GROUP BY v.analyst ORDER BY v.analyst", (f"-{int(days)} days",))]


def supersede(new_acptno, corp_name, stock_code, dates: list[str], before_dt: str):
    """정정공시가 나오면 같은 회사의 정정 전 공시(들)를 목록에서 숨김"""
    with conn() as c:
        q = "SELECT acptno FROM filings WHERE acptno<>? AND (stock_code=? OR corp_name=?) AND superseded_by IS NULL"
        rows = [r["acptno"] for r in c.execute(q + (" AND rcept_dt IN (%s)" % ",".join("?" * len(dates)) if dates else
                                                    " AND rcept_dt<=? ORDER BY rcept_dt DESC, acptno DESC LIMIT 1"),
                                               (new_acptno, stock_code or "", corp_name, *(dates or [before_dt])))]
        for a in rows:
            c.execute("UPDATE filings SET superseded_by=?, digest_at=COALESCE(digest_at, 'superseded') WHERE acptno=?",
                      (new_acptno, a))
        # 발표일 시총은 정정 전 최초 공시일 기준
        first = None
        if rows:
            q2 = "SELECT MIN(COALESCE(orig_disclosed_at, disclosed_at, substr(rcept_dt,1,4)||'-'||substr(rcept_dt,5,2)||'-'||substr(rcept_dt,7,2))) v " \
                 "FROM filings WHERE acptno IN (%s)" % ",".join("?" * len(rows))
            first = c.execute(q2, rows).fetchone()["v"]
        elif dates:
            d = min(dates)
            first = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if first:
            c.execute("UPDATE filings SET orig_disclosed_at=?, ann_final=0 WHERE acptno=? "
                      "AND COALESCE(orig_disclosed_at,'')<>?", (first, new_acptno, first))
        return rows

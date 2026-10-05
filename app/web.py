"""팀 공유 대시보드 (FastAPI)"""
import io
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import config, coverage, db, digest, market, telegram

security = HTTPBasic(auto_error=False)
STATUSES = ["미착수", "작성중", "완료", "제외"]


def auth(cred: HTTPBasicCredentials | None = Depends(security)):
    if not config.DASHBOARD_PASSWORD:
        return
    ok = cred and secrets.compare_digest(cred.username, config.DASHBOARD_USER) and \
        secrets.compare_digest(cred.password, config.DASHBOARD_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


def create_app(worker=None) -> FastAPI:
    app = FastAPI(title="밸류업 공시 알림", dependencies=[Depends(auth)])
    tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tpl.env.globals.update(kind_url=telegram.kind_url, dart_url=telegram.dart_url, STATUSES=STATUSES,
                           fmt_cap=market.fmt_cap, fmt_won=market.fmt_won, fmt_dt=market.fmt_dt,
                           key_line=digest.key_line, TYPES=["계획", "이행", "이행+계획", "재공시"], DIGEST_TIMES=config.DIGEST_TIMES)

    tpl.env.globals["last_update"] = None

    @app.middleware("http")
    async def _lu(request, call_next):
        tpl.env.globals["last_update"] = db.get_meta("last_update")
        return await call_next(request)

    def add_quotes(rows):
        for r in rows:
            r["q"] = market.quote(r.get("stock_code"), r.get("corp_code"), offline=True)
        return rows

    def me(request: Request):
        return unquote(request.cookies.get("me", ""))

    PERIODS = {"today": "오늘", "week": "이번 주", "month": "최근 30일", "year": "1년"}

    def since_of(period):
        t = datetime.now().date()
        d = {"today": t, "week": t - timedelta(days=t.weekday()), "month": t - timedelta(days=30),
             "year": t - timedelta(days=365)}.get(period, t)
        return d.strftime("%Y%m%d")

    def filtered(period="today", analyst="", t="", all=0, q=""):
        rows = db.list_filings(since=since_of(period), kind_type=t or None, q=q or None,
                               target_only=not all, limit=5000)
        cov = {}
        for c in db.coverage_list():
            cov.setdefault(c["stock_code"], []).append(c["analyst"])
        for r in rows:
            r["analysts"] = cov.get(r["stock_code"] or "", [])
        counts = {"": len(rows), "__none__": sum(1 for r in rows if not r["analysts"])}
        for a in db.analyst_names():
            counts[a] = sum(1 for r in rows if a in r["analysts"])
        if analyst == "__none__":
            rows = [r for r in rows if not r["analysts"]]
        elif analyst:
            rows = [r for r in rows if analyst in r["analysts"]]
        return rows, counts

    @app.get("/")
    def index(request: Request, period: str = "today", analyst: str = "", t: str = "", all: int = 0, q: str = ""):
        rows, counts = filtered(period, analyst, t, all, q)
        today = db.list_filings(since=since_of("today"), limit=1000)
        banner = {"n": len(today), "plan": sum(1 for r in today if (r["kind_type"] or "계획") in ("계획", "재공시")),
                  "impl": sum(1 for r in today if (r["kind_type"] or "").startswith("이행")),
                  "target": sum(1 for r in today if r["target"] is not False)}
        return tpl.TemplateResponse(request, "index.html", {
            "rows": rows, "counts": counts, "analysts": db.analyst_names(), "banner": banner,
            "f": {"period": period, "analyst": analyst, "t": t, "all": all, "q": q},
            "PERIODS": PERIODS, "today": datetime.now(), "last_update": db.get_meta("last_update"),
            "qs": str(request.query_params), "me": me(request)})

    @app.post("/filing/{acptno}/pub")
    async def set_pub(request: Request, acptno: str):
        data = await request.json()
        field = {"plan": "pub_plan", "impl": "pub_impl"}.get(data.get("field"))
        if not field or not db.get_filing(acptno):
            raise HTTPException(400)
        f = db.set_pub(acptno, field, bool(data.get("value")), me(request) or "-")
        return {"pub_status": f["pub_status"], "pub_plan": f["pub_plan"], "pub_impl": f["pub_impl"]}

    @app.post("/me")
    def set_me(name: str = Form("")):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("me", quote(name), max_age=3600 * 24 * 365)
        return r

    @app.get("/filing/{acptno}")
    def detail(request: Request, acptno: str):
        f = db.get_filing(acptno)
        if not f:
            raise HTTPException(404)
        f["analysts"] = db.analysts_for(f["stock_code"] or "")
        f["q"] = market.quote(f.get("stock_code"), f.get("corp_code"), offline=True)
        return tpl.TemplateResponse(request, "detail.html", {"f": f, "me": me(request)})

    @app.post("/filing/{acptno}/status")
    def set_status(request: Request, acptno: str, status: str = Form(...), note: str = Form(""),
                   by: str = Form(""), back: str = Form("")):
        if status not in STATUSES:
            raise HTTPException(400)
        db.set_status(acptno, status, by or me(request) or "-", note)
        return RedirectResponse(back or f"/filing/{acptno}", status_code=303)

    @app.post("/filing/{acptno}/recheck")
    def recheck(acptno: str):
        if worker:
            with worker.lock:
                worker.check(acptno)
        return RedirectResponse(f"/filing/{acptno}", status_code=303)

    @app.get("/files/{path:path}")
    def files(path: str):
        p = (config.FILE_DIR / path).resolve()
        if not str(p).startswith(str(config.FILE_DIR.resolve())) or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, filename=p.name)

    # ---------------- 커버리지 ----------------
    @app.get("/coverage")
    def cov_page(request: Request, analyst: str = "", msg: str = ""):
        return tpl.TemplateResponse(request, "coverage.html", {
            "summary": db.coverage_summary(), "items": db.coverage_list(analyst or None),
            "analyst": analyst, "msg": msg, "me": me(request), "src": coverage.status()})

    @app.post("/coverage/reload")
    def cov_reload():
        st = worker.sync_coverage(force=True) if worker else coverage.sync({}, force=True)
        msg = f"원본 다시 읽음: {st.get('count') or 0}개" + (f" · ⚠ {st['error']}" if st.get("error") else "")
        return RedirectResponse(f"/coverage?msg={msg}", status_code=303)

    @app.get("/coverage/source.xlsx")
    def cov_source():
        if config.COVERAGE_URL or not config.COVERAGE_FILE.exists():
            raise HTTPException(404)
        return FileResponse(config.COVERAGE_FILE, filename=config.COVERAGE_FILE.name)

    @app.post("/coverage/upload")
    async def cov_upload(file: UploadFile = File(...)):
        """올린 엑셀을 양식 그대로 원본 파일로 교체 (인식되는지 먼저 확인)"""
        if config.COVERAGE_URL:
            return RedirectResponse("/coverage?msg=URL 원본을 쓰는 중이라 원본을 직접 수정하세요", status_code=303)
        suffix = Path(file.filename or "x.xlsx").suffix or ".xlsx"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
        rows, warns = coverage.load(Path(tmp.name), worker.corp_map if worker else {})
        if not rows:
            Path(tmp.name).unlink(missing_ok=True)
            return RedirectResponse("/coverage?msg=인식된 종목이 없습니다. 양식을 확인하세요", status_code=303)
        config.COVERAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp.name, config.COVERAGE_FILE)
        st = worker.sync_coverage(force=True) if worker else coverage.sync({}, force=True)
        msg = f"원본 교체 완료: {st.get('count') or len(rows)}개 종목"
        if warns:
            msg += f" · 확인 필요 {len(warns)}건: " + "; ".join(warns[:10])
        return RedirectResponse(f"/coverage?msg={msg}", status_code=303)

    @app.get("/coverage/template.xlsx")
    def cov_template():
        df = pd.DataFrame([["홍길동", "005930", "삼성전자"], ["홍길동", "000660", "SK하이닉스"],
                           ["김철수", "388050", "지투파워"]], columns=["애널리스트", "종목코드", "종목명"])
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": "attachment; filename=coverage_template.xlsx"})

    @app.get("/coverage/export.xlsx")
    def cov_export():
        buf = io.BytesIO()
        pd.DataFrame(db.coverage_list()).rename(columns={"analyst": "애널리스트", "stock_code": "종목코드",
                                                         "corp_name": "종목명"}).to_excel(buf, index=False)
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": "attachment; filename=coverage.xlsx"})

    @app.get("/export.xlsx")
    def export(period: str = "year", analyst: str = "", t: str = "", all: int = 0, q: str = ""):
        rows, _ = filtered(period, analyst, t, all, q)
        df = pd.DataFrame([{
            "종목명": r["corp_name"],
            "발표일 시총(억원)": round(r["ann_cap"] / 1e8) if r.get("ann_cap") else None,
            "담당": ", ".join(r["analysts"]) or "커버리지 외",
            "발간 상태": r["pub_status"],
        } for r in rows], columns=["종목명", "발표일 시총(억원)", "담당", "발간 상태"])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="밸류업 현황")
            ws = w.sheets["밸류업 현황"]
            for col, width in zip("ABCD", (18, 16, 18, 16)):
                ws.column_dimensions[col].width = width
            for cell in ws["B"][1:]:
                cell.number_format = "#,##0"
        buf.seek(0)
        name = f"valueup_status_{datetime.now():%Y%m%d}.xlsx"
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/digest")
    def digest_preview(request: Request, msg: str = ""):
        msgs = digest.build(db.digest_candidates())
        return tpl.TemplateResponse(request, "digest.html", {
            "msgs": msgs, "me": me(request), "msg": msg, "times": config.DIGEST_TIMES,
            "last": db.last_digest(), "next_slot": digest.due_slot()})

    @app.post("/digest/send")
    def digest_send():
        if worker:
            with worker.lock:
                worker.sync_coverage()
                digest.send_digest(None)
        else:
            digest.send_digest(None)
        return RedirectResponse("/digest?msg=리포트를 보냈습니다", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True, **(worker.status if worker else {})}

    return app

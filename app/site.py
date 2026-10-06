"""GitHub Pages용 정적 대시보드 + 현황 엑셀"""
import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from . import config, db, digest, market, telegram


def rows_for_site(days=370) -> list[dict]:
    cov = {}
    for c in db.coverage_list():
        cov.setdefault(c["stock_code"], []).append(c["analyst"])
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    out = []
    for r in db.list_filings(since=since, limit=5000):
        out.append({
            "id": r["acptno"], "dt": r["rcept_dt"], "time": (r["disclosed_at"] or "")[11:16],
            "name": r["corp_name"], "code": r["stock_code"] or "", "market": r["market"] or "",
            "type": r["kind_type"] or "계획", "cap": r["ann_cap"], "capTxt": market.fmt_cap(r["ann_cap"]) if r["ann_cap"] else "",
            "capDt": market.fmt_dt(r["ann_dt"]), "final": bool(r["ann_final"]), "close": r["ann_close"],
            "target": r["target"], "key": digest.key_line(r, 80), "analysts": cov.get(r["stock_code"] or "", []),
            "pubPlan": bool(r["pub_plan"]), "pubImpl": bool(r["pub_impl"]), "pub": r["pub_status"],
            "url": telegram.kind_url(r["acptno"]),
            "unverified": str(r.get("last_error") or "").startswith("첨부 미확인"),
        })
    return out


def status_xlsx(days=365) -> bytes:
    rows = [r for r in rows_for_site(days) if r["target"] is not False]
    df = pd.DataFrame([{"종목명": r["name"], "발표일 시총(억원)": round(r["cap"] / 1e8) if r["cap"] else None,
                        "담당": ", ".join(r["analysts"]) or "커버리지 외", "발간 상태": r["pub"]} for r in rows],
                      columns=["종목명", "발표일 시총(억원)", "담당", "발간 상태"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="밸류업 현황")
        ws = w.sheets["밸류업 현황"]
        for col, width in zip("ABCD", (18, 16, 18, 16)):
            ws.column_dimensions[col].width = width
    return buf.getvalue()


def build(out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir or config.SITE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    data = {"rows": rows_for_site(), "analysts": db.analyst_names(),
            "lastUpdate": db.get_meta("last_update") or "", "today": datetime.now().strftime("%Y%m%d"),
            "capMin": config.CAP_MIN_EOK, "capMax": config.CAP_MAX_EOK,
            "repo": os.getenv("GITHUB_REPOSITORY", ""), "branch": os.getenv("GITHUB_REF_NAME", "main"),
            "capError": db.get_meta("cap_error") or ""}
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    html = env.get_template("static.html").render(data_json=json.dumps(data, ensure_ascii=False))
    p = out_dir / "index.html"
    p.write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return p

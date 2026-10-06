"""GitHub Pages용 정적 대시보드 + 현황 엑셀"""
import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from . import config, db, digest, market, pubsync, teamcfg, telegram


def rows_for_site(days=None) -> list[dict]:
    """days=None: 쌓인 공시 전체 (기간 조회는 사이트에서)"""
    cov = {}
    for c in db.coverage_list():
        cov.setdefault(c["stock_code"], []).append(c["analyst"])
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d") if days else None
    out = []
    implset = pubsync.impl_codes()
    tmap = teamcfg.teams()
    for r in db.list_filings(since=since, limit=100000):
        if not db.analysts_for(r["stock_code"]) and not teamcfg.visible_unassigned(r["stock_code"], tmap):
            continue   # 담당 없는 다른 팀 종목은 제외
        out.append({
            "id": r["acptno"], "dt": r["rcept_dt"], "time": (r["disclosed_at"] or "")[11:16],
            "name": r["corp_name"], "code": r["stock_code"] or "", "market": r["market"] or "",
            "type": r["kind_type"] or "계획", "cap": r["ann_cap"], "capTxt": market.fmt_cap(r["ann_cap"]) if r["ann_cap"] else "",
            "capDt": market.fmt_dt(r["ann_dt"]), "final": bool(r["ann_final"]), "close": r["ann_close"],
            "target": r["target"], "key": digest.key_line(r, 80), "team": tmap.get(r["stock_code"] or "", ""), "baseAnalysts": cov.get(r["stock_code"] or "", []),
            "analysts": db.analysts_for(r["stock_code"]) if r["stock_code"] else [],
            "pub": pubsync.company_status(r["stock_code"], implset),
            "url": telegram.kind_url(r["acptno"]), "corr": bool(r.get("is_correction")),
            "unverified": str(r.get("last_error") or "").startswith("첨부 미확인"),
        })
    return out


def status_xlsx(days=365) -> bytes:
    rows = [r for r in rows_for_site(days) if r["target"] is not False]
    pub = pubsync.state()["pub"]
    cols = ["공시일", "종목명", "발표일 시총(억원)", "담당", "발간 상태", "계획 발간일", "계획 발간인", "이행 발간일", "이행 발간인"]
    df = pd.DataFrame([{"공시일": f"{r['dt'][:4]}.{r['dt'][4:6]}.{r['dt'][6:]}", "종목명": r["name"],
                        "발표일 시총(억원)": round(r["cap"] / 1e8) if r["cap"] else None,
                        "담당": ", ".join(r["analysts"]) or ("커버리지 외(1팀)" if r["team"] else "미분류"), "발간 상태": r["pub"],
                        "계획 발간일": pub.get(r["code"], {}).get("plan_date", ""), "계획 발간인": pub.get(r["code"], {}).get("plan_by", ""),
                        "이행 발간일": pub.get(r["code"], {}).get("impl_date", ""), "이행 발간인": pub.get(r["code"], {}).get("impl_by", "")}
                       for r in rows], columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="밸류업 현황")
        ws = w.sheets["밸류업 현황"]
        for col, width in zip("ABCDEFGHI", (12, 18, 16, 18, 14, 12, 10, 12, 10)):
            ws.column_dimensions[col].width = width
    return buf.getvalue()


def build(out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir or config.SITE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    st = teamcfg.settings()
    data = {"rows": rows_for_site(), "analysts": st["tabs"] or db.analyst_names(), "assignees": teamcfg.assignees(),
            "lastUpdate": db.get_meta("last_update") or "", "today": datetime.now().strftime("%Y%m%d"),
            "capMin": config.CAP_MIN_EOK, "capMax": config.CAP_MAX_EOK,
            "repo": os.getenv("GITHUB_REPOSITORY", ""), "branch": os.getenv("GITHUB_REF_NAME", "main"),
            "capError": db.get_meta("cap_error") or "", "state": pubsync.state(),
            "implCodes": sorted(pubsync.impl_codes())}
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    html = env.get_template("static.html").render(data_json=json.dumps(data, ensure_ascii=False))
    p = out_dir / "index.html"
    p.write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return p

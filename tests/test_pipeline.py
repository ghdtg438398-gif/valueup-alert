"""네트워크 없이 실제 KIND 페이지 구조(2026-10 캡처)로 전체 흐름 검증"""
import io
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return (FX / name).read_bytes()


class FakeResp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, n):
        yield self.content


class FakeSession:
    headers = {}

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _route(self, url, params=None):
        key = url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
        self.calls.append(key)
        for pat, body in self.routes.items():
            if pat in key:
                return FakeResp(body)
        return FakeResp(b"", 404)

    def get(self, url, params=None, **kw):
        return self._route(url, params)

    def post(self, url, data=None, **kw):
        return self._route(url + "#POST")


ROUTES = {
    "main.do": b"ok",
    "disclsstat.do#POST": fx("list.html"),
    "acptno=20260930000281": fx("viewer.html"),
    "acptno=20260930000835": fx("viewer_noatt.html"),
    "docNo=20260930000598": fx("contents_att.html"),
    "docNo=20260928001940": fx("contents_main.html"),
    "docNo=20260930002222": fx("contents_main.html"),
    "99928.htm": fx("attach.htm"),
    "72101.htm": fx("main.htm"),
    ".pdf": b"%PDF-1.4 fake",
    "acptno=20261005000222": fx("viewer_corr.html"),
    "docNo=20261005000111": fx("contents_corr.html"),
    "80000.htm": fx("main_corr.htm"),
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "FILE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "COVERAGE_FILE", tmp_path / "coverage.xlsx")
    monkeypatch.setattr(config, "COVERAGE_URL", "")
    monkeypatch.setattr(config, "HOLIDAY_FILE", tmp_path / "krx_holidays.txt")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "INSTANT_ALERT", False)
    monkeypatch.setattr(config, "SEND_DM_TO_ANALYST", False)
    monkeypatch.setattr(config, "DASHBOARD_URL", "http://dash")
    from app import db, market, telegram, worker
    db.init()
    sent = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, buttons=None: sent.append((chat, text, buttons)) or {"ok": 1})
    xml = fx("fchart.xml").decode("utf-8")
    monkeypatch.setattr(market, "fetch_closes", lambda code, basis: (market.parse_fchart(xml), 18_745_338))
    monkeypatch.setattr(config, "KIS_APP_KEY", "")
    w = worker.Worker()
    w.kind.s = FakeSession(dict(ROUTES))
    w.kind._warm = True
    w.corp_map = {"지투파워": {"corp_code": "01586767", "stock_code": "388050", "corp_name": "지투파워"},
                  "세아제강지주": {"corp_code": "00128555", "stock_code": "003030", "corp_name": "세아제강지주"}}
    return w, db, sent


def write_cov(path, rows):
    pd.DataFrame(rows, columns=["애널리스트", "종목코드", "종목명"]).to_excel(path, index=False)


def test_parsers():
    from app import kind
    rows, total = kind.parse_valueup_list(fx("list.html").decode())
    assert total == 3 and rows[2]["acptno"] == "20260930000281"
    assert rows[2]["disclosed_at"] == "2026-09-30 11:39" and rows[2]["market"] == "코스닥"
    assert rows[1]["market"] == "유가" and rows[0]["title"] == "기업가치 제고 계획 예고"
    v = kind.parse_viewer(fx("viewer.html").decode())
    assert v["main"] == [("20260928001940", "기업가치 제고 계획(자율공시) (2026.09.30)")]
    assert v["attached"][0][0] == "20260930000598"
    links = kind.find_file_links(fx("attach.htm").decode(),
                                 "https://kind.krx.co.kr/external/2026/09/30/000281/20260930000598/99928.htm")
    assert links[0]["ext"] == "pdf" and " " not in links[0]["url"]
    assert links[0]["url"].startswith("https://kind.krx.co.kr/external/2026/09/30/000281/20260930000598/2026%20")
    # 홈페이지·관련공시 링크는 첨부로 치지 않음
    assert kind.find_file_links('<a href="http://www.abc.com">x</a><a href="/common/disclsviewer.do?method=search">y</a>',
                                "https://kind.krx.co.kr/a/b.htm") == []
    b = kind.parse_main_body(fx("main.htm").decode())
    assert b["plan_name"] == "2026 지투파워(주) 기업가치 제고 계획"
    assert "배당성향 30% 이상" in b["main_content"] and "고배당" not in b["main_content"]
    assert (b["high_dividend"], b["pbr_plan"], b["decision_date"]) == ("해당", "미포함", "2026-09-30")
    assert b["related"] == [{"date": "2026-09-10", "title": "기업가치 제고 계획 예고"},
                            {"date": "2026-03-31", "title": "기업가치 제고 계획(자율공시)"}]


@pytest.mark.parametrize("title,plan,expect", [
    ("기업가치 제고 계획(자율공시)", "2026 (주)이수페타시스 기업가치 제고 계획 이행현황", "이행"),
    ("기업가치 제고 계획(자율공시)", "현대엘리베이터 기업가치 제고 계획 이행현황(2025년)", "이행"),
    ("기업가치 제고 계획(자율공시)(2025년 이행현황)", "", "이행"),
    ("기업가치 제고 계획(자율공시)(2026년 1분기 이행현황)", "", "이행"),
    ("기업가치 제고 계획(자율공시)((2025년 이행현황 및 2026년 기업가치제고계획))", "", "이행+계획"),
    ("기업가치 제고 계획(자율공시)(고배당기업 표시를 위한 재공시)", "", "재공시"),
    ("기업가치 제고 계획(자율공시)(이행현황 재공시 (고배당기업에 해당))", "", "이행"),
    ("기업가치 제고 계획(자율공시)(2026년)", "2026 지투파워(주) 기업가치 제고 계획", "계획"),
    ("기업가치 제고 계획(자율공시)(중기 주주환원 정책)", "", "계획"),
])
def test_classify(title, plan, expect):
    from app import kind
    assert kind.classify(title, plan) == expect


def test_dart_helpers():
    from app import dart
    assert dart.is_valueup("기업가치제고계획(자율공시)              (2026년)")
    assert dart.is_valueup("[기재정정]기업가치제고계획(자율공시)")
    assert not dart.is_valueup("기업가치제고계획예고")
    assert dart.to_kind_acptno("20260930900281") == "20260930000281"
    assert dart.to_dart_rcept_no("20260930000281") == "20260930900281"


def test_calendar_and_price_basis(env):
    from app import config, market
    from app.digest import due_slot
    D = datetime
    assert market.is_business_day(date(2026, 10, 2))
    assert not market.is_business_day(date(2026, 10, 5))     # 개천절 대체공휴일
    assert not market.is_business_day(date(2026, 12, 31))    # 연말 휴장
    assert market.price_basis_date(D(2026, 10, 2, 15, 0)) == date(2026, 10, 1)   # 장중 → 전일
    assert market.price_basis_date(D(2026, 10, 2, 15, 31)) == date(2026, 10, 2)  # 15:30 이후 → 당일
    assert market.price_basis_date(D(2026, 10, 6, 9, 0)) == date(2026, 10, 2)    # 연휴 다음날 → 직전 영업일
    assert due_slot(D(2026, 10, 2, 9, 5)) == "2026-10-02 09:00"
    assert due_slot(D(2026, 10, 2, 8, 59)) is None
    assert due_slot(D(2026, 10, 2, 15, 31)) is None               # 15:30 회차는 종가 확정 대기
    assert due_slot(D(2026, 10, 2, 15, 36)) == "2026-10-02 15:30"
    assert due_slot(D(2026, 10, 5, 9, 5)) is None                # 휴일
    config.HOLIDAY_FILE.write_text("2026-10-02  # 임시휴장 예시\n", encoding="utf-8")
    assert due_slot(D(2026, 10, 2, 9, 5)) is None


def test_quote(env):
    from app import market
    q = market.quote("388050", "01586767", datetime(2026, 10, 2, 15, 0))
    assert q["close"] == 12650 and q["close_dt"] == "20261001" and q["basis_label"] == "전일 종가"
    assert q["mktcap"] == 12650 * 18_745_338
    assert market.fmt_cap(q["mktcap"]) == "2,371억"
    assert market.fmt_cap(4_752_045 * 10**8) == "475조 2,045억"


def test_coverage_file_sync(env):
    from app import config, coverage
    w, db, sent = env
    st = coverage.sync(w.corp_map)                    # 파일이 없으면 예시 원본 생성
    assert config.COVERAGE_FILE.exists() and int(st["count"]) == 3
    write_cov(config.COVERAGE_FILE, [["홍길동", "388050", "지투파워"], ["이영희", None, "세아제강지주"]])
    st = coverage.sync(w.corp_map)
    assert int(st["count"]) == 2 and db.analysts_for("003030") == ["이영희"]
    assert "김철수" not in db.analyst_names()          # 원본에서 빠진 사람 정리
    before = db.get_meta("coverage_loaded_at")
    coverage.sync(w.corp_map)                         # 변경 없으면 재반영 안 함
    assert db.get_meta("coverage_loaded_at") == before
    # 잘못된 파일 → 기존 커버리지 유지
    config.COVERAGE_FILE.write_bytes(b"garbage")
    st = coverage.sync(w.corp_map)
    assert st["error"] and db.analysts_for("388050") == ["홍길동"]


def test_end_to_end_digest(env):
    from app import config
    w, db, sent = env
    write_cov(config.COVERAGE_FILE, [["홍길동", "388050", "지투파워"], ["김철수", "388050", "지투파워"],
                                     ["이영희", "005930", "삼성전자"]])
    w.sync_coverage()
    assert w.poll_kind(days=1) == 2                   # 예고 제외
    assert sent == []                                 # 실시간 알림 끔 → 정기 알림으로만
    g = db.get_filing("20260930000281")
    assert g["valid"] == 1 and g["kind_type"] == "계획" and g["stock_code"] == "388050"
    s = db.get_filing("20260930000835")
    assert s["valid"] == 0 and s["kind_type"] == "이행"   # 첨부 없음 → 미인정
    w.maybe_digest(datetime(2026, 10, 2, 8, 50))
    assert sent == []
    w.maybe_digest(datetime(2026, 10, 2, 9, 3))
    g = db.get_filing("20260930000281")
    assert g["ann_close"] == 12340 and g["ann_dt"] == "20260930" and g["ann_final"] == 1   # 공시일(9/30) 종가
    assert g["ann_cap"] == 12340 * 18_745_338 and g["target"] is True
    text = "\n".join(m[1] for m in sent)
    assert "10/02(금) 09:00" in text and "작성 대상 <b>1건</b>" in text
    assert "<b>1) 지투파워</b> · 👤 김철수, 홍길동" in text
    assert "🟦 계획 · 발표일 시총 <b>2,313억</b>" in text and "▸ " in text
    n = len(sent)
    w.maybe_digest(datetime(2026, 10, 2, 9, 30))       # 같은 회차 재발송 없음
    assert len(sent) == n
    # 세아제강지주(커버리지 외) 첨부가 나중에 올라옴 → 15:30 회차에 '이행'으로
    w.kind.s.routes["acptno=20260930000835"] = fx("viewer.html")
    w.check("20260930000835")
    w.maybe_digest(datetime(2026, 10, 2, 15, 36))
    text2 = "\n".join(m[1] for m in sent[n:])
    assert "세아제강지주" in text2 and "🟩 이행" in text2 and "미분류" in text2 and "지투파워" not in text2
    # 작성 대상 범위 밖이면 알림에서 빠짐
    config.CAP_MAX_EOK = 1000
    try:
        db.update_filing("20260930000281", digest_at=None)
        m = len(sent)
        w.maybe_digest(datetime(2026, 10, 6, 9, 0))
        assert "지투파워" not in sent[m][1] and "작성 대상 공시 없음" in sent[m][1]
    finally:
        config.CAP_MAX_EOK = 5000


def test_instant_alert_option(env, monkeypatch):
    from app import config
    w, db, sent = env
    monkeypatch.setattr(config, "INSTANT_ALERT", True)
    w.poll_kind(days=1)
    assert len(sent) == 1 and "[계획]" in sent[0][1] and "시총" in sent[0][1]


def test_backfill_silent(env):
    w, db, sent = env
    w.poll_kind(days=30, silent=True)
    assert sent == [] and db.digest_candidates() == []


def test_coverage_loader(tmp_path):
    from app import coverage
    cmap = {"지투파워": {"stock_code": "388050", "corp_name": "지투파워", "corp_code": "x"},
            "삼성전자": {"stock_code": "005930", "corp_name": "삼성전자", "corp_code": "y"}}
    p = tmp_path / "long.xlsx"
    pd.DataFrame({"애널리스트": ["김선호", "김선호", "이영희"], "종목코드": ["A388050", None, "5930"],
                  "종목명": ["지투파워", "삼성전자", None]}).to_excel(p, index=False)
    rows, warns = coverage.load(p, cmap)
    assert sorted(rows) == [("김선호", "005930", "삼성전자"), ("김선호", "388050", "지투파워"),
                            ("이영희", "005930", "삼성전자")]
    p2 = tmp_path / "wide.xlsx"
    pd.DataFrame({"김선호": ["지투파워", "없는회사"], "이영희": ["005930", None]}).to_excel(p2, index=False)
    rows, warns = coverage.load(p2, cmap)
    assert ("김선호", "388050", "지투파워") in rows and ("이영희", "005930", "삼성전자") in rows
    assert len(warns) == 1


def test_web(env):
    from fastapi.testclient import TestClient
    from app import config
    from app.web import create_app
    w, db, sent = env
    write_cov(config.COVERAGE_FILE, [["홍길동", "388050", "지투파워"]])
    w.sync_coverage()
    w.poll_kind(days=1)
    w.warm_quotes()
    c = TestClient(create_app(w))
    w.compute_caps()
    r = c.get("/?period=year")
    assert r.status_code == 200 and "지투파워" in r.text and "세아제강지주" not in r.text   # 미인정 제외
    assert "t-계획" in r.text and "2,313억" in r.text and "마지막 업데이트" in r.text
    assert "오늘 신규 공시" in r.text and "1년" in r.text
    r = c.post("/filing/20260930000281/pub", json={"field": "plan", "value": True})
    assert r.json()["pub_status"] == "계획 발간완료"
    import pandas as _pd
    x = _pd.read_excel(io.BytesIO(c.get("/export.xlsx?period=year").content))
    assert list(x.columns) == ["종목명", "발표일 시총(억원)", "담당", "발간 상태"]
    assert x.iloc[0].tolist() == ["지투파워", 2313, "홍길동", "계획 발간완료"]
    from app import config as _c
    _c.CAP_MAX_EOK = 1000
    try:
        assert "지투파워" not in c.get("/?period=year").text          # 작성 대상만 (기본)
        assert "대상 외" in c.get("/?period=year&all=1").text
    finally:
        _c.CAP_MAX_EOK = 5000
    d = c.get("/filing/20260930000281")
    assert "2026 지투파워(주) 기업가치 제고 계획" in d.text and "관련 공시" in d.text and "발표일 시총" in d.text
    # 업로드 → 원본 파일이 바뀌고 반영
    buf = io.BytesIO()
    pd.DataFrame({"애널리스트": ["박민수"], "종목코드": ["003030"], "종목명": ["세아제강지주"]}).to_excel(buf, index=False)
    r = c.post("/coverage/upload", files={"file": ("c.xlsx", buf.getvalue())}, )
    assert r.status_code == 200 and "1개 종목" in r.text
    assert pd.read_excel(config.COVERAGE_FILE, dtype=str)["애널리스트"].tolist() == ["박민수"]
    assert "원본 파일" in c.get("/coverage").text
    assert c.get("/coverage/source.xlsx").status_code == 200
    assert "다음 리포트" in c.get("/digest").text or c.get("/digest").status_code == 200
    assert c.post("/digest/send", follow_redirects=False).status_code == 303 and sent
    f = db.get_filing("20260930000281")["attachments"][0]["local"]
    assert c.get("/files/" + f).content.startswith(b"%PDF")
    assert c.get("/files/../../etc/passwd").status_code == 404


def test_action_run_site_and_buttons(env, monkeypatch, tmp_path):
    from app import config, site, telegram
    w, db, sent = env
    monkeypatch.setattr(config, "SITE_DIR", tmp_path / "docs")
    write_cov(config.COVERAGE_FILE, [["홍길동", "388050", "지투파워"]])
    db.set_meta("initialized", "x")
    docs = []
    monkeypatch.setattr(telegram, "send_document", lambda *a, **k: docs.append(a))
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda m, **k: calls.append((m, k)) or [])
    # 15:30 회차 실행 → 알림 + 엑셀 첨부 + 정적 페이지
    sent_kb = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, buttons=None: sent.append((chat, text, buttons)) or {"ok": 1})
    res = w.run_once_action(now=datetime(2026, 10, 2, 15, 40))
    assert res["slot"] == "2026-10-02 15:30" and res["kind_ok"]
    assert "지투파워" in sent[-1][1] and sent[-1][2][0][0]["callback_data"] == "pub:20260930000281:plan"
    assert docs and docs[0][2].endswith(".xlsx")
    html = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert "지투파워" in html and "마지막 업데이트" in html and "xlsx.full.min.js" in html
    # 텔레그램 버튼 눌림 → 발간 체크
    telegram.handle_update({"update_id": 1, "callback_query": {"id": "c1", "data": "pub:20260930000281:plan",
                                                               "from": {"id": 99, "first_name": "길동"}}})
    from app import pubsync
    assert pubsync.company_status("388050") == "이행 미공시"          # 계획✓, 이행 공시 없음
    x = pd.read_excel(io.BytesIO(site.status_xlsx()))
    row = dict(zip(x.columns, x.iloc[0].tolist()))
    assert row["종목명"] == "지투파워" and row["발간 상태"] == "이행 미공시"


def test_company_status_and_assign(env, tmp_path, monkeypatch):
    from app import pubsync
    w, db, sent = env
    monkeypatch.setattr(pubsync, "PATH", tmp_path / "pub_status.json")
    write_cov(__import__("app").config.COVERAGE_FILE, [["홍길동", "388050", "지투파워"]])
    w.sync_coverage()
    w.kind.s.routes["acptno=20260930000835"] = fx("viewer.html")   # 세아제강지주 이행 공시 인정
    w.poll_kind(days=1)
    assert pubsync.company_status("003030") == "미발간"
    pubsync.set_pub("003030", "plan", True, "x")
    assert pubsync.company_status("003030") == "이행 미발간"         # 이행 공시가 이미 나와 있음
    pubsync.set_pub("003030", "impl", True, "x")
    assert pubsync.company_status("003030") == "발간 완료"
    pubsync.set_pub("388050", "plan", True, "x")
    assert pubsync.company_status("388050") == "이행 미공시"
    # 담당 직접 지정 → 알림·사이트에 반영, 파일로 저장 후 다시 읽어도 유지
    st = pubsync.state(); st["assign"]["388050"] = {"analyst": "김철수"}; pubsync._save_state(st)
    assert db.analysts_for("388050") == ["김철수"]
    assert pubsync.push() and pubsync.load_file()["assign"]["388050"]["analyst"] == "김철수"
    st["assign"]["388050"] = {"analyst": "-"}; pubsync._save_state(st)
    assert db.analysts_for("388050") == []
    pubsync.pull()                                                     # 파일 기준으로 되돌림
    assert db.analysts_for("388050") == ["김철수"]


def test_kind_blocked_fallback(env):
    from app import config
    w, db, sent = env
    w.kind_ok = False
    w.kind.s.routes = {}  # KIND 전부 404
    w.register({"acptno": "20261002000999", "corp_name": "지투파워", "report_nm": "기업가치제고계획(자율공시)(2025년 이행현황)",
                "rcept_dt": "20261002", "source": "dart"})
    f = db.get_filing("20261002000999")
    assert f["valid"] == 1 and f["kind_type"] == "이행" and f["last_error"].startswith("첨부 미확인")


def test_correction_replaces_original(env):
    from app import kind
    w, db, sent = env
    html = fx("list.html").decode().replace(
        "<tbody>", """<tbody><tr><td class="first txc">4</td><td class="txc">2026-10-05 10:00</td>
<td><img alt='코스닥'> <a title='지투파워'>지투파워</a></td>
<td><a href="#viewer" onclick="openDisclsViewer('20261005000222','')" title='기업가치 제고 계획(자율공시)'><font color="#FF8040">[정정]</font>기업가치 제고 계획(자율공시)</a></td></tr>""")
    rows, _ = kind.parse_valueup_list(html)
    assert rows[0]["title"] == "[정정]기업가치 제고 계획(자율공시)"
    w.kind.s.routes["disclsstat.do#POST"] = html.encode()
    w.poll_kind(days=7)
    ids = [r["acptno"] for r in db.list_filings(days=0, limit=50)]
    assert "20261005000222" in ids and "20260930000281" not in ids          # 원본은 정정본으로 대체
    c = db.get_filing("20261005000222")
    assert c["is_correction"] == 1 and c["plan_name"].endswith("(정정)") and "배당성향 35%" in c["main_content"]
    assert db.get_filing("20260930000281")["superseded_by"] == "20261005000222"
    # 발표일 시총은 정정일(10/5)이 아니라 최초 공시일(9/30) 종가 기준
    assert c["orig_disclosed_at"] == "2026-09-30 11:39"
    w.compute_caps()
    c = db.get_filing("20261005000222")
    assert c["ann_dt"] == "20260930" and c["ann_close"] == 12340


def test_reject_status(env, tmp_path, monkeypatch):
    from app import pubsync
    w, db, sent = env
    monkeypatch.setattr(pubsync, "PATH", tmp_path / "p.json")
    pubsync.set_pub("388050", "plan", True)
    pubsync.set_pub("388050", "reject", True)
    assert pubsync.company_status("388050") == "발간 거절"


def test_team_classification(env):
    import json
    from app import digest, teamcfg
    w, db, sent = env
    w.kind.s.routes["acptno=20260930000835"] = fx("viewer.html")
    w.poll_kind(days=1)
    w.compute_caps()
    f = [db.get_filing("20260930000835")]
    db.set_meta("teams", json.dumps({"003030": "리서치1팀"}))
    assert "커버리지 외(1팀)" in digest.build(f)[0]
    db.set_meta("teams", json.dumps({"003030": "리서치2팀"}))
    assert "세아제강지주" not in digest.build(f)[0]          # 다른 팀 종목은 제외
    db.set_meta("teams", json.dumps({}))
    assert "미분류" in digest.build(f)[0]                    # 산업구분 시트에 없음
    assert teamcfg.visible_unassigned("999999", {}) is True

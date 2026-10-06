"""영업일 달력 + 종가/시가총액

- 영업일: 주말·공휴일(holidays 패키지, 대체공휴일·선거일 포함)·연말 휴장일(12/31)·
          data/krx_holidays.txt 에 적은 임시 휴장일 제외
- 가격 기준: 영업일 15:30 이후면 당일 종가, 그 전이면 직전 영업일 종가
- 종가: 한국투자증권 Open API 일봉 (키가 없으면 네이버 일봉)
- 시가총액: 종가 × 상장주식수 (KIS 상장주식수, 없으면 DART 주식총수현황)
- 발표일 시총: 공시일 종가 기준 → 작성 대상(기본 300억~5,000억) 판정
"""
import logging
import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache

import holidays
import requests

from . import config, db

log = logging.getLogger(__name__)
CLOSE_TIME = time(15, 30)


# ----------------------------------------------------------------------
# 영업일
# ----------------------------------------------------------------------
@lru_cache(maxsize=8)
def _kr_holidays(year: int) -> set:
    s = set(holidays.country_holidays("KR", years=[year]).keys())
    s.add(date(year, 12, 31))  # 연말 휴장
    return s


def _extra_holidays() -> set:
    p = config.HOLIDAY_FILE
    out = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", line)
            if m:
                out.add(date(*map(int, m.groups())))
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _kr_holidays(d.year) and d not in _extra_holidays()


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def price_basis_date(now: datetime | None = None) -> date:
    """가격 기준일: 영업일 15:30 이후 → 당일, 그 외 → 직전 영업일"""
    now = now or datetime.now()
    if is_business_day(now.date()) and now.time() >= CLOSE_TIME:
        return now.date()
    return prev_business_day(now.date())


# ----------------------------------------------------------------------
# 한국투자증권 Open API
# ----------------------------------------------------------------------
def kis_enabled() -> bool:
    return bool(config.KIS_APP_KEY and config.KIS_APP_SECRET)


def kis_token() -> str:
    tok, exp = db.get_meta("kis_token"), db.get_meta("kis_token_exp")
    if tok and exp and exp > (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"):
        return tok
    r = requests.post(f"{config.KIS_BASE_URL}/oauth2/tokenP", timeout=15,
                      json={"grant_type": "client_credentials", "appkey": config.KIS_APP_KEY,
                            "appsecret": config.KIS_APP_SECRET})
    r.raise_for_status()
    js = r.json()
    db.set_meta("kis_token", js["access_token"])
    db.set_meta("kis_token_exp", js.get("access_token_token_expired")
                or (datetime.now() + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S"))
    return js["access_token"]


def kis_daily(code: str, start: date, end: date) -> tuple[dict[str, int], int | None]:
    """일봉(원주가) → ({YYYYMMDD: 종가}, 현재 상장주식수)"""
    r = requests.get(
        f"{config.KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers={"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {kis_token()}",
                 "appkey": config.KIS_APP_KEY, "appsecret": config.KIS_APP_SECRET,
                 "tr_id": "FHKST03010100", "custtype": "P"},
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"},
        timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("rt_cd") not in (None, "0"):
        raise RuntimeError(f"KIS {js.get('msg_cd')}: {js.get('msg1')}")
    closes = {}
    for it in js.get("output2") or []:
        d, c = it.get("stck_bsop_date"), _to_int(it.get("stck_clpr"))
        if d and c:
            closes[d] = c
    shares = _to_int((js.get("output1") or {}).get("lstn_stcn")) or None
    return closes, shares


# ----------------------------------------------------------------------
# 종가 (KIS 우선, 키가 없으면 네이버 일봉)
# ----------------------------------------------------------------------
def parse_fchart(xml: str) -> dict[str, int]:
    """<item data="20261002|시가|고가|저가|종가|거래량" /> → {YYYYMMDD: 종가}"""
    out = {}
    for m in re.finditer(r'data="(\d{8})\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|', xml):
        try:
            out[m.group(1)] = int(float(m.group(5)))
        except ValueError:
            continue
    return out


def fetch_closes(code: str, basis: date) -> tuple[dict[str, int], int | None]:
    """KIS 우선, 실패하면 네이버 일봉으로 대체"""
    if kis_enabled():
        try:
            return kis_daily(code, basis - timedelta(days=14), basis)
        except Exception as e:  # noqa: BLE001
            LAST_ERROR["kis"] = f"KIS 실패: {str(e)[:200]}"
            log.warning("KIS 조회 실패 %s → 네이버로 대체: %s", code, e)
    r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                     params={"symbol": code, "timeframe": "day", "count": 400, "requestType": 0},
                     headers=config.HTTP_HEADERS, timeout=15)
    r.raise_for_status()
    return parse_fchart(r.content.decode("euc-kr", errors="replace")), None


LAST_ERROR: dict = {}
_live: dict = {}  # 장 마감 직후(확정 전) 값 임시 보관 {(code, basis): (close, dt, ts)}


def get_close(code: str, basis: date, offline=False) -> tuple[int | None, str | None]:
    """basis 일 종가 (그날 거래가 없으면 그 이전 마지막 종가). 반환 (종가, 실제 일자 YYYYMMDD)"""
    key = basis.strftime("%Y%m%d")
    with db.conn() as c:
        r = c.execute("SELECT close, dt FROM prices WHERE code=? AND basis=?", (code, key)).fetchone()
    if r:
        return r["close"], r["dt"]
    lv = _live.get((code, key))
    if lv and (offline or (datetime.now() - lv[2]).seconds < 300):
        return lv[0], lv[1]
    if offline:
        return None, None
    try:
        closes, shares = fetch_closes(code, basis)
    except Exception as e:  # noqa: BLE001
        log.warning("종가 조회 실패 %s: %s", code, e)
        LAST_ERROR["price"] = (LAST_ERROR.get("kis", "") + " / " if LAST_ERROR.get("kis") else "") + \
            f"네이버 종가 조회 실패: {str(e)[:150]}"
        return None, None
    if shares:
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO shares(corp_code, shares, basis, fetched) VALUES(?,?,?,?)",
                      (f"S{code}", shares, "상장주식수(KIS)", datetime.now().strftime("%Y-%m-%d")))
    cands = sorted(d for d in closes if d <= key)
    if not cands:
        return None, None
    dt = cands[-1]
    now = datetime.now()
    if basis == now.date() and now.time() < time(16, 0):
        _live[(code, key)] = (closes[dt], dt, now)
        return closes[dt], dt  # 장 마감 직후엔 DB에 저장하지 않음 (종가 확정 대기)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO prices(code, basis, close, dt) VALUES(?,?,?,?)",
                  (code, key, closes[dt], dt))
    return closes[dt], dt


# ----------------------------------------------------------------------
# 상장주식수: KIS(lstn_stcn) 우선, 없으면 DART 주식의 총수 현황
# ----------------------------------------------------------------------
REPORTS = [("11014", "3분기보고서"), ("11012", "반기보고서"), ("11013", "1분기보고서"), ("11011", "사업보고서")]


def _to_int(s) -> int:
    s = re.sub(r"[^\d]", "", str(s or ""))
    return int(s) if s else 0


def fetch_shares(corp_code: str) -> tuple[int | None, str | None]:
    if not config.DART_API_KEY or not corp_code:
        return None, None
    year = datetime.now().year
    for y in (year, year - 1):
        for rc, rname in REPORTS:
            try:
                js = requests.get("https://opendart.fss.or.kr/api/stockTotqySttus.json",
                                  params={"crtfc_key": config.DART_API_KEY, "corp_code": corp_code,
                                          "bsns_year": str(y), "reprt_code": rc}, timeout=15).json()
            except Exception as e:  # noqa: BLE001
                log.warning("주식총수 조회 실패 %s: %s", corp_code, e)
                return None, None
            if js.get("status") != "000":
                continue
            rows = js.get("list", [])
            common = next((r for r in rows if "보통" in (r.get("se") or "")), None) or \
                next((r for r in rows if "합계" in (r.get("se") or "")), None)
            if common and _to_int(common.get("istc_totqy")):
                return _to_int(common["istc_totqy"]), f"{y} {rname}"
    return None, None


def get_shares(stock_code: str, corp_code: str | None, offline=False) -> tuple[int | None, str | None]:
    with db.conn() as c:
        r = c.execute("SELECT shares, basis, fetched FROM shares WHERE corp_code=?", (f"S{stock_code}",)).fetchone() \
            or c.execute("SELECT shares, basis, fetched FROM shares WHERE corp_code=?", (corp_code or "",)).fetchone()
    if r and (offline or r["fetched"] >= (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")):
        return r["shares"], r["basis"]
    if offline:
        return (r["shares"], r["basis"]) if r else (None, None)
    n, basis = fetch_shares(corp_code)   # KIS 주식수가 없을 때 DART로 보완
    if n:
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO shares(corp_code, shares, basis, fetched) VALUES(?,?,?,?)",
                      (corp_code, n, basis, datetime.now().strftime("%Y-%m-%d")))
        return n, basis
    return (r["shares"], r["basis"]) if r else (None, None)


# ----------------------------------------------------------------------
# 한국거래소 Open API: 일별매매정보 (그 날 종가·상장주식수·시가총액 공식값)
# ----------------------------------------------------------------------
KRX_URLS = ["https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",   # 유가증권
            "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",   # 코스닥
            "https://data-dbg.krx.co.kr/svc/apis/sto/knx_bydd_trd"]   # 코넥스


def krx_enabled() -> bool:
    return bool(config.KRX_API_KEY)


def krx_day(basis: date) -> dict:
    """{종목코드: (종가, 시가총액, 상장주식수)} — 날짜별 1회 받아 DB에 저장"""
    key = basis.strftime("%Y%m%d")
    with db.conn() as c:
        rows = c.execute("SELECT code, close, mktcap, shares FROM krx_daily WHERE basis=?", (key,)).fetchall()
    if rows:
        return {r["code"]: (r["close"], r["mktcap"], r["shares"]) for r in rows}
    out = {}
    for url in KRX_URLS:
        try:
            r = requests.get(url, params={"basDd": key}, headers={"AUTH_KEY": config.KRX_API_KEY}, timeout=30)
            r.raise_for_status()
            for it in r.json().get("OutBlock_1") or []:
                code = (it.get("ISU_CD") or it.get("ISU_SRT_CD") or "").strip()
                code = code[-6:] if len(code) >= 6 else code
                close, cap, sh = _to_int(it.get("TDD_CLSPRC")), _to_int(it.get("MKTCAP")), _to_int(it.get("LIST_SHRS"))
                if code and close:
                    out[code] = (close, cap or close * sh, sh)
        except Exception as e:  # noqa: BLE001
            LAST_ERROR["krx"] = f"KRX 조회 실패({url.rsplit('/', 1)[-1]} {key}): {str(e)[:150]}"
            log.warning("KRX 일별매매정보 실패 %s %s: %s", url, key, e)
    if out and len(out) > 1500:   # 유가+코스닥이 다 들어왔을 때만 저장 (데이터 미공개일·부분 실패 방지)
        with db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO krx_daily VALUES(?,?,?,?,?)",
                          [(key, k, v[0], v[1], v[2]) for k, v in out.items()])
    return out


def kis_now(code: str) -> tuple[int | None, int | None, int | None]:
    """한투 주식현재가 시세 → (현재가, 상장주식수, 시가총액[원]). 장 마감 뒤 = 당일 종가 기준"""
    r = requests.get(
        f"{config.KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers={"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {kis_token()}",
                 "appkey": config.KIS_APP_KEY, "appsecret": config.KIS_APP_SECRET,
                 "tr_id": "FHKST01010100", "custtype": "P"},
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("rt_cd") not in (None, "0"):
        raise RuntimeError(f"KIS {js.get('msg_cd')}: {js.get('msg1')}")
    o = js.get("output") or {}
    avls = _to_int(o.get("hts_avls"))          # HTS 시가총액 (억원)
    return _to_int(o.get("stck_prpr")) or None, _to_int(o.get("lstn_stcn")) or None, (avls * 10**8 if avls else None)


def announce_cap(stock_code: str, corp_code: str | None, basis: date, now: datetime | None = None) -> dict:
    """발표일 시총
    1) 공시 당일 → 한투 '시가총액'(hts_avls) 그대로. 장 마감(15:30) 뒤 조회면 확정, 장중이면 잠정
    2) 지난 날짜 → 한국거래소 공식 일별매매정보의 시가총액 (KRX_API_KEY) → 확정
    3) 둘 다 안 되면 → 종가 × 현재 상장주식수 → 추정 (다음 실행에 다시 시도)"""
    now = now or datetime.now()
    out = {"close": None, "close_dt": None, "mktcap": None, "shares": None, "src": None, "final": False}
    if not stock_code:
        return out
    if basis == now.date() and kis_enabled():
        try:
            px, sh, cap = kis_now(stock_code)
            if px and (cap or sh):
                closed = now.time() >= time(15, 35)
                out.update(close=px, mktcap=cap or px * sh, shares=sh, close_dt=basis.strftime("%Y%m%d"),
                           src="한투" if closed else "한투(장중)", final=closed)
                return out
        except Exception as e:  # noqa: BLE001
            LAST_ERROR["kis"] = f"한투 시세 조회 실패: {str(e)[:150]}"
    if krx_enabled() and basis < now.date():
        hit = krx_day(basis).get(stock_code)
        if hit:
            out.update(close=hit[0], mktcap=hit[1], shares=hit[2], close_dt=basis.strftime("%Y%m%d"),
                       src="KRX", final=True)
            return out
    q = cap_on(stock_code, corp_code, basis)
    if q["close"]:
        sh, _ = get_shares(stock_code, corp_code, offline=True)
        out.update(close=q["close"], close_dt=q["close_dt"], mktcap=q["mktcap"], shares=sh,
                   src="추정(현재 주식수)", final=False)
    return out


def cap_on(stock_code: str, corp_code: str | None, basis: date, offline=False) -> dict:
    """basis 일 종가·시총"""
    out = {"close": None, "close_dt": None, "mktcap": None, "shares_basis": None}
    if not stock_code:
        return out
    close, dt = get_close(stock_code, basis, offline)
    out["close"], out["close_dt"] = close, dt
    if close:
        shares, sb = get_shares(stock_code, corp_code, offline)
        if shares:
            out["mktcap"], out["shares_basis"] = close * shares, sb
    return out


def quote(stock_code: str, corp_code: str | None, now: datetime | None = None, offline=False) -> dict:
    """현재 기준 종가·시총 (15:30 전 전일, 이후 당일)"""
    now = now or datetime.now()
    basis = price_basis_date(now)
    out = cap_on(stock_code, corp_code, basis, offline)
    out["basis_label"] = "당일 종가" if basis == now.date() else "전일 종가"
    return out


def announce_basis(disclosed: str | None, rcept_dt: str) -> date:
    """발표일 시총 기준일: 공시일(영업일 아니면 직전 영업일)"""
    d = datetime.strptime((disclosed or "")[:10], "%Y-%m-%d").date() if disclosed else \
        datetime.strptime(rcept_dt, "%Y%m%d").date()
    return d if is_business_day(d) else prev_business_day(d)


def is_final(basis: date, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return basis < now.date() or (basis == now.date() and now.time() >= time(15, 35))


def in_target(cap) -> bool | None:
    if not cap:
        return None
    return config.CAP_MIN_EOK * 1e8 <= cap <= config.CAP_MAX_EOK * 1e8


def fmt_won(v) -> str:
    return f"{v:,}원" if v else "-"


def fmt_cap(v) -> str:
    """시가총액 → '1조 2,345억' / '987억'"""
    if not v:
        return "-"
    eok = round(v / 1e8)
    jo, rest = divmod(eok, 10000)
    if jo:
        return f"{jo:,}조 {rest:,}억" if rest else f"{jo:,}조"
    return f"{rest:,}억"


def fmt_dt(dt: str | None) -> str:
    return f"{dt[4:6]}/{dt[6:]}" if dt else ""

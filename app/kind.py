"""KIND(한국거래소 공시시스템) - 밸류업 공시현황 목록, 공시뷰어, 첨부파일 확인"""
import logging
import re
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger(__name__)
BASE = "https://kind.krx.co.kr"


class KindClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(config.HTTP_HEADERS)
        self._warm = False

    def _warmup(self):
        if not self._warm:
            try:
                self.s.get(f"{BASE}/main.do?method=loadInitPage&scrnmode=1", timeout=15)
            except requests.RequestException:
                pass
            self._warm = True

    def _get(self, url, **kw):
        self._warmup()
        r = self.s.get(url, timeout=20, **kw)
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------
    # 1) 기업 밸류업 > 공시현황 목록
    # ------------------------------------------------------------------
    def fetch_valueup_list(self, from_date: str, to_date: str, page_size: int = 100) -> list[dict]:
        """from/to: YYYY-MM-DD. 반환: [{acptno, disclosed_at, corp_name, market, title}]"""
        self._warmup()
        out, page = [], 1
        while True:
            data = {
                "method": "valueupDisclsStatSub", "currentPageSize": str(page_size),
                "pageIndex": str(page), "orderMode": "1", "orderStat": "D",
                "forward": "valueupDisclsStat_sub", "fromDate": from_date, "toDate": to_date,
                "marketType": "", "searchCorpName": "", "repIsuSrtCd": "", "isurCd": "",
                "allRepIsuSrtCd": "",
            }
            r = self.s.post(f"{BASE}/valueup/disclsstat.do", data=data, timeout=20,
                            headers={"Referer": f"{BASE}/valueup/disclsstat.do?method=valueupDisclsStatMain"})
            r.raise_for_status()
            rows, total = parse_valueup_list(r.text)
            out += rows
            if not rows or len(out) >= total:
                break
            page += 1
        return out

    # ------------------------------------------------------------------
    # 2) 공시뷰어: 본문/첨부서류 목록
    # ------------------------------------------------------------------
    def fetch_viewer(self, acptno: str) -> dict:
        r = self._get(f"{BASE}/common/disclsviewer.do", params={"method": "search", "acptno": acptno})
        return parse_viewer(r.text)

    def doc_url(self, doc_no: str) -> str | None:
        """docNo → 실제 문서(htm) URL"""
        r = self._get(f"{BASE}/common/disclsviewer.do",
                      params={"method": "searchContents", "docNo": doc_no})
        m = re.search(r"setPath\(\s*'[^']*'\s*,\s*'([^']+)'", r.text)
        if not m:
            return None
        return m.group(1).replace("http://", "https://")

    def fetch_html(self, url: str) -> str:
        r = self._get(url)
        return decode_html(r.content)

    def download(self, url: str, dest) -> int:
        r = self._get(url, stream=True)
        n = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                n += len(chunk)
        return n

    # ------------------------------------------------------------------
    # 3) 종합 점검: 첨부 인정 여부 + 본문 요약 필드
    # ------------------------------------------------------------------
    def inspect(self, acptno: str) -> dict:
        v = self.fetch_viewer(acptno)
        res = {"title": v["title"], "attachments": [], "body": {}, "attached_docs": v["attached"],
               "prior_dates": v.get("prior_dates", [])}
        # 본문
        if v["main"]:
            url = self.doc_url(v["main"][0][0])
            if url:
                html = self.fetch_html(url)
                res["body"] = parse_main_body(html)
                res["attachments"] += find_file_links(html, url)
        # 첨부서류
        for doc_no, doc_title in v["attached"]:
            url = self.doc_url(doc_no)
            if not url:
                continue
            html = self.fetch_html(url)
            for a in find_file_links(html, url):
                a["doc_title"] = doc_title
                res["attachments"].append(a)
        # 중복 제거
        seen, uniq = set(), []
        for a in res["attachments"]:
            if a["url"] not in seen:
                seen.add(a["url"])
                uniq.append(a)
        res["attachments"] = uniq
        res["valid"] = any(a["ext"] in config.VALID_EXTS for a in uniq)
        return res


# ======================================================================
# 파서 (네트워크 없이 테스트 가능)
# ======================================================================
def decode_html(raw: bytes) -> str:
    head = raw[:2000].decode("ascii", errors="ignore")
    m = re.search(r"charset=([\w-]+)", head, re.I)
    enc = (m.group(1) if m else "utf-8").lower()
    for e in (enc, "utf-8", "cp949"):
        try:
            return raw.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_valueup_list(html: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tds[3].find("a", onclick=True)
        if not a:
            continue
        m = re.search(r"openDisclsViewer\('(\d{14})'", a["onclick"])
        if not m:
            continue
        img = tds[2].find("img")
        name_a = tds[2].find("a")
        rows.append({
            "acptno": m.group(1),
            "disclosed_at": tds[1].get_text(strip=True),
            "corp_name": (name_a.get("title") or name_a.get_text(strip=True)).strip() if name_a else "",
            "market": {"유가증권": "유가", "코스닥": "코스닥", "코넥스": "코넥스"}.get(
                img.get("alt", "") if img else "", img.get("alt", "") if img else ""),
            "title": (("[정정]" if "[정정]" in a.get_text() and "정정" not in (a.get("title") or "") else "")
                      + (a.get("title") or a.get_text(strip=True)).strip()),
        })
    m = re.search(r"전체\s*<em>\s*([\d,]+)\s*</em>", html)
    total = int(m.group(1).replace(",", "")) if m else len(rows)
    return rows, total


def parse_viewer(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def opts(sel_id):
        out = []
        sel = soup.find("select", id=sel_id)
        if not sel:
            return out
        for o in sel.find_all("option"):
            val = (o.get("value") or "").strip()
            if not val or not re.match(r"^\d{8,}", val):
                continue
            out.append((val, o.get_text(" ", strip=True)))
        return out

    mains = opts("mainDoc")
    # 정정공시면 본문 목록에 원본(N)과 최신 정정본(Y)이 함께 있음 → 최신(Y)을 앞으로
    latest = [(v.split("|")[0], t) for v, t in mains if v.endswith("|Y")]
    older = [(v.split("|")[0], t) for v, t in mains if not v.endswith("|Y")]
    prior_dates = []
    for _, t in older:
        m = re.search(r"\((\d{4})\.(\d{2})\.(\d{2})\)", t)
        if m:
            prior_dates.append("".join(m.groups()))
    t = soup.find("title")
    return {
        "title": t.get_text(strip=True) if t else "",
        "main": latest + older,
        "prior_dates": prior_dates,   # 정정 전 공시 날짜들 (YYYYMMDD)
        "attached": opts("attachedDoc"),
    }


def find_file_links(html: str, base_url: str) -> list[dict]:
    """문서 htm 안의 첨부파일 링크(pdf/hwp/doc 등) 추출"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = re.search(r"\.([A-Za-z0-9]{2,5})(?:$|[?#])", href)
        if not m:
            continue
        ext = m.group(1).lower()
        if ext not in config.FILE_EXTS:
            continue
        url = urljoin(base_url, quote(href, safe="/:%?=&#._-()"))
        name = a.get_text(" ", strip=True).strip("[] ").strip() or href.rsplit("/", 1)[-1]
        out.append({"name": name, "url": url, "ext": ext})
    return out


LABELS = [
    ("plan_name", r"계획서\s*명칭"),
    ("main_content", r"주요\s*내용"),
    ("high_dividend", r"고배당기업\s*여부"),
    ("pbr_plan", r"PBR\s*개선\s*계획"),
    ("tech_growth", r"기술성장기업의\s*기업가치\s*제고\s*계획"),
    ("decision_date", r"결정일자"),
    ("published", r"관련\s*자료\s*게재일시"),
    ("etc", r"기타\s*투자판단과\s*관련한\s*중요사항"),
]


def parse_main_body(html: str) -> dict:
    """기업가치 제고 계획(자율공시) 본문 표 → 주요 필드"""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    # 라벨 위치 찾기 ("1. 계획서 명칭" 처럼 번호가 붙은 위치 우선)
    pos = []
    for key, pat in LABELS:
        m = re.search(r"(?:^|\n)\s*\d+\.\s*[^\n]{0,40}?" + pat, text)
        if not m:
            m = re.search(pat, text)
        if m:
            pos.append((m.start(), m.end(), key))
    pos.sort()
    fields = {}
    for i, (st, en, key) in enumerate(pos):
        nxt = pos[i + 1][0] if i + 1 < len(pos) else len(text)
        val = text[en:nxt].strip(" \n:")
        fields[key] = re.sub(r"\n+", "\n", val).strip()

    out = {"body_text": text[:20000]}
    out["plan_name"] = fields.get("plan_name", "").split("\n")[0].strip()
    out["main_content"] = fields.get("main_content", "").strip()
    hd = fields.get("high_dividend", "")
    out["high_dividend"] = "미해당" if "미해당" in hd[:20] else ("해당" if "해당" in hd[:20] else "")
    pb = fields.get("pbr_plan", "")
    out["pbr_plan"] = "미포함" if "미포함" in pb[:20] else ("포함" if "포함" in pb[:20] else "")
    m = re.search(r"\d{4}[-.]\d{2}[-.]\d{2}", fields.get("decision_date", ""))
    out["decision_date"] = m.group(0) if m else ""
    out["related"] = parse_related(text)
    return out


# ======================================================================
# 계획 / 이행 구분
# ======================================================================
TYPE_PLAN, TYPE_IMPL, TYPE_BOTH, TYPE_REFILE = "계획", "이행", "이행+계획", "재공시"


def classify(title: str, plan_name: str = "", main_content: str = "", att_names=()) -> str:
    """공시 제목 괄호·계획서 명칭·첨부파일명·주요내용으로 계획/이행 판별.
    KIND 제목은 대부분 '기업가치 제고 계획(자율공시)'로만 나와서 본문의 '계획서 명칭'이 핵심 단서."""
    def n(s):
        return re.sub(r"\s+", "", s or "")

    head = n(title) + "|" + n(plan_name) + "|" + "|".join(n(a) for a in att_names)
    body = n(main_content)[:300]
    impl = "이행" in head or bool(re.search(r"이행(현황|실적|점검|평가|내역|사항)", body))
    # '2025년 이행현황 및 2026년 기업가치제고계획' 처럼 새 계획도 같이 낸 경우
    plan_too = bool(re.search(r"(및|&|과|와|,)\d{4}년?[^|]{0,30}기업가치제고계획", head)) or \
        bool(re.search(r"기업가치제고계획\(?(수립|신규|갱신|업데이트)", head))
    refile = "재공시" in head or "고배당기업표시" in head
    if impl and plan_too:
        return TYPE_BOTH
    if impl:
        return TYPE_IMPL
    if refile:
        return TYPE_REFILE
    return TYPE_PLAN


def parse_related(text: str) -> list[dict]:
    """본문 끝 '※ 관련공시 2026-09-10 기업가치 제고 계획 예고 2026-03-31 …' → [{date,title}]"""
    m = re.search(r"※\s*관련\s*공시(.*)$", text or "", re.S)
    if not m:
        return []
    seg = re.sub(r"\s+", " ", m.group(1)).strip()
    parts = re.split(r"(\d{4}-\d{2}-\d{2})", seg)
    out = []
    for i in range(1, len(parts) - 1, 2):
        t = parts[i + 1].strip(" -")
        if t:
            out.append({"date": parts[i], "title": t[:80]})
    return out

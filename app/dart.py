"""DART OpenAPI - 공시검색(list.json) 백업 감지 + 상장사 코드표(corpCode.xml)"""
import io
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

from . import config

log = logging.getLogger(__name__)
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
MARKET = {"Y": "유가", "K": "코스닥", "N": "코넥스", "E": "기타"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def is_valueup(report_nm: str) -> bool:
    """기업가치제고계획 본공시(자율공시/정정 포함) 여부. '예고'는 제외"""
    n = _norm(report_nm)
    return config.REPORT_KEYWORD in n and "예고" not in n


def is_correction(report_nm: str) -> bool:
    return "정정" in _norm(report_nm)


def to_kind_acptno(rcept_no: str) -> str:
    """DART 거래소공시 접수번호(…90xxxx) → KIND 접수번호(…00xxxx)"""
    return rcept_no[:8] + "00" + rcept_no[10:] if len(rcept_no) == 14 else rcept_no


def to_dart_rcept_no(acptno: str) -> str:
    """KIND 접수번호 → DART 접수번호 (거래소 공시는 9~10번째 자리가 '90')"""
    return acptno[:8] + "90" + acptno[10:] if len(acptno) == 14 else acptno


def norm_name(name: str) -> str:
    n = re.sub(r"\(주\)|㈜|주식회사|\s+", "", name or "")
    return n.upper()


def fetch_corp_map(session: requests.Session | None = None) -> dict:
    """corpCode.xml → {정규화된 회사명: {corp_code, stock_code, corp_name}} (상장사만)"""
    s = session or requests.Session()
    r = s.get("https://opendart.fss.or.kr/api/corpCode.xml",
              params={"crtfc_key": config.DART_API_KEY}, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    out = {}
    for el in ET.fromstring(xml).iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not stock:
            continue
        name = (el.findtext("corp_name") or "").strip()
        out[norm_name(name)] = {"corp_code": el.findtext("corp_code"), "stock_code": stock, "corp_name": name}
    return out


def fetch_list(bgn_de: str, end_de: str, session: requests.Session | None = None, corp_code: str | None = None) -> list[dict]:
    """기간 내 거래소공시(I) 전체를 받아 기업가치제고 공시만 반환 (KIND 감지의 백업용)
    corp_code를 주면 그 회사만 (기간 제한 없이 과거 공시까지)"""
    s = session or requests.Session()
    out, page = [], 1
    while True:
        params = {
            "crtfc_key": config.DART_API_KEY,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": "I",          # 거래소공시
            "page_no": page,
            "page_count": 100,
            "sort": "date",
            "sort_mth": "desc",
        }
        if corp_code:
            params["corp_code"] = corp_code
        r = s.get(LIST_URL, params=params, timeout=20)
        r.raise_for_status()
        js = r.json()
        st = js.get("status")
        if st == "013":                # 조회된 데이터 없음
            break
        if st != "000":
            raise RuntimeError(f"DART API 오류 {st}: {js.get('message')}")
        for it in js.get("list", []):
            if is_valueup(it.get("report_nm", "")):
                it["market"] = MARKET.get(it.get("corp_cls", ""), it.get("corp_cls", ""))
                out.append(it)
        if page >= int(js.get("total_page", 1)):
            break
        page += 1
        time.sleep(0.3)
    return out


# ----------------------------------------------------------------------
# DART 웹 뷰어로 첨부파일 확인 (KIND가 막혔을 때 대신 사용)
#   main.do?rcpNo=…  → <select id="att"> 첨부 목록 (값: rcpNo=…&dcmNo=…)
#   report/viewer.do?rcpNo=…&dcmNo=… → 첨부문서 안의 파일 링크 (/report/download.do?dcmNo=…&flNm=파일명.pdf)
#   첨부 목록이 '+첨부선택+' 하나뿐이면 → 첨부 없음 (KIND에서도 동일하게 첨부 없음)
# ----------------------------------------------------------------------
WEB = "https://dart.fss.or.kr"


class DartBlocked(Exception):
    """DART 웹 접속 거부·구조 변경 → 판단 보류"""


class DartWebClient:
    def __init__(self, delay=1.0):
        self.s = requests.Session()
        self.s.headers.update(config.HTTP_HEADERS)
        self.delay = delay   # DART는 빠르게 연속 요청하면 '거부' 페이지를 줌

    def _html(self, url) -> str:
        from .kind import decode_html
        if self.delay:
            time.sleep(self.delay)
        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        html = decode_html(r.content)
        if re.search(r"<title>\s*거부\s*</title>", html):
            raise DartBlocked("DART 접속 거부(요청 과다)")
        return html

    def _viewer(self, rcp, dcm) -> str:
        return self._html(f"{WEB}/report/viewer.do?rcpNo={rcp}&dcmNo={dcm}&eleId=0&offset=0&length=0&dtd=HTML")

    def inspect(self, rcept_no: str) -> dict:
        from .kind import parse_main_body
        main = self._html(f"{WEB}/dsaf001/main.do?rcpNo={rcept_no}")
        atts, main_dcm = parse_att_options(main), parse_main_dcm(main, rcept_no)
        if atts is None:
            raise DartBlocked("DART 뷰어에서 첨부 목록을 찾지 못함")
        res = {"attachments": [], "body": {}, "attached_docs": [(d, t) for _, d, t in atts],
               "prior_dates": [], "source": "dart"}
        if main_dcm:
            try:
                html = self._viewer(rcept_no, main_dcm)
                res["body"] = parse_main_body(html)
            except Exception as e:  # noqa: BLE001
                log.info("DART 본문 읽기 실패(첨부 확인은 계속): %s", e)
        for rcp, dcm, title in atts:
            html = self._viewer(rcp, dcm)
            for a in parse_file_links(html):
                a["doc_title"] = title
                res["attachments"].append(a)
        res["valid"] = any(a["ext"] in config.VALID_EXTS for a in res["attachments"])
        return res


def parse_att_options(html: str):
    """첨부 select → [(rcpNo, dcmNo, 제목)]. select 자체가 없으면 None(판단 불가)"""
    m = re.search(r'<select[^>]*id="att"[^>]*>(.*?)</select>', html, re.S)
    if not m:
        return None
    out = []
    for val, text in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S):
        val = val.replace("&amp;", "&")
        q = dict(re.findall(r"(rcpNo|dcmNo)=(\d+)", val))
        if "dcmNo" in q:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", text)).strip()
            out.append((q.get("rcpNo", ""), q["dcmNo"], title))
    return out


def parse_main_dcm(html: str, rcept_no: str):
    m = re.search(r'viewDoc\("%s",\s*"(\d+)"' % rcept_no, html) or \
        re.search(r"openPdfDownload\('%s',\s*'(\d+)'\)" % rcept_no, html)
    return m.group(1) if m else None


def parse_file_links(html: str) -> list[dict]:
    """DART 첨부문서 안의 파일 링크: /report/download.do?dcmNo=…&flNm=파일명.pdf"""
    from urllib.parse import unquote_plus
    out, seen = [], set()
    for href in re.findall(r'href="([^"]*download\.do[^"]*)"', html):
        href = href.replace("&amp;", "&")
        m = re.search(r"flNm=([^&#]+)", href)
        if not m:
            continue
        name = unquote_plus(m.group(1))
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        url = href if href.startswith("http") else WEB + href
        if ext in config.FILE_EXTS and url not in seen:
            seen.add(url)
            out.append({"name": name, "url": url, "ext": ext})
    return out

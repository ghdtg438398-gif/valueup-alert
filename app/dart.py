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


def fetch_list(bgn_de: str, end_de: str, session: requests.Session | None = None) -> list[dict]:
    """기간 내 거래소공시(I) 전체를 받아 기업가치제고 공시만 반환 (KIND 감지의 백업용)"""
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

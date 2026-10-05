"""(선택) Claude API로 첨부 PDF 핵심 요약. ANTHROPIC_API_KEY가 없으면 건너뜀"""
import base64
import logging
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

PROMPT = """다음은 한국 상장사가 거래소에 제출한 '기업가치 제고 계획(밸류업)' 첨부 자료입니다.
셀사이드 애널리스트가 30초 안에 파악할 수 있도록 개조식으로 요약하세요.

형식(각 항목 1~2줄, 명사형/개조식 종결, 수치는 원문 그대로):
■ 핵심 목표: (ROE·PBR·매출·영업이익·시총 등 정량 목표와 목표 연도)
■ 주주환원: (배당성향/DPS, 자사주 매입·소각, TSR 등 구체 수치와 기간)
■ 성장·자본배분: (CAPEX, M&A, 사업 재편 등)
■ 거버넌스·소통: (이사회, IR 계획 등. 없으면 생략)
■ 체크포인트: (애널리스트가 확인해야 할 점 1~2개: 목표의 현실성, 기존 계획 대비 변화, 모호한 부분)

원문에 없는 내용은 추측하지 말고 '언급 없음'으로 표기하세요."""


def enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def summarize_pdf(path: Path, extra_context: str = "") -> str | None:
    if not enabled() or not path or not Path(path).exists():
        return None
    data = Path(path).read_bytes()
    if len(data) > 30 * 1024 * 1024:
        log.warning("PDF가 너무 커서 요약 생략: %s", path)
        return None
    content = [
        {"type": "document",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": PROMPT + (f"\n\n[공시 본문 참고]\n{extra_context[:3000]}" if extra_context else "")},
    ]
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1200,
                  "messages": [{"role": "user", "content": content}]},
            timeout=180,
        )
        r.raise_for_status()
        js = r.json()
        return "".join(b.get("text", "") for b in js.get("content", []) if b.get("type") == "text").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("AI 요약 실패: %s", e)
        return None

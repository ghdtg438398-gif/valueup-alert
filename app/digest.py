"""영업일 정기 리포트 (기본 09:00, 15:00)"""
import html
import logging
import re
from datetime import datetime, timedelta

from . import config, db, market, telegram

log = logging.getLogger(__name__)
WEEK = "월화수목금토일"
TYPE_ICON = {"계획": "🟦", "이행": "🟩", "이행+계획": "🟪", "재공시": "⬜"}
MAX_LEN = 3800


def due_slot(now: datetime | None = None) -> str | None:
    """지금 보내야 할 리포트 슬롯 'YYYY-MM-DD HH:MM' (없으면 None)"""
    now = now or datetime.now()
    if not market.is_business_day(now.date()):
        return None
    due = None
    for t in config.DIGEST_TIMES:
        hh, mm = map(int, t.split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # 15:30 이후 회차는 종가 확정을 기다렸다가 5분 뒤 발송
        start = slot + timedelta(minutes=5 if (hh, mm) >= (15, 30) else 0)
        if start <= now <= slot + timedelta(minutes=config.DIGEST_GRACE_MIN):
            key = slot.strftime("%Y-%m-%d %H:%M")
            if not db.digest_sent(key):
                due = key
    return due


def key_line(f: dict, n: int = 140) -> str:
    """알림용 한 줄 요지: AI 요약 첫 항목 > 주요내용에서 수치가 있는 줄"""
    if f.get("ai_summary"):
        for ln in f["ai_summary"].splitlines():
            ln = ln.strip(" ■-•*")
            if len(ln) > 8:
                return ln[:n]
    lines = [ln.strip(" -·*") for ln in (f.get("main_content") or "").splitlines()]
    lines = [ln for ln in lines if len(ln) > 6]
    num = [ln for ln in lines if re.search(r"\d+(\.\d+)?\s*(%|배|억|조|원)", ln)]
    pick = num[:2] or lines[:2]
    s = " / ".join(pick)
    return s[:n] + ("…" if len(s) > n else "")


def target_items(filings: list[dict], only_analyst: str | None = None) -> list[tuple]:
    """알림 대상: 발표일 시총 범위 안(미확인 포함) → [(담당자들, 공시)] 담당자순"""
    items = []
    for f in filings:
        if f.get("target") is False:
            continue
        ans = db.analysts_for(f.get("stock_code") or "")
        if only_analyst and only_analyst not in ans:
            continue
        if config.ALERT_SCOPE == "coverage" and not ans:
            continue
        items.append((ans, f))
    items.sort(key=lambda x: (not x[0], x[0][0] if x[0] else "", x[1].get("disclosed_at") or ""))
    return items


def pub_buttons(items) -> list:
    """텔레그램 발간 체크 버튼 (누르면 다음 실행 때 대시보드에 반영)"""
    rows = []
    for i, (_, f) in enumerate(items, 1):
        nm = f["corp_name"][:8]
        rows.append([{"text": f"{i}.{nm} 계획발간", "callback_data": f"pub:{f['acptno']}:plan"},
                     {"text": "이행발간", "callback_data": f"pub:{f['acptno']}:impl"}])
    return rows[:40]


def build(filings: list[dict], now: datetime | None = None, only_analyst: str | None = None,
          label_time: datetime | None = None) -> list[str]:
    """작성 대상(발표일 시총 범위) 공시만: 종목명 / 담당자 / 발표일 시총 / 핵심 내용 / 공시 구분"""
    now = now or datetime.now()
    lt = label_time or now
    e = html.escape
    items = target_items(filings, only_analyst)
    lo, hi = market.fmt_cap(config.CAP_MIN_EOK * 1e8), market.fmt_cap(config.CAP_MAX_EOK * 1e8)
    title = "📊 <b>밸류업 공시</b>" + (f" · {e(only_analyst)}" if only_analyst else "")
    head = f"{title} · {lt:%m/%d}({WEEK[lt.weekday()]}) {lt:%H:%M}\n"
    head += (f"작성 대상 <b>{len(items)}건</b> (발표일 시총 {lo}~{hi})" if items
             else f"직전 알림 이후 작성 대상 공시 없음 (발표일 시총 {lo}~{hi})")
    blocks = []
    for i, (ans, f) in enumerate(items, 1):
        t = f.get("kind_type") or "계획"
        cap = market.fmt_cap(f.get("ann_cap")) if f.get("ann_cap") else "확인 중"
        prov = "" if f.get("ann_final") or not f.get("ann_cap") else " (잠정)"
        who = ", ".join(ans) if ans else "커버리지 외"
        b = [f"\n<b>{i}) {e(f['corp_name'])}</b> · 👤 {e(who)}",
             f"   {TYPE_ICON.get(t, '')} {e(t)} · 발표일 시총 <b>{cap}</b>{prov}"]
        kl = key_line(f)
        if kl:
            b.append(f"   ▸ {e(kl)}")
        if f.get("last_error", "") and str(f["last_error"]).startswith("첨부 미확인"):
            b.append("   ⚠ 첨부 미확인 (KIND 접속 불가 — 원문 확인 필요)")
        blocks.append("\n".join(b))
    foot = f'\n🔗 <a href="{e(config.DASHBOARD_URL)}">대시보드</a>' if config.DASHBOARD_URL else ""

    msgs, cur = [], head
    for blk in blocks:
        if len(cur) + len(blk) + 1 > MAX_LEN:
            msgs.append(cur)
            cur = "(계속)"
        cur += "\n" + blk
    if foot:
        if len(cur) + len(foot) > MAX_LEN:
            msgs.append(cur)
            cur = foot.strip()
        else:
            cur += "\n" + foot
    msgs.append(cur)
    return msgs


def send_digest(slot: str | None = None, now: datetime | None = None, dry_run=False) -> list[str]:
    now = now or datetime.now()
    filings = db.digest_candidates()
    if config.ALERT_SCOPE == "coverage":
        filings_for_mark = filings
        filings = [f for f in filings if db.analysts_for(f.get("stock_code") or "")]
    else:
        filings_for_mark = filings
    label = datetime.strptime(slot, "%Y-%m-%d %H:%M") if slot else now
    msgs = build(filings, now, label_time=label)
    if not dry_run:
        kb = pub_buttons(target_items(filings)) if config.PUB_BUTTONS else None
        for i, m in enumerate(msgs):
            telegram.send(config.TELEGRAM_CHAT_ID, m, kb if (kb and i == len(msgs) - 1) else None)
        if config.SEND_DM_TO_ANALYST:
            chats = {a["analyst"]: a["telegram_chat_id"] for a in db.coverage_summary() if a["telegram_chat_id"]}
            for a, cid in chats.items():
                mine = [f for f in filings if a in db.analysts_for(f.get("stock_code") or "")]
                if mine:
                    for m in build(mine, now, only_analyst=a, label_time=label):
                        telegram.send(cid, m)
        db.mark_digested([f["acptno"] for f in filings_for_mark], now.strftime("%Y-%m-%d %H:%M:%S"))
        if slot:
            db.record_digest(slot, len(filings))
    log.info("정기 리포트 %s: %d건", slot or "수동", len(filings))
    return msgs

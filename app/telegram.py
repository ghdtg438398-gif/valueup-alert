"""텔레그램 알림 + 간단한 봇 명령어(/register, /mine, /help)"""
import html
import logging
import time

import requests

from . import config, db, market

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def _call(method, **payload):
    if not config.TELEGRAM_BOT_TOKEN:
        log.info("[텔레그램 미설정] %s %s", method, str(payload)[:300])
        return None
    for attempt in range(3):
        try:
            r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                              json=payload, timeout=40)
            js = r.json()
            if js.get("ok"):
                return js.get("result")
            if js.get("error_code") == 429:  # rate limit
                time.sleep(js.get("parameters", {}).get("retry_after", 3))
                continue
            log.warning("텔레그램 오류 %s: %s", method, js)
            return None
        except requests.RequestException as e:
            log.warning("텔레그램 통신 오류: %s", e)
            time.sleep(2)
    return None


def send(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return _call("sendMessage", **payload)


def kind_url(acptno):
    return f"https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={acptno}"


def dart_url(rcept_no):
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_message(f: dict, analysts: list[str], chats: dict) -> tuple[str, list]:
    e = html.escape
    tag = "🔁 정정 " if f.get("is_correction") else ""
    q = market.quote(f.get("stock_code"), f.get("corp_code"))
    lines = [f"🔔 <b>{tag}밸류업 공시</b> | <b>{e(f['corp_name'])}</b> "
             f"({e(f.get('stock_code') or '-')}·{e(f.get('market') or '-')}) · <b>[{e(f.get('kind_type') or '계획')}]</b>",
             f"💰 시총 {market.fmt_cap(q['mktcap'])} · {q['basis_label']} {market.fmt_won(q['close'])}"
             f"{' (' + market.fmt_dt(q['close_dt']) + ')' if q['close_dt'] else ''}",
             f"📄 {e(f.get('report_nm') or '')}",
             f"🕒 {e(f.get('disclosed_at') or f.get('rcept_dt') or '')}"]
    if analysts:
        who = []
        for a in analysts:
            u = (chats.get(a) or {}).get("telegram_username")
            who.append(f"{e(a)}" + (f" (@{e(u)})" if u else ""))
        lines.append(f"👤 담당: <b>{', '.join(who)}</b>  ✍️ 작성 대상")
    else:
        lines.append("⚪ 커버리지 외 종목")
    atts = f.get("attachments") or []
    exts = ", ".join(sorted({a["ext"].upper() for a in atts if a["ext"] in config.VALID_EXTS}))
    lines.append(f"📎 첨부: {exts} {len(atts)}건 ✅")
    if f.get("plan_name"):
        lines.append(f"\n▪ <b>계획서</b>: {e(f['plan_name'])}")
    meta = []
    if f.get("high_dividend"):
        meta.append(f"고배당기업 {f['high_dividend']}")
    if f.get("pbr_plan"):
        meta.append(f"PBR 개선계획 {f['pbr_plan']}")
    if f.get("decision_date"):
        meta.append(f"이사회 {f['decision_date']}")
    if meta:
        lines.append("▪ " + e(" / ".join(meta)))
    if f.get("ai_summary"):
        lines.append(f"\n🤖 <b>AI 요약</b>\n{e(_clip(f['ai_summary'], 1800))}")
    elif f.get("main_content"):
        lines.append(f"\n▪ <b>주요 내용</b>\n{e(_clip(f['main_content'], 900))}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"

    row1 = [{"text": "KIND 원문", "url": kind_url(f["acptno"])}]
    if f.get("rcept_no"):
        row1.append({"text": "DART", "url": dart_url(f["rcept_no"])})
    buttons = [row1]
    pdf = next((a for a in atts if a["ext"] == "pdf"), None) or (atts[0] if atts else None)
    row2 = []
    if pdf:
        row2.append({"text": f"📎 {pdf['ext'].upper()} 열기", "url": pdf["url"]})
    if config.DASHBOARD_URL:
        row2.append({"text": "대시보드", "url": f"{config.DASHBOARD_URL.rstrip('/')}/filing/{f['acptno']}"})
    if row2:
        buttons.append(row2)
    return text, buttons


def notify(f: dict):
    analysts = db.analysts_for(f.get("stock_code") or "")
    if config.ALERT_SCOPE == "coverage" and not analysts:
        return False
    chats = db.analyst_chats(analysts)
    text, buttons = build_message(f, analysts, chats)
    ok = False
    if config.TELEGRAM_CHAT_ID:
        ok = send(config.TELEGRAM_CHAT_ID, text, buttons) is not None or not config.TELEGRAM_BOT_TOKEN
    if config.SEND_DM_TO_ANALYST:
        for a in analysts:
            cid = (chats.get(a) or {}).get("telegram_chat_id")
            if cid:
                send(cid, "📌 <b>내 커버리지 종목</b>\n" + text, buttons)
                ok = True
    return ok


# ----------------------------------------------------------------------
# 봇 명령어 처리 (long polling)
# ----------------------------------------------------------------------
HELP = ("밸류업 공시 알림봇 (영업일 09:00·15:00 정기 리포트)\n"
        "/register 이름 — 내 텔레그램을 애널리스트 이름과 연결 (커버리지 엑셀의 이름과 동일하게)\n"
        "/mine — 내 커버리지 종목의 최근 30일 밸류업 공시\n"
        "/chatid — 현재 대화방 chat_id 확인 (단톡방 설정용)")


def send_document(chat_id, data: bytes, filename: str, caption: str = ""):
    if not config.TELEGRAM_BOT_TOKEN:
        log.info("[텔레그램 미설정] 파일 %s (%d bytes)", filename, len(data))
        return None
    try:
        r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendDocument"),
                          data={"chat_id": chat_id, "caption": caption}, files={"document": (filename, data)},
                          timeout=60)
        return r.json().get("result")
    except requests.RequestException as e:
        log.warning("파일 전송 실패: %s", e)
        return None


def handle_callback(cq: dict):
    """알림의 '계획발간/이행발간' 버튼 → 발간 체크 토글"""
    data = cq.get("data") or ""
    frm = cq.get("from", {})
    who = frm.get("first_name") or frm.get("username") or "-"
    # 텔레그램 사용자와 연결된 애널리스트 이름이 있으면 그 이름으로 기록
    me = next((a["analyst"] for a in db.coverage_summary() if a["telegram_chat_id"] == str(frm.get("id"))), who)
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "pub" and parts[2] in ("plan", "impl"):
        f = db.get_filing(parts[1])
        if f:
            field = "pub_" + parts[2]
            f = db.set_pub(parts[1], field, not f[field], me)
            _call("answerCallbackQuery", callback_query_id=cq["id"],
                  text=f"{f['corp_name']}: {f['pub_status']} (대시보드는 다음 업데이트 때 반영)")
            return
    _call("answerCallbackQuery", callback_query_id=cq["id"], text="처리할 수 없는 버튼입니다")


def process_updates_once():
    """쌓여 있는 텔레그램 업데이트(버튼·명령)를 한 번에 처리 (GitHub Actions 실행 시)"""
    if not config.TELEGRAM_BOT_TOKEN:
        return 0
    offset = int(db.get_meta("tg_offset", "0"))
    res = _call("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"]) or []
    for u in res:
        offset = u["update_id"] + 1
        try:
            handle_update(u)
        except Exception as e:  # noqa: BLE001
            log.exception("업데이트 처리 오류: %s", e)
    db.set_meta("tg_offset", offset)
    return len(res)


def handle_update(u: dict):
    if u.get("callback_query"):
        return handle_callback(u["callback_query"])
    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd in ("/start", "/help"):
        send(chat["id"], HELP)
    elif cmd == "/chatid":
        send(chat["id"], f"chat_id: <code>{chat['id']}</code>")
    elif cmd == "/register":
        name = arg.strip()
        if not name:
            send(chat["id"], "사용법: /register 홍길동")
        elif chat.get("type") != "private":
            send(chat["id"], "개인 알림 연결은 봇과의 1:1 대화에서 해주세요.")
        elif db.link_telegram(name, chat["id"], frm.get("username")):
            send(chat["id"], f"✅ '{html.escape(name)}' 님과 연결됐습니다. 담당 종목 공시는 DM으로도 보내드려요.")
        else:
            names = ", ".join(db.analyst_names()) or "(등록된 애널리스트 없음)"
            send(chat["id"], f"'{html.escape(name)}' 이름이 커버리지 목록에 없습니다.\n등록된 이름: {html.escape(names)}")
    elif cmd == "/mine":
        me = next((a for a in db.coverage_summary() if a["telegram_chat_id"] == str(chat["id"])), None)
        if not me:
            send(chat["id"], "먼저 /register 이름 으로 연결해주세요.")
            return
        rows = db.list_filings(analyst=me["analyst"], days=30, limit=30)
        if not rows:
            send(chat["id"], "최근 30일 내 담당 종목 밸류업 공시가 없습니다.")
            return
        lines = [f"<b>{html.escape(me['analyst'])}</b> 님 최근 30일 밸류업 공시"]
        for r in rows:
            lines.append(f"• {r['disclosed_at'] or r['rcept_dt']} {html.escape(r['corp_name'])} — {r['status']}")
        send(chat["id"], "\n".join(lines))


def poll_commands(stop_event):
    if not config.TELEGRAM_BOT_TOKEN:
        return
    offset = int(db.get_meta("tg_offset", "0"))
    while not stop_event.is_set():
        res = _call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
        for u in res or []:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:  # noqa: BLE001
                log.exception("명령 처리 오류: %s", e)
        db.set_meta("tg_offset", offset)
        if res is None:
            stop_event.wait(5)

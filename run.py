"""밸류업 공시 알림 시스템 실행

  python run.py                 # 감시 + 텔레그램 + 대시보드 (기본)
  python run.py --once          # 한 번만 조회하고 종료 (스케줄러/테스트용)
  python run.py --backfill 60   # 과거 60일치 공시를 알림 없이 불러오기
  python run.py --test-telegram # 텔레그램 연결 테스트 메시지 전송
  python run.py --check 20260930000281   # 특정 공시(KIND 접수번호)의 첨부·본문·구분 확인
  python run.py --price 388050          # 종가·시총 조회 확인
  python run.py --preview               # 다음 정기 리포트 미리보기 (발송 안 함)
  python run.py --digest-now            # 정기 리포트 지금 발송
  python run.py --action                # GitHub Actions: 1회 수집 + (09:00/15:30 회차면) 알림 + 정적 대시보드
  python run.py --action --slot 15:30   # 해당 회차로 강제 발송 (수동 실행용)
"""
import argparse
import logging
import threading

import uvicorn

from app import config, coverage, db, digest, kind, market, telegram
from app.web import create_app
from app.worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--check", default="")
    ap.add_argument("--price", default="")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--digest-now", action="store_true")
    ap.add_argument("--action", action="store_true")
    ap.add_argument("--slot", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    db.init()
    if not config.HOLIDAY_FILE.exists():
        config.HOLIDAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.HOLIDAY_FILE.write_text(
            "# 공휴일·대체공휴일·선거일·12/31은 자동 반영됩니다.\n"
            "# 그 밖의 임시 휴장일만 한 줄에 하나씩 YYYY-MM-DD 로 적으세요. (예: 2027-03-03  # 임시공휴일)\n",
            encoding="utf-8")
    w = Worker()

    if args.test_telegram:
        r = telegram.send(config.TELEGRAM_CHAT_ID, "✅ 밸류업 공시 알림봇 연결 테스트 (영업일 "
                          + "·".join(config.DIGEST_TIMES) + " 정기 리포트)")
        print("성공" if r else "실패 — 토큰/chat_id를 확인하세요 (봇을 단톡방에 초대했는지도)")
        return
    if args.action:
        from datetime import datetime
        slot = f"{datetime.now():%Y-%m-%d} {args.slot}" if args.slot else None
        print(w.run_once_action(force_slot=slot))
        return
    if args.check:
        import json
        res = w.kind.inspect(args.check)
        b = res["body"]
        b.pop("body_text", None)
        res["구분"] = kind.classify(res["title"], b.get("plan_name", ""), b.get("main_content", ""),
                                  [a["name"] for a in res["attachments"]])
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    if args.price:
        w.refresh_corp_map()
        hit = next((v for v in w.corp_map.values() if v["stock_code"] == args.price), {})
        q = market.quote(args.price, hit.get("corp_code"))
        print(hit.get("corp_name", args.price), q["basis_label"], market.fmt_won(q["close"]), q["close_dt"],
              "| 시총", market.fmt_cap(q["mktcap"]), "| 주식수 기준", q["shares_basis"])
        return
    if args.preview or args.digest_now:
        w.refresh_corp_map()
        w.sync_coverage()
        if args.preview:
            for m in digest.build(db.digest_candidates()):
                print(m, "\n" + "-" * 40)
        else:
            digest.send_digest(None)
            print("발송 완료")
        return
    if args.backfill:
        w.backfill(args.backfill)
        db.set_meta("initialized", db.now())
        return
    if args.once:
        if db.get_meta("initialized") is None:
            w.backfill(config.BACKFILL_DAYS)
            db.set_meta("initialized", db.now())
        w.tick()
        return

    threading.Thread(target=w.run_forever, daemon=True, name="watcher").start()
    threading.Thread(target=telegram.poll_commands, args=(w.stop,), daemon=True, name="tg-bot").start()
    uvicorn.run(create_app(w), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""감지 → 첨부 검증 → (요약) → 알림 파이프라인"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta

from . import config, coverage, dart, db, digest, kind, market, summarize, telegram

log = logging.getLogger(__name__)
UNVERIFIED = "첨부 미확인 (KIND 접속 불가)"


class Worker:
    def __init__(self):
        self.kind = kind.KindClient()
        self.corp_map: dict = {}
        self.corp_map_date = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_dart = datetime.min
        self.status = {"last_kind": None, "last_dart": None, "last_error": None}
        self.kind_ok = True   # 이번 실행에서 KIND 접속 가능 여부 (해외 서버에서 막히는 경우 대비)

    # ------------------------------------------------------------------
    def refresh_corp_map(self, force=False):
        today = datetime.now().date()
        if not config.DART_API_KEY or (self.corp_map_date == today and not force):
            return
        try:
            self.corp_map = dart.fetch_corp_map()
            self.corp_map_date = today
            log.info("상장사 코드표 갱신: %d개", len(self.corp_map))
        except Exception as e:  # noqa: BLE001
            log.warning("corpCode 갱신 실패: %s", e)

    def lookup(self, corp_name):
        return self.corp_map.get(dart.norm_name(corp_name)) or {}

    # ------------------------------------------------------------------
    def register(self, item: dict, silent=False) -> bool:
        """새 공시 등록. item 키: acptno, corp_name, report_nm, (market, disclosed_at, rcept_no, stock_code, corp_code, rcept_dt)"""
        if not dart.is_valueup(item.get("report_nm", "")):
            return False
        acptno = item["acptno"]
        if db.filing_exists(acptno):
            # DART 쪽에서 더 정확한 코드가 오면 보강
            cur = db.get_filing(acptno)
            upd = {k: item[k] for k in ("stock_code", "corp_code", "rcept_no", "disclosed_at", "market")
                   if item.get(k) and not cur.get(k)}
            db.update_filing(acptno, **upd)
            return False
        hit = self.lookup(item["corp_name"])
        row = {
            "acptno": acptno,
            "rcept_no": item.get("rcept_no") or dart.to_dart_rcept_no(acptno),
            "corp_code": item.get("corp_code") or hit.get("corp_code"),
            "corp_name": item["corp_name"],
            "stock_code": item.get("stock_code") or hit.get("stock_code"),
            "market": item.get("market"),
            "report_nm": re.sub(r"\s{2,}", " ", item["report_nm"]).strip(),
            "rcept_dt": item.get("rcept_dt") or acptno[:8],
            "disclosed_at": item.get("disclosed_at"),
            "source": item.get("source"),
            "detected_at": db.now(),
            "is_correction": int(dart.is_correction(item["report_nm"])),
            "alerted_at": "silent(backfill)" if silent else None,
            "digest_at": "silent(backfill)" if silent else None,
        }
        db.insert_filing(row)
        log.info("신규 감지: %s %s (%s)", row["corp_name"], row["report_nm"], acptno)
        self.check(acptno, silent=silent)
        return True

    def check(self, acptno, silent=False):
        f = db.get_filing(acptno)
        if not f:
            return
        try:
            res = self.kind.inspect(acptno)
        except Exception as e:  # noqa: BLE001
            if not self.kind_ok and not f["valid"]:
                # KIND 자체가 막힌 환경: DART로 감지한 공시를 '첨부 미확인'으로 넘겨 알림은 가게 함
                db.update_filing(acptno, valid=1, last_error=UNVERIFIED, last_checked=db.now(),
                                 kind_type=kind.classify(f["report_nm"]), check_count=f["check_count"] + 1)
                log.warning("KIND 접속 불가 → 첨부 미확인으로 처리: %s", f["corp_name"])
                return
            db.update_filing(acptno, last_error=str(e)[:500], last_checked=db.now(),
                             check_count=f["check_count"] + 1)
            log.warning("KIND 확인 실패 %s: %s", acptno, e)
            return
        body = res.get("body", {})
        upd = {
            "attachments": res["attachments"],
            "valid": int(res["valid"]),
            "plan_name": body.get("plan_name"),
            "main_content": body.get("main_content"),
            "high_dividend": body.get("high_dividend"),
            "pbr_plan": body.get("pbr_plan"),
            "decision_date": body.get("decision_date"),
            "related": body.get("related") or [],
            "kind_type": kind.classify(f["report_nm"], body.get("plan_name", ""), body.get("main_content", ""),
                                       [a["name"] for a in res["attachments"]] + [t for _, t in res.get("attached_docs", [])]),
            "body_text": body.get("body_text"),
            "last_checked": db.now(),
            "check_count": f["check_count"] + 1,
            "last_error": None,
        }
        db.update_filing(acptno, **upd)
        if not res["valid"]:
            log.info("첨부(PDF/HWP/DOC) 없음 → 미인정, 재확인 대기: %s %s", f["corp_name"], acptno)
            return
        f = db.get_filing(acptno)
        self._download(f)
        if not silent and summarize.enabled() and not f.get("ai_summary"):
            f = db.get_filing(acptno)
            pdf = next((a for a in f["attachments"] if a["ext"] == "pdf" and a.get("local")), None)
            if pdf:
                s = summarize.summarize_pdf(config.FILE_DIR / pdf["local"], f.get("body_text") or "")
                if s:
                    db.update_filing(acptno, ai_summary=s)
        f = db.get_filing(acptno)
        if config.INSTANT_ALERT and not f.get("alerted_at"):
            if telegram.notify(f):
                db.update_filing(acptno, alerted_at=db.now())
            else:
                db.update_filing(acptno, alerted_at="skipped")

    def _download(self, f):
        if not config.DOWNLOAD_FILES:
            return
        atts = f["attachments"]
        changed = False
        for i, a in enumerate(atts):
            if a["ext"] not in config.VALID_EXTS or a.get("local"):
                continue
            d = config.FILE_DIR / f["acptno"]
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[\\/:*?"<>|]', "_", a["name"])[:120] or f"file{i}"
            if not safe.lower().endswith("." + a["ext"]):
                safe += "." + a["ext"]
            try:
                self.kind.download(a["url"], d / safe)
                a["local"] = f"{f['acptno']}/{safe}"
                changed = True
            except Exception as e:  # noqa: BLE001
                log.warning("첨부 다운로드 실패 %s: %s", a["url"], e)
        if changed:
            db.update_filing(f["acptno"], attachments=json.dumps(atts, ensure_ascii=False))

    # ------------------------------------------------------------------
    def poll_kind(self, days=1, silent=False):
        to = datetime.now()
        fr = to - timedelta(days=days)
        rows = self.kind.fetch_valueup_list(fr.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d"))
        n = 0
        for r in reversed(rows):  # 오래된 것부터
            n += self.register({**r, "report_nm": r["title"], "source": "kind"}, silent=silent)
        self.status["last_kind"] = db.now()
        db.set_meta("last_update", db.now())
        return n

    def poll_dart(self, days=0, silent=False):
        if not config.DART_API_KEY:
            return 0
        to = datetime.now()
        fr = to - timedelta(days=days)
        items = dart.fetch_list(fr.strftime("%Y%m%d"), to.strftime("%Y%m%d"))
        n = 0
        for it in reversed(items):
            n += self.register({
                "acptno": dart.to_kind_acptno(it["rcept_no"]),
                "rcept_no": it["rcept_no"], "corp_code": it.get("corp_code"),
                "corp_name": it["corp_name"], "stock_code": it.get("stock_code"),
                "market": it.get("market"), "report_nm": it["report_nm"],
                "rcept_dt": it.get("rcept_dt"), "source": "dart",
            }, silent=silent)
        self.status["last_dart"] = db.now()
        return n

    def recheck_pending(self):
        for f in db.pending_checks(config.RECHECK_HOURS):
            # 처음엔 자주, 이후엔 드물게 (점검 횟수에 따라 1분→30분)
            gap = min(30, 1 + f["check_count"] * 2)
            if f["last_checked"] and datetime.strptime(f["last_checked"], "%Y-%m-%d %H:%M:%S") > \
                    datetime.now() - timedelta(minutes=gap):
                continue
            self.check(f["acptno"])

    # ------------------------------------------------------------------
    def backfill(self, days):
        log.info("과거 %d일 공시 불러오기(알림 없음)…", days)
        with self.lock:
            self.refresh_corp_map()
            n = 0
            try:
                n += self.poll_kind(days=days, silent=True)
            except Exception as e:  # noqa: BLE001
                log.warning("KIND 백필 실패: %s", e)
            try:
                n += self.poll_dart(days=min(days, 89), silent=True)
            except Exception as e:  # noqa: BLE001
                log.warning("DART 백필 실패: %s", e)
        log.info("백필 완료: %d건", n)
        return n

    def sync_coverage(self, force=False):
        st = coverage.sync(self.corp_map, force=force)
        self.status["coverage"] = st
        return st

    def warm_quotes(self, days=30, limit=150):
        """대시보드가 바로 뜨도록 최근 공시 종목의 종가·주식수를 미리 받아 둔다"""
        basis = market.price_basis_date()
        if self.status.get("quotes_basis") == str(basis):
            return
        for f in db.list_filings(days=days, limit=limit):
            if f.get("stock_code"):
                market.quote(f["stock_code"], f.get("corp_code"))
        if datetime.now().time() >= market.time(16, 0) or basis != datetime.now().date():
            self.status["quotes_basis"] = str(basis)

    def compute_caps(self, now=None):
        """발표일 시총(공시일 종가 × 상장주식수) 계산·확정"""
        ok = fail = 0
        market.LAST_ERROR.clear()
        for f in db.caps_pending(limit=3000):
            basis = market.announce_basis(f.get("disclosed_at"), f["rcept_dt"])
            q = market.cap_on(f["stock_code"], f.get("corp_code"), basis)
            if q["close"]:
                db.update_filing(f["acptno"], ann_close=q["close"], ann_cap=q["mktcap"], ann_dt=q["close_dt"],
                                 ann_final=int(bool(q["mktcap"]) and market.is_final(basis, now)))
            if q["mktcap"]:
                ok += 1
            else:
                fail += 1
        err = market.LAST_ERROR.get("price") or ("" if not fail else "상장주식수 조회 실패")
        db.set_meta("cap_error", f"시총 미계산 {fail}건 · {err}" if fail else "")
        log.info("발표일 시총 계산: 성공 %d / 실패 %d %s (KIS 키 %s)", ok, fail, err,
                 "있음" if market.kis_enabled() else "없음 → 네이버·DART 사용")
        return ok, fail

    def maybe_digest(self, now=None):
        slot = digest.due_slot(now)
        if slot:
            self.sync_coverage()
            self.compute_caps(now)
            digest.send_digest(slot, now)
            self.status["last_digest"] = slot

    def tick(self):
        with self.lock:
            self.refresh_corp_map()
            self.sync_coverage()
            try:
                self.poll_kind(days=1)
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = f"{db.now()} KIND: {e}"
                log.warning("KIND 조회 실패: %s", e)
            if datetime.now() - self.last_dart >= timedelta(seconds=config.DART_INTERVAL_SEC):
                try:
                    self.poll_dart(days=0)
                    self.last_dart = datetime.now()
                except Exception as e:  # noqa: BLE001
                    self.status["last_error"] = f"{db.now()} DART: {e}"
                    log.warning("DART 조회 실패: %s", e)
            self.recheck_pending()
            try:
                self.compute_caps()
            except Exception as e:  # noqa: BLE001
                log.warning("시총 계산 실패: %s", e)
            try:
                self.maybe_digest()
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = f"{db.now()} 리포트: {e}"
                log.exception("정기 리포트 실패: %s", e)

    def run_forever(self):
        if db.get_meta("initialized") is None:
            self.backfill(config.BACKFILL_DAYS)
            db.set_meta("initialized", db.now())
        last_poll = datetime.min
        while not self.stop.is_set():
            h = datetime.now().hour
            interval = config.NIGHT_INTERVAL_SEC if (h >= 22 or h < 6) else config.POLL_INTERVAL_SEC
            # 리포트 직전에는 최신 공시를 반영하도록 조회를 당겨서 실행
            if datetime.now() - last_poll >= timedelta(seconds=interval) or digest.due_slot():
                self.tick()
                last_poll = datetime.now()
            self.stop.wait(30)


    # ------------------------------------------------------------------
    def run_once_action(self, now=None, force_slot=None, backfill_days=0) -> dict:
        """GitHub Actions용 1회 실행: 수집 → 판별 → 시총 → (정기 알림) → 정적 대시보드"""
        from . import site
        now = now or datetime.now()
        if db.get_meta("initialized") is None or backfill_days:
            # 과거 공시는 알림 없이 적재 (이미 있는 공시는 건너뜀)
            self.backfill(backfill_days or config.BACKFILL_DAYS)
            db.set_meta("initialized", db.now())
        from . import pubsync
        self.refresh_corp_map()
        self.sync_coverage()
        pubsync.pull()                             # 사이트에서 한 발간 체크 반영
        telegram.process_updates_once()            # 텔레그램 발간 체크 버튼/명령 반영
        try:
            self.poll_kind(days=4)
            self.kind_ok = True
        except Exception as e:  # noqa: BLE001
            self.kind_ok = False
            log.warning("KIND 접속 실패 → DART로만 감지: %s", e)
        try:
            self.poll_dart(days=3)
        except Exception as e:  # noqa: BLE001
            log.warning("DART 조회 실패: %s", e)
        db.set_meta("last_update", db.now())
        self.recheck_pending()
        self.compute_caps(now)
        slot = force_slot or digest.due_slot(now)
        sent = []
        if slot:
            sent = digest.send_digest(slot, now)
            if slot.endswith("15:30") and config.TELEGRAM_CHAT_ID:
                telegram.send_document(config.TELEGRAM_CHAT_ID, site.status_xlsx(),
                                       f"밸류업_현황_{now:%Y%m%d}.xlsx", "📎 밸류업 현황 (최근 1년)")
        pubsync.push()
        site.build()
        return {"slot": slot, "messages": len(sent), "kind_ok": self.kind_ok}

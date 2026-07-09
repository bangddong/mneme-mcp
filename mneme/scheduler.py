"""APScheduler — Outer Loop 주기 트리거 (하이브리드).

INTERVAL_MIN 마다 깨어나 outer_loop.run_cycle()을 호출한다. 실제 정산 여부는
run_cycle 내부의 N-에피소드 게이트가 결정한다(force=False). 즉 시간(스케줄러)과
이벤트(누적 에피소드) 둘 다 만족해야 정산이 돈다.
"""
import os
import traceback
from apscheduler.schedulers.background import BackgroundScheduler

from mneme import outer_loop
from mneme import self_model

INTERVAL_MIN = int(os.getenv("OUTER_LOOP_INTERVAL_MIN", "10"))

_scheduler: BackgroundScheduler | None = None


def _tick():
    try:
        result = outer_loop.run_cycle(force=False)   # L3 생애주기 정산
        if result.get("ran"):
            print(f"[outer_loop] cycle ran: {result}")
    except Exception:
        # 스케줄러 잡 예외는 서버를 죽이지 않는다 — 로깅만.
        print("[outer_loop] cycle failed:\n" + traceback.format_exc())

    try:
        res = self_model.assess(force=False)          # L5 자기평가 (메타 레이어)
        if res.get("ran"):
            print(f"[self_model] assessed: regulation={res['regulation']} "
                  f"difficulty={res['difficulty']}")
    except Exception:
        print("[self_model] assess failed:\n" + traceback.format_exc())


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(_tick, "interval", minutes=INTERVAL_MIN, id="outer_loop")
    _scheduler.start()
    print(f"[outer_loop] scheduler started (every {INTERVAL_MIN} min)")


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

"""자기 인식 4장치 (MNEME 요소 5 / L5 Identity).

Outer Loop 위의 메타 레이어. 스케줄러 틱에서 run_cycle 뒤에 assess()가 돌며,
자기 모델을 갱신하고 비정상 성장을 감지한다.

  M14 self_model       — 자기 지표 시계열(보정오차·성장속도·난이도). 기반 데이터.
  M15 Phoenix Assessor — 수행자와 분리된 독립 재평가(llm.assess_episode) → 보정오차.
  M16 GrowthRate Reg.  — 성장속도에서 급락/정체/과속 감지 → 로그+status 경보.
  M17 내재적 동기      — 성장 타깃 추천 + 난이도 스칼라 (축소 해석).

M15만 로컬 LLM을 쓰고, 미가용 시 보정오차=None으로 두고 나머지는 결정론으로 진행한다.
"""
import json
from mneme.memory import get_connection
from mneme import llm
from mneme.cib import clip
from mneme import outer_loop as ol
from mneme import growth

# ── 게이트 ──────────────────────────────────────────────
ASSESS_EPISODES = 20

# ── M16 조절 임계 ───────────────────────────────────────
CRASH_DROP = -0.25       # growth_rate 이하 → 급락
OVERSPEED = 0.4          # growth_rate 이상 → 과속
STAGNANT_EPS = 0.02      # |growth_rate| 미만이면 정체로 간주
STAGNANT_CYCLES = 3      # 연속 정체 사이클 수

# ── M17 난이도 ──────────────────────────────────────────
CAL_GOOD = 0.2           # 보정오차 이하면 좋은 보정
CAL_BAD = 0.4            # 보정오차 초과면 나쁜 보정
DIFF_STEP = 0.1
DIFF_START = 0.5


def _last_row(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM self_model ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _recent_rows(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM self_model ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def _episode_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def _max_episode_id(conn) -> int:
    return conn.execute("SELECT MAX(id) FROM episodes").fetchone()[0] or 0


def _window_episodes(conn, since_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM episodes WHERE id > ?", (since_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _calibration_error(window: list[dict]) -> float | None:
    """M15: 윈도우 내 self-score가 있는 에피소드를 독립 재평가해 평균 괴리."""
    if not llm.is_available():
        return None
    scored = [
        e for e in window
        if e.get("tool") == "episode_reflect" and e.get("score") is not None
    ]
    if not scored:
        return None
    diffs = []
    for e in scored:
        assessor = llm.assess_episode(e.get("query") or "", e.get("result_summary") or "")
        if assessor is None:
            continue
        diffs.append(abs(float(e["score"]) - assessor))
    if not diffs:
        return None
    return sum(diffs) / len(diffs)


def _regulation(growth_rate: float, recent_rows: list[dict], episodes_in_window: int) -> str:
    """M16: 성장속도 → healthy/crash/stagnant/overspeed."""
    if growth_rate <= CRASH_DROP:
        return "crash"
    if growth_rate >= OVERSPEED:
        return "overspeed"
    # 정체: 직전 (STAGNANT_CYCLES-1)개 행 + 이번 growth_rate가 모두 평탄 & 에피소드는 진행 중
    if episodes_in_window > 0:
        recent_grs = [abs(growth_rate)] + [
            abs(r["growth_rate"]) for r in recent_rows if r.get("growth_rate") is not None
        ]
        if len(recent_grs) >= STAGNANT_CYCLES and all(
            g < STAGNANT_EPS for g in recent_grs[:STAGNANT_CYCLES]
        ):
            return "stagnant"
    return "healthy"


def _difficulty(prev_difficulty: float | None, regulation: str,
                calibration_error: float | None) -> float:
    """M17: 내재적 동기 난이도 스칼라 갱신."""
    base = DIFF_START if prev_difficulty is None else prev_difficulty
    if regulation == "healthy" and (calibration_error is None or calibration_error < CAL_GOOD):
        signal = 1
    elif regulation == "crash" or (calibration_error is not None and calibration_error > CAL_BAD):
        signal = -1
    else:
        signal = 0
    return clip(base + DIFF_STEP * signal)


def _curriculum(conn) -> list[dict]:
    """M17: 비아카이브 스킬을 성장 타깃으로 분류 (outer_loop 임계 재사용)."""
    rows = conn.execute(
        "SELECT * FROM skills WHERE state != 'archived'"
    ).fetchall()
    targets = []
    for r in rows:
        s = dict(r)
        use = s["use_count"]
        rate = (s["success_count"] / use) if use else 0.0
        prop = s["propensity"]
        state = s["state"]
        if state in ("seeding", "developing") and use < ol.MIN_USE_ACTIVE:
            targets.append({"skill": s["name"], "state": state,
                            "reason": "under-practiced", "action": "사용 축적 필요"})
        elif state == "developing" and use >= ol.MIN_USE_ACTIVE and (
                rate < ol.PROMOTE_RATE or prop < ol.PROMOTE_PROP):
            targets.append({"skill": s["name"], "state": state,
                            "reason": "quality-gap", "action": "성공률·성향 개선 필요"})
        elif state == "degrading":
            targets.append({"skill": s["name"], "state": state,
                            "reason": "at-risk", "action": "회복 또는 아카이브 임박"})
    return targets


def _build_actions(regulation: str, growth_rate: float, success_rate: float,
                   calibration_error: float | None, curriculum: list[dict]) -> list[dict]:
    """감지된 문제를 성장 조치 항목(desired)으로 변환. 정상 항목은 포함하지 않아 자동 해소된다."""
    desired = []

    # M16: 성장속도 비정상
    if regulation == "crash":
        desired.append({
            "kind": "regulation", "severity": "critical", "subject": "성장속도 급락",
            "detail": f"성공률이 급락했습니다 (growth_rate={growth_rate:.3f}, "
                      f"success_rate={success_rate:.3f}). 최근 실패 에피소드·관련 스킬을 점검하세요.",
            "dedup_key": "regulation:crash",
        })
    elif regulation == "stagnant":
        desired.append({
            "kind": "regulation", "severity": "warn", "subject": "성장 정체",
            "detail": f"성장이 정체되어 있습니다 (success_rate={success_rate:.3f}). "
                      f"난이도를 올리거나 새 스킬 시드를 고려하세요.",
            "dedup_key": "regulation:stagnant",
        })
    elif regulation == "overspeed":
        desired.append({
            "kind": "regulation", "severity": "warn", "subject": "성장 과속",
            "detail": f"성장속도가 과도합니다 (growth_rate={growth_rate:.3f}). "
                      f"Goodhart/과적합 위험 — 표본이 충분한지 확인하세요.",
            "dedup_key": "regulation:overspeed",
        })

    # M15: 보정오차(자기점수 ↔ 독립평가 괴리) 과대
    if calibration_error is not None and calibration_error > CAL_BAD:
        desired.append({
            "kind": "calibration", "severity": "warn", "subject": "자기평가 신뢰도 저하",
            "detail": f"자기점수와 독립평가의 괴리가 큽니다 (calibration_error="
                      f"{calibration_error:.3f} > {CAL_BAD}). episode_reflect 채점 기준을 점검하세요.",
            "dedup_key": "calibration",
        })

    # M17: 아카이브 임박 스킬
    for t in curriculum:
        if t.get("reason") == "at-risk":
            desired.append({
                "kind": "skill-at-risk", "severity": "warn", "subject": t["skill"],
                "detail": f"스킬 '{t['skill']}'(state={t['state']})가 강등 중입니다 — "
                          f"회복 또는 아카이브 임박. 사용 맥락·위키 문서를 보강하세요.",
                "dedup_key": f"skill:{t['skill']}",
            })

    return desired


def assess(force: bool = False) -> dict:
    """자기평가 한 사이클. Outer Loop 틱 뒤 메타 레이어로 호출된다."""
    conn = get_connection()
    try:
        last = _last_row(conn)
        total = _episode_count(conn)
        seen_before = last["episodes_seen"] if last else 0
        new_episodes = total - seen_before

        if not force and new_episodes < ASSESS_EPISODES:
            return {"ran": False, "reason": f"new episodes {new_episodes} < {ASSESS_EPISODES}",
                    "new_episodes": new_episodes}

        since_id = max(0, _max_episode_id(conn) - new_episodes)
        window = _window_episodes(conn, since_id)

        # M16 성장속도
        if window:
            success_rate = sum(1 for e in window if e["success"]) / len(window)
        else:
            success_rate = last["success_rate"] if last and last["success_rate"] is not None else 0.0
        prev_rate = last["success_rate"] if last and last["success_rate"] is not None else success_rate
        growth_rate = success_rate - prev_rate

        # M15 보정오차
        calibration_error = _calibration_error(window)

        # M16 조절
        recent_rows = _recent_rows(conn, STAGNANT_CYCLES - 1)
        regulation = _regulation(growth_rate, recent_rows, len(window))

        # M17 난이도·커리큘럼
        prev_diff = last["difficulty"] if last else None
        difficulty = _difficulty(prev_diff, regulation, calibration_error)
        curriculum = _curriculum(conn)

        with conn:
            conn.execute(
                """INSERT INTO self_model
                   (episodes_seen, success_rate, growth_rate, calibration_error,
                    difficulty, regulation, curriculum)
                   VALUES (?,?,?,?,?,?,?)""",
                (total, success_rate, growth_rate, calibration_error, difficulty,
                 regulation, json.dumps(curriculum, ensure_ascii=False)),
            )

        # 감지된 문제를 성장 조치 큐에 동기화 (정상화된 항목은 자동 해소)
        desired = _build_actions(regulation, growth_rate, success_rate,
                                 calibration_error, curriculum)
        growth.emit(desired)

        if regulation != "healthy":
            print(f"[self_model] ALERT regulation={regulation} "
                  f"growth_rate={growth_rate:.3f} success_rate={success_rate:.3f}")

        return {
            "ran": True,
            "episodes_seen": total,
            "success_rate": success_rate,
            "growth_rate": growth_rate,
            "calibration_error": calibration_error,
            "difficulty": difficulty,
            "regulation": regulation,
            "curriculum": curriculum,
        }
    finally:
        conn.close()


def status(limit: int = 5) -> dict:
    """최근 자기 지표 시계열 + 최신 조절 상태·난이도·경보 플래그."""
    conn = get_connection()
    try:
        rows = _recent_rows(conn, limit)
        for r in rows:
            try:
                r["curriculum"] = json.loads(r["curriculum"]) if r["curriculum"] else []
            except (json.JSONDecodeError, TypeError):
                r["curriculum"] = []
        latest = rows[0] if rows else None
        return {
            "recent": rows,
            "regulation": latest["regulation"] if latest else None,
            "difficulty": latest["difficulty"] if latest else None,
            "alert": bool(latest and latest["regulation"] != "healthy"),
        }
    finally:
        conn.close()


def curriculum_suggest() -> dict:
    """M17: 에이전트가 다음 작업 선택에 참고할 난이도 + 성장 타깃 (read-only, 즉석 산출)."""
    conn = get_connection()
    try:
        last = _last_row(conn)
        difficulty = last["difficulty"] if last else DIFF_START
        return {"difficulty": difficulty, "targets": _curriculum(conn)}
    finally:
        conn.close()

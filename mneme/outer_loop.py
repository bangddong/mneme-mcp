"""Outer Loop (MNEME 요소 4) — 스킬 생명주기 주기 정산.

Inner Loop(`skills.update_propensity`)가 매 에피소드 성향을 미세 조정한다면,
Outer Loop는 N 에피소드 주기로 깨어나 *누적 성과를 보고 스킬의 state를 전이*한다.
CIB가 L3 스킬의 방향을 지키듯, Outer Loop가 L3 스킬의 생애주기를 정산한다.

  seeding → developing → active ↔ degrading → archived

또 매 사이클의 무결성 지표를 `loop_cycles`에 남긴다:
  CI (Coherence Index)        — 스킬셋의 헌법 정렬도 (로컬 LLM 채점, 선택적)
  BC (Behavioral Consistency) — 성향 안정도 (결정론적)
"""
import json
from mneme.memory import get_connection
from mneme import llm
from mneme import constitution
from mneme import notify

# ── 트리거 ──────────────────────────────────────────────
CYCLE_EPISODES = 20      # 직전 정산 이후 이만큼 새 에피소드가 쌓여야 실행 (force=False)

# ── 상태 전이 임계값 ────────────────────────────────────
MIN_USE_DEVELOPING = 3   # seeding → developing
MIN_USE_ACTIVE = 10      # developing → active (사용 빈도)
PROMOTE_RATE = 0.7       # developing → active / degrading → active (성공률)
PROMOTE_PROP = 0.6       # developing → active (성향)
DEMOTE_RATE = 0.4        # active → degrading (최근 성공률)
DEMOTE_PROP = 0.35       # active → degrading (성향)
ARCHIVE_PROP = 0.2       # degrading → archived (성향)
STALE_CYCLES = 3         # degrading → archived (미사용 사이클 수)


def _episode_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def _last_cycle(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM loop_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _recent_rate(conn, skill_name: str, since_episode_id: int) -> float | None:
    """직전 사이클 이후 해당 스킬을 사용한 에피소드의 성공 비율.

    episodes.skills_used 는 콤마로 join된 스킬명 CSV. 최근 윈도우에서
    이 스킬이 등장한 에피소드만 골라 success 평균을 낸다. 표본 없으면 None.
    """
    rows = conn.execute(
        """SELECT success, skills_used FROM episodes
           WHERE id > ? AND skills_used IS NOT NULL AND skills_used != ''""",
        (since_episode_id,),
    ).fetchall()
    used = [r for r in rows if skill_name in (r["skills_used"] or "").split(",")]
    if not used:
        return None
    successes = sum(1 for r in used if r["success"])
    return successes / len(used)


def _max_episode_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) FROM episodes").fetchone()
    return row[0] or 0


def _decide_transition(skill: dict, recent_rate: float | None,
                       cycles_idle: int) -> tuple[str, str] | None:
    """스킬의 (새 state, 사유) 반환. 전이 없으면 None.

    seed_protected 스킬은 강등(degrading)·아카이브(archived) 모두 면제한다
    (헌법 "정체성 보존" — cib.check의 강등 차단과 동일한 원칙).
    """
    state = skill["state"]
    use = skill["use_count"]
    success = skill["success_count"]
    prop = skill["propensity"]
    protected = bool(skill["seed_protected"])
    success_rate = (success / use) if use else 0.0

    if state == "seeding":
        if use >= MIN_USE_DEVELOPING:
            return "developing", f"use_count {use} >= {MIN_USE_DEVELOPING}"
        return None

    if state == "developing":
        if use >= MIN_USE_ACTIVE and success_rate >= PROMOTE_RATE and prop >= PROMOTE_PROP:
            return "active", (
                f"use {use}>={MIN_USE_ACTIVE}, rate {success_rate:.2f}>={PROMOTE_RATE}, "
                f"prop {prop:.2f}>={PROMOTE_PROP}"
            )
        return None

    if state == "active":
        if protected:
            return None  # seed_protected 강등 면제
        if (recent_rate is not None and recent_rate < DEMOTE_RATE) or prop < DEMOTE_PROP:
            why = (f"recent_rate {recent_rate:.2f}<{DEMOTE_RATE}"
                   if recent_rate is not None and recent_rate < DEMOTE_RATE
                   else f"prop {prop:.2f}<{DEMOTE_PROP}")
            return "degrading", why
        return None

    if state == "degrading":
        if recent_rate is not None and recent_rate >= PROMOTE_RATE:
            return "active", f"recovered: recent_rate {recent_rate:.2f}>={PROMOTE_RATE}"
        if protected:
            return None  # seed_protected 아카이브 면제
        if prop < ARCHIVE_PROP:
            return "archived", f"prop {prop:.2f}<{ARCHIVE_PROP}"
        if cycles_idle >= STALE_CYCLES:
            return "archived", f"idle {cycles_idle} cycles >= {STALE_CYCLES}"
        return None

    return None


def _compute_ci(active_developing: list[dict]) -> float | None:
    """active/developing 스킬을 헌법 시나리오에 채점한 평균 [0,1].

    로컬 LLM 미가용 시 None (Outer Loop는 그래도 결정론 부분으로 진행).
    """
    if not active_developing:
        return None
    if not llm.is_available():
        return None
    values = constitution.get_absolute_values()
    scenarios = constitution.get_test_scenarios()
    scores = []
    for s in active_developing:
        score = llm.score_coherence(
            skill_desc=s.get("description") or s["name"],
            direction="maintain",
            values=values,
            scenarios=scenarios,
        )
        scores.append(score)
    return sum(scores) / len(scores) if scores else None


def _compute_bc(prev: dict | None, current_props: dict[str, float]) -> float:
    """직전 사이클 대비 성향 안정도 = 1 - 평균|Δpropensity|. 기준점 없으면 1.0."""
    if not prev or not prev.get("propensities"):
        return 1.0
    try:
        prev_props = json.loads(prev["propensities"])
    except (json.JSONDecodeError, TypeError):
        return 1.0
    shared = [k for k in current_props if k in prev_props]
    if not shared:
        return 1.0
    deltas = [abs(current_props[k] - prev_props[k]) for k in shared]
    return max(0.0, 1.0 - sum(deltas) / len(deltas))


def run_cycle(force: bool = False) -> dict:
    """Outer Loop 한 사이클 실행.

    force=False면 직전 정산 이후 새 에피소드가 CYCLE_EPISODES 미만일 때
    실행을 건너뛴다(하이브리드 트리거). force=True면 무조건 정산.
    """
    conn = get_connection()
    try:
        last = _last_cycle(conn)
        total_episodes = _episode_count(conn)
        processed_so_far = last["episodes_processed"] if last else 0
        new_episodes = total_episodes - processed_so_far

        if not force and new_episodes < CYCLE_EPISODES:
            return {
                "ran": False,
                "reason": f"new episodes {new_episodes} < {CYCLE_EPISODES}",
                "new_episodes": new_episodes,
            }

        # 직전 사이클 이후 윈도우의 시작 episode id (최근 성공률 산출용).
        # episodes.id 는 AUTOINCREMENT 연속이라 (현재 max - 신규 수) 가 윈도우 시작점.
        max_id = _max_episode_id(conn)
        since_id = max(0, max_id - new_episodes)

        rows = conn.execute(
            "SELECT * FROM skills WHERE state != 'archived'"
        ).fetchall()
        skills = [dict(r) for r in rows]

        transitions = []
        active_developing = []
        current_props: dict[str, float] = {}

        for skill in skills:
            current_props[skill["name"]] = skill["propensity"]
            recent_rate = _recent_rate(conn, skill["name"], since_id)
            # 미사용 사이클 수: 최근 윈도우에서 한 번도 안 쓰였으면 +1, 썼으면 0.
            # (간이 구현 — 정밀 추적은 추후 self_model에서.)
            cycles_idle = 0 if recent_rate is not None else 1
            verdict = _decide_transition(skill, recent_rate, cycles_idle)
            if verdict:
                new_state, reason = verdict
                conn.execute(
                    "UPDATE skills SET state=?, updated_at=datetime('now') WHERE name=?",
                    (new_state, skill["name"]),
                )
                transitions.append({
                    "name": skill["name"],
                    "from": skill["state"],
                    "to": new_state,
                    "reason": reason,
                })
            if skill["state"] in ("active", "developing"):
                active_developing.append(skill)

        ci = _compute_ci(active_developing)
        bc = _compute_bc(last, current_props)

        with conn:
            conn.execute(
                """INSERT INTO loop_cycles
                   (episodes_processed, ci, bc, transitions, propensities)
                   VALUES (?,?,?,?,?)""",
                (total_episodes, ci, bc,
                 json.dumps(transitions, ensure_ascii=False),
                 json.dumps(current_props)),
            )

        # 복습 알림 (Discord 웹훅) — 실패해도 정산 결과에는 영향 없음
        notify.notify_degrading(transitions)

        return {
            "ran": True,
            "episodes_processed": total_episodes,
            "new_episodes": new_episodes,
            "transitions": transitions,
            "ci": ci,
            "bc": bc,
        }
    finally:
        conn.close()


def status(limit: int = 5) -> dict:
    """최근 사이클 기록 + 현재 스킬 state 분포."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ran_at, episodes_processed, ci, bc, transitions FROM loop_cycles "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        cycles = []
        for r in rows:
            d = dict(r)
            try:
                d["transitions"] = json.loads(d["transitions"]) if d["transitions"] else []
            except (json.JSONDecodeError, TypeError):
                d["transitions"] = []
            cycles.append(d)

        dist_rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM skills GROUP BY state"
        ).fetchall()
        distribution = {r["state"]: r["n"] for r in dist_rows}

        return {"recent_cycles": cycles, "skill_states": distribution}
    finally:
        conn.close()

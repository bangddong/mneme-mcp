"""L3 Procedural Memory — 스킬 성향(θ_eff) 관리.

Inner Loop의 학습 단위. 각 스킬은 [0,1] 성향 점수를 가지며,
에피소드 결과에 따라 CIB 게이트를 통과할 때만 갱신된다.

  θ_eff = clip(base + δ)
  δ_{t+1} = δ_t + η · reward     (reward = +1 성공 / -1 실패)
"""
from datetime import datetime, timezone
from mneme.memory import get_connection
from mneme.cib import check, clip, DELTA_MAX

ETA = 0.1  # 학습률 η (한 번에 움직이는 보폭)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def seed_skill(name: str, description: str, wiki_path: str | None = None,
               seed_protected: bool = False) -> dict:
    """새 스킬을 시드(seeding 상태)로 등록. 이미 있으면 그대로 반환."""
    conn = get_connection()
    existing = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
    if existing:
        conn.close()
        return dict(existing)
    with conn:
        conn.execute(
            """INSERT INTO skills(name, description, wiki_path, seed_protected, updated_at)
               VALUES(?,?,?,?,?)""",
            (name, description, wiki_path, 1 if seed_protected else 0, _now()),
        )
    row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row)


def suggest(task: str, limit: int = 5) -> list[dict]:
    """작업 관련 스킬을 성향(propensity) 내림차순으로 반환.

    간단 매칭: task 토큰이 스킬 name/description에 포함되는 것을 우선,
    없으면 propensity 상위 스킬을 반환 (계획 단계 메모리 주입).
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM skills WHERE state != 'archived' ORDER BY propensity DESC"
    ).fetchall()
    conn.close()

    skills = [dict(r) for r in rows]
    tokens = [t.lower() for t in task.split() if len(t) > 1]

    def relevance(s: dict) -> int:
        text = f"{s['name']} {s.get('description') or ''}".lower()
        return sum(1 for t in tokens if t in text)

    skills.sort(key=lambda s: (relevance(s), s["propensity"]), reverse=True)
    return skills[:limit]


def update_propensity(name: str, reward: float) -> dict:
    """에피소드 결과로 스킬 성향을 갱신 (CIB 게이트 적용).

    reward: +1(성공) / -1(실패)
    반환: {"applied": bool, "propensity": float, "reason": str}
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
    if row is None:
        conn.close()
        return {"applied": False, "propensity": None, "reason": f"unknown skill: {name}"}

    skill = dict(row)
    candidate_delta = clip(skill["delta"] + ETA * reward, -DELTA_MAX, DELTA_MAX)

    verdict = check(skill, candidate_delta, reward)

    # use_count/success_count는 통과 여부와 무관하게 기록
    new_use = skill["use_count"] + 1
    new_success = skill["success_count"] + (1 if reward >= 0 else 0)

    if verdict["pass"]:
        new_delta = candidate_delta
        new_propensity = clip(skill["base"] + new_delta)
    else:
        # 폐기 — δ 미반영
        new_delta = skill["delta"]
        new_propensity = skill["propensity"]

    with conn:
        conn.execute(
            """UPDATE skills SET delta=?, propensity=?, use_count=?, success_count=?, updated_at=?
               WHERE name=?""",
            (new_delta, new_propensity, new_use, new_success, _now(), name),
        )
    conn.close()

    return {
        "applied": verdict["pass"],
        "propensity": new_propensity,
        "reason": verdict["reason"],
    }

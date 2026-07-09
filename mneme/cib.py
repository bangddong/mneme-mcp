"""CIB — Constitutional Boundary (헌법 게이트).

MNEME의 핵심 안전 장치. 후보 성향 업데이트(δ)가 헌법 공간 안에 머무를 때만
통과시킨다. 통과하지 못하면 업데이트는 폐기된다.

검증은 2단계:
  (a) 프록시 — δ 크기 ±DELTA_MAX 이내, propensity는 [0,1]로 clip.
  (b) 방향(coherence) — haiku로 테스트 시나리오 채점, 최저 점수 < threshold면 block.
      방향이 진짜 원칙이고, 프록시는 보조 안전 플로어다.
"""
from mneme import constitution
from mneme import llm

DELTA_MAX = 0.2       # 한 스텝 최대 이동폭 (프록시)
DELTA_FLOOR = 0.1     # 최소 여유 (널 컨버전스 방지, 설계상 참고값)


def clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def check(skill: dict, candidate_delta: float, reward: float) -> dict:
    """후보 δ 업데이트가 헌법을 통과하는지 검사.

    skill: skills 테이블 행 (name, description, seed_protected 등)
    candidate_delta: 적용하려는 새 δ 값
    reward: +1(성공) / -1(실패) — 성향을 올리는지 내리는지 방향 판단용

    반환: {"pass": bool, "reason": str}
    """
    # 1) seed_protected 스킬은 강등(δ 감소 = reward<0) 차단
    if skill.get("seed_protected") and reward < 0:
        return {"pass": False, "reason": "seed_protected skill cannot be demoted"}

    # 2) 프록시 — δ 크기 제한
    if abs(candidate_delta) > DELTA_MAX + 1e-9:
        return {"pass": False, "reason": f"delta {candidate_delta:.3f} exceeds ±{DELTA_MAX}"}

    # 3) 방향(coherence) — 헌법 시나리오 채점
    direction = "increase" if reward >= 0 else "decrease"
    threshold = constitution.get_coherence_threshold()
    score = llm.score_coherence(
        skill_desc=skill.get("description") or skill.get("name", ""),
        direction=direction,
        values=constitution.get_absolute_values(),
        scenarios=constitution.get_test_scenarios(),
    )
    if score < threshold:
        return {"pass": False, "reason": f"coherence {score:.2f} < {threshold}"}

    return {"pass": True, "reason": f"coherence {score:.2f} >= {threshold}"}

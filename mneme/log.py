"""접근/이력 로그 조회 (옵저버빌리티).

episodes 테이블 = "어떤 에이전트가 / 어떤 tool로 / 무엇을 묻고 / 뭐라 답했나"의 단일 기록처.
CLI(`python -m mneme.log`)와 MCP tool(`episode_log`)이 이 모듈을 공유한다.

토큰/지연은 아직 기록하지 않음(로컬 Ollama라 과금 없음). 추후 컬럼 추가 시 여기 select만 확장.
"""
import sys
import io

from mneme.memory import get_connection

_COLS = ("created_at", "agent", "tool", "query", "result_summary",
         "success", "score", "skills_used")


def fetch_episodes(limit: int = 20, agent: str | None = None,
                   tool: str | None = None) -> list[dict]:
    """최근 에피소드를 최신순으로 반환한다. agent/tool로 필터 가능."""
    conn = get_connection()
    where, params = [], []
    if agent:
        where.append("agent = ?")
        params.append(agent)
    if tool:
        where.append("tool = ?")
        params.append(tool)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM episodes {clause} "
        f"ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _fmt(text, width):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def main():
    import argparse

    # Windows 콘솔 cp949에서 한국어/중국어 깨짐 방지
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="mneme 접근/이력 로그 조회")
    p.add_argument("-n", "--limit", type=int, default=20, help="최근 N건 (기본 20)")
    p.add_argument("-a", "--agent", help="에이전트로 필터")
    p.add_argument("-t", "--tool", help="tool로 필터")
    args = p.parse_args()

    eps = fetch_episodes(limit=args.limit, agent=args.agent, tool=args.tool)
    if not eps:
        print("(에피소드 없음)")
        return

    for e in eps:
        ok = {1: "OK", 0: "FAIL"}.get(e["success"], "-")
        score = f"{e['score']:.2f}" if e["score"] is not None else "-"
        print(f"[{e['created_at']}] {e['agent']} · {e['tool']}  ({ok}, score={score})")
        print(f"   Q: {_fmt(e['query'], 100)}")
        print(f"   A: {_fmt(e['result_summary'], 100)}")
        if e["skills_used"]:
            print(f"   skills: {e['skills_used']}")
        print()

    print(f"— 총 {len(eps)}건 (최신순)")


if __name__ == "__main__":
    main()

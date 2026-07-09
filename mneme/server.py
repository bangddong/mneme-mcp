import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP
from mneme.memory import init_db, get_connection
from mneme.wiki import read_file, write_file, list_md_files
from mneme import wiki as wiki_mod
from mneme import index as idx
from mneme import llm
from mneme import skills as skill_layer
from mneme import lint as wiki_lint_mod
from mneme import outer_loop
from mneme import self_model as self_model_mod
from mneme import log as access_log
from mneme import growth
from mneme.watcher import start_watcher, stop_watcher
from mneme.scheduler import start_scheduler, stop_scheduler

def _build_auth():
    """MCP_AUTH_TOKEN이 설정되면 bearer 정적 토큰 인증을 켠다.

    비우면 인증 없음(로컬 전용, 기존과 동일). 외부 노출(Tailscale Funnel 등) 시 필수.
    쉼표로 여러 토큰 허용 — `이름:토큰` 형식이면 이름이 client_id로 기록돼
    누가 접속했는지 에피소드 로그에서 식별 가능.
    """
    raw = os.getenv("MCP_AUTH_TOKEN", "").strip()
    if not raw:
        return None
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    tokens: dict[str, dict] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, tok = item.partition(":")
        if sep:
            tokens[tok.strip()] = {"client_id": name.strip() or "unnamed"}
        else:
            tokens[item] = {"client_id": "owner"}
    return StaticTokenVerifier(tokens=tokens)


mcp = FastMCP("mneme", auth=_build_auth())


@mcp.tool()
def wiki_search(query: str, session_id: str, agent: str = "unknown") -> dict:
    """FTS5 + 로컬 LLM으로 wiki에서 관련 문서를 검색한다."""
    conn = get_connection()

    # 세션 캐시 확인
    cache_key = f"search:{query}"
    row = conn.execute(
        "SELECT value FROM working WHERE session_id=? AND key=? AND expires_at > datetime('now')",
        (session_id, cache_key),
    ).fetchone()
    if row:
        conn.close()
        import json
        return json.loads(row["value"])

    # 로컬 LLM으로 후보 path 선별
    summaries = idx.get_all_summaries()
    candidate_paths = llm.select_candidate_paths(query, summaries)
    summary_by_path = {s["path"]: s.get("summary") or "" for s in summaries}

    results = []
    if candidate_paths:
        for path in candidate_paths:
            fts_results = idx.search_fts(query, limit=3)
            path_results = [r for r in fts_results if r["path"] == path]
            if path_results:
                results.extend(path_results)
            else:
                # FTS 미히트지만 LLM이 관련 있다 판단 → 문서 요약으로 excerpt 대체
                results.append({"path": path, "excerpt": summary_by_path.get(path, "")})

    # 로컬 LLM으로 최종 요약
    summary = ""
    if results:
        excerpts = "\n".join(f"[{r['path']}] {r['excerpt']}" for r in results)
        try:
            summary = llm._call(
                "Summarize these search results in 2-3 sentences. Return plain text only. Answer in Korean.",
                f"Query: {query}\n\nResults:\n{excerpts}",
            )
        except Exception:
            summary = f"{len(results)} document(s) found."

    output = {"results": results, "summary": summary}

    # 캐시 저장 (30분)
    import json
    expires = conn.execute("SELECT datetime('now', '+30 minutes')").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO working(session_id, key, value, expires_at) VALUES(?,?,?,?)",
            (session_id, cache_key, json.dumps(output), expires),
        )

    # 이력 기록
    with conn:
        conn.execute(
            "INSERT INTO episodes(session_id, agent, tool, query, result_summary) VALUES(?,?,?,?,?)",
            (session_id, agent, "wiki_search", query, summary[:200]),
        )
    conn.close()
    return output


@mcp.tool()
def wiki_get(path: str) -> dict:
    """wiki/{path} 파일 전문을 반환한다."""
    data = read_file(path)
    if data is None:
        return {"error": f"not found: {path}"}

    conn = get_connection()
    row = conn.execute(
        "SELECT updated_by FROM wiki_index WHERE path=?", (path,)
    ).fetchone()
    conn.close()

    data["updated_by"] = row["updated_by"] if row else "unknown"
    return data


@mcp.tool()
def wiki_inject(path: str, content: str, source_agent: str, session_id: str) -> dict:
    """wiki에 파일을 생성하거나 기존 파일과 병합한다.

    content는 OKF(Open Knowledge Format) 페이지 형식을 따라야 한다:
    YAML frontmatter에 type(어휘: concept/source/lesson/roadmap/schema/doc)·
    title·description·timestamp(YYYY-MM-DD)·tags,
    본문에 ## Summary / ## Details / ## Sources / ## Related 섹션.
    링크는 위키 루트 절대경로(예: /tech/pages/java-spring/foo.md),
    미존재 페이지 링크는 같은 줄에 <!-- stub --> 마커.

    형식 위반은 쓰지 않고 {"action": "rejected", "errors": [...]}를 반환한다 —
    오류를 고쳐 재호출하면 된다. index.md/log.md는 예약 파일로 코드가 자동
    갱신하므로 직접 inject하지 않는다.
    """
    norm = path.replace("\\", "/").strip("/")
    basename = norm.rsplit("/", 1)[-1]
    parts = norm.split("/")

    # --- 게이트 1: 경로 규칙 (결정론) ---
    errors: list[str] = []
    if basename in ("index.md", "log.md", "CLAUDE.md"):
        errors.append(f"예약 파일은 직접 inject 불가: {basename} (index.md/log.md는 페이지 생성 시 자동 갱신됨)")
    elif not norm.endswith(".md"):
        errors.append(f".md 파일만 inject 가능: {norm}")
    else:
        cat_dir = wiki_mod.get_wiki_dir() / parts[0]
        if len(parts) < 2 or not cat_dir.is_dir():
            errors.append(f"존재하지 않는 카테고리: {parts[0]} (위키 루트 1단계 디렉토리여야 함)")
        elif len(parts) >= 2 and parts[1] == "pages" and len(parts) == 3:
            # pages/ 직속 금지 — 카테고리가 서브폴더 규약을 쓰고 있으면 강제
            subdirs = sorted(
                p.name for p in (cat_dir / "pages").iterdir() if p.is_dir()
            ) if (cat_dir / "pages").is_dir() else []
            if subdirs:
                errors.append(
                    f"pages/ 바로 아래 생성 불가 — 서브폴더에 배치: {', '.join(subdirs)}"
                )

    # --- 게이트 2: OKF 형식 (pages/** 만 엄격 검사) ---
    is_page = len(parts) >= 2 and parts[1] == "pages"
    if not errors and is_page:
        errors.extend(wiki_lint_mod.lint_page_text(content, label=norm))

    if errors:
        return {"action": "rejected", "path": norm, "errors": errors}

    existing_data = read_file(norm)

    if existing_data is None:
        write_file(norm, content)
        idx.index_file(norm, updated_by=source_agent)
        action = "created"
        fm = wiki_lint_mod._parse_frontmatter(content) or {}
        wiki_mod.update_reserved_files(
            norm, fm.get("title", basename), fm.get("description", ""),
            source_agent, action="Creation",
        )
    else:
        # frontmatter는 코드가 보존 — LLM에는 본문만 병합시킨다 (에러 로그 07-06 ②)
        old_fm, old_body = wiki_lint_mod.split_frontmatter(existing_data["content"])
        _, new_body = wiki_lint_mod.split_frontmatter(content)
        judgment = llm.judge_conflict(old_body, new_body, source_agent)
        action = judgment.get("action", "skip")

        if action == "merge":
            merged_body = judgment.get("merged", "").strip("\n")
            fm_block = old_fm or ""
            if fm_block:
                today = datetime.now().strftime("%Y-%m-%d")
                fm_block = re.sub(
                    r"^timestamp:.*$", f"timestamp: {today}", fm_block, flags=re.MULTILINE
                )
            merged = f"{fm_block}\n\n{merged_body}\n" if fm_block else f"{merged_body}\n"
            # 병합 결과 lint — 실패 시 원문 유지 (에러 로그 07-06 ①)
            merge_errors = wiki_lint_mod.lint_page_text(merged, label=norm) if is_page else []
            if merge_errors:
                action = "merge_rejected"
            else:
                write_file(norm, merged)
                idx.index_file(norm, updated_by=source_agent)
                wiki_mod.update_reserved_files(
                    norm, "", "", source_agent, action="Update",
                )
        elif action == "conflict":
            marker = (
                f"\n\n<!-- CONFLICT [{source_agent}] {datetime.now(tz=timezone.utc).isoformat()} -->\n"
                f"{content}\n"
                f"<!-- END CONFLICT -->\n"
            )
            write_file(norm, existing_data["content"] + marker)
            idx.index_file(norm, updated_by=source_agent)

    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO episodes(session_id, agent, tool, query, result_summary) VALUES(?,?,?,?,?)",
            (session_id, source_agent, "wiki_inject", norm, action),
        )
    conn.close()
    result = {"action": action, "path": norm}
    if action == "merge_rejected":
        result["errors"] = merge_errors
    return result


@mcp.tool()
def wiki_list(prefix: str = "") -> list:
    """wiki_index 목록을 반환한다. prefix로 디렉토리 필터 가능."""
    conn = get_connection()
    if prefix:
        rows = conn.execute(
            "SELECT path, summary, tags, updated_at, updated_by FROM wiki_index WHERE path LIKE ? ORDER BY path",
            (f"{prefix}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path, summary, tags, updated_at, updated_by FROM wiki_index ORDER BY path"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def wiki_lint(category: str = "") -> dict:
    """wiki L2 무결성 검사(결정론적 기계 lint)를 실행한다.

    형식(frontmatter/섹션)·깨진 링크(<!-- stub --> 제외)·고아 페이지·
    index.md 동기화·오래된 페이지를 검사한다. category="" 면 전체 카테고리.
    내용 모순 같은 의미 검사는 별도(AI 영역)이며 여기엔 포함되지 않는다.
    """
    return wiki_lint_mod.lint_wiki(category or None)


@mcp.tool()
def outer_loop_run(force: bool = False) -> dict:
    """Outer Loop(요소 4) 정산을 한 사이클 실행한다.

    누적 성과로 스킬 state를 전이(seeding→developing→active↔degrading→archived)하고
    CI(헌법 정렬도)·BC(성향 안정도)를 기록한다. force=False면 직전 정산 이후 새
    에피소드가 임계(20)에 못 미칠 때 건너뛴다. 수동 점검 시 force=True.
    """
    return outer_loop.run_cycle(force=force)


@mcp.tool()
def outer_loop_status() -> dict:
    """최근 Outer Loop 사이클 기록과 현재 스킬 state 분포를 반환한다."""
    return outer_loop.status()


@mcp.tool()
def self_model_status() -> dict:
    """자기 인식(L5) 상태를 반환한다.

    최근 자기 지표 시계열(성공률·성장속도·보정오차·난이도)과 최신 조절 상태
    (healthy/crash/stagnant/overspeed), 비정상 성장 경보 플래그를 담는다.
    """
    return self_model_mod.status()


@mcp.tool()
def curriculum_suggest() -> dict:
    """내재적 동기(M17): 현재 난이도 스칼라 + 성장 타깃(연습·품질·위험 스킬)을 반환한다.

    에이전트가 다음 작업의 야심 수준·집중 대상을 고를 때 참고한다.
    """
    return self_model_mod.curriculum_suggest()


@mcp.tool()
def mneme_status() -> dict:
    """서버 상태 및 통계를 반환한다."""
    conn = get_connection()

    wiki_count = len(list_md_files(content_only=True))
    index_count = conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0]
    today_episodes = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE date(created_at) = date('now')"
    ).fetchone()[0]
    last_indexed_row = conn.execute(
        "SELECT MAX(updated_at) FROM wiki_index"
    ).fetchone()
    last_indexed = last_indexed_row[0] if last_indexed_row else None
    active_sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM working WHERE expires_at > datetime('now')"
    ).fetchone()[0]

    conn.close()
    return {
        "wiki_count": wiki_count,
        "index_count": index_count,
        "today_episodes": today_episodes,
        "last_indexed": last_indexed,
        "active_sessions": active_sessions,
    }


@mcp.tool()
def skill_seed(name: str, description: str, wiki_path: str = "",
               seed_protected: bool = False) -> dict:
    """새 스킬을 L3 procedural memory에 시드로 등록한다.

    seed_protected=True 인 스킬(안전·거부 관련)은 CIB가 강등을 차단한다.
    """
    skill = skill_layer.seed_skill(
        name=name,
        description=description,
        wiki_path=wiki_path or None,
        seed_protected=seed_protected,
    )
    return {"name": skill["name"], "state": skill["state"], "propensity": skill["propensity"]}


@mcp.tool()
def skill_suggest(task: str, session_id: str) -> dict:
    """작업에 관련된 스킬을 성향 순으로 추천하고 과거 교훈을 함께 반환한다.

    Inner Loop 1~2단계(계획 지원) — 외부 에이전트가 작업 시작 시 호출.
    """
    suggested = skill_layer.suggest(task)

    # 관련 과거 반성(next_hint) 주입 — 최근 성공/실패 교훈
    conn = get_connection()
    hint_rows = conn.execute(
        """SELECT next_hint FROM episodes
           WHERE next_hint IS NOT NULL AND next_hint != ''
           ORDER BY created_at DESC LIMIT 3"""
    ).fetchall()
    conn.close()
    hints = [r["next_hint"] for r in hint_rows]

    return {
        "skills": [
            {"name": s["name"], "description": s["description"],
             "propensity": s["propensity"], "state": s["state"]}
            for s in suggested
        ],
        "past_hints": hints,
    }


@mcp.tool()
def episode_reflect(task: str, outcome: str, success: bool, score: float,
                    skills_used: list, session_id: str, agent: str = "unknown") -> dict:
    """작업 결과를 반성·학습한다 (Inner Loop 4~6단계).

    1) 로컬 LLM으로 반성 생성 (무엇이 통했나/실패했나/다음 힌트)
    2) episode 저장
    3) 사용한 스킬의 성향을 CIB 게이트로 갱신 (성공=+1 / 실패=-1)
    """
    reflection = llm.reflect_episode(task, outcome, success)
    reward = 1.0 if success else -1.0

    # 스킬 성향 갱신 (CIB 게이트)
    skill_updates = []
    for name in skills_used:
        result = skill_layer.update_propensity(name, reward)
        skill_updates.append({"name": name, **result})

    # episode 저장
    conn = get_connection()
    with conn:
        conn.execute(
            """INSERT INTO episodes(session_id, agent, tool, query, result_summary,
                                    success, score, what_worked, what_failed, next_hint, skills_used)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, agent, "episode_reflect", task, outcome[:200],
             1 if success else 0, score,
             reflection["what_worked"], reflection["what_failed"], reflection["next_hint"],
             ",".join(skills_used)),
        )
    conn.close()

    return {
        "reflection": reflection,
        "skill_updates": skill_updates,
    }


@mcp.tool()
def episode_log(limit: int = 20, agent: str = "", tool: str = "") -> dict:
    """접근/이력 로그를 조회한다 — 어떤 에이전트가 어떤 tool로 무엇을 묻고 뭐라 답했나.

    옵저버빌리티용. agent/tool로 필터, 최근 limit건을 최신순 반환.
    (토큰/지연은 아직 미기록 — 로컬 Ollama라 과금 없음.)
    """
    eps = access_log.fetch_episodes(
        limit=limit, agent=agent or None, tool=tool or None
    )
    return {"count": len(eps), "episodes": eps}


@mcp.tool()
def growth_log(status: str = "open") -> dict:
    """성장 조치 큐 — 자기평가가 감지한 "사람이 해소해야 할" 문제 목록.

    status="open"(기본): 미해소 항목만. "all": 해소된 것까지.
    정상화된 항목은 자기평가가 자동 해소하므로, open 목록 = 지금 손봐야 할 것.
    """
    actions = growth.open_actions() if status == "open" else growth.all_actions()
    return {"count": len(actions), "status": status, "actions": actions}


@mcp.tool()
def growth_resolve(action_id: int, note: str = "") -> dict:
    """성장 조치 항목을 해소(resolved) 처리한다. note에 어떻게 해소했는지 기록."""
    return growth.resolve(action_id, note)


def main():
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8080"))

    init_db()
    idx.reindex_all()
    start_watcher()
    start_scheduler()

    print(f"Mneme MCP server running at http://{host}:{port}/mcp")
    try:
        mcp.run(transport="streamable-http", host=host, port=port)
    finally:
        stop_scheduler()
        stop_watcher()


if __name__ == "__main__":
    main()

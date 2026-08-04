"""LLM 런타임이 없을 때 wiki_search가 FTS 결과로 폴백하는지 검증.

README는 "런타임이 꺼져 있어도 서버는 보수적 fallback으로 무중단 동작"이라고 하는데,
wiki_search만은 그렇지 않았다: LLM이 후보 path를 골라준 뒤에만 FTS를 조회하는 구조라
(server.py의 `if candidate_paths:` 게이트) LLM이 없으면 FTS 인덱스에 문서가 멀쩡히
있어도 빈 결과가 나갔다.

실측(2026-08-04, 위키 33개 인덱싱 상태): 'grafana'/'observability'/'EKS'가 FTS로는
전부 히트하는데 wiki_search는 {"results":[],"summary":""}를 반환했다.
"""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated(monkeypatch):
    """격리된 임시 WIKI_DIR/DB_PATH + LLM 전면 차단."""
    tmp = tempfile.mkdtemp(prefix="mneme-ftsfallback-")
    wiki = Path(tmp) / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(Path(tmp) / "state.db"))

    from mneme import memory, index, llm

    # LLM 런타임 부재를 재현한다. 실제 서버가 꺼져 있을 때와 같은 동작:
    #   - select_candidate_paths 는 예외를 삼키고 [] 를 반환
    #   - _call 은 예외를 던짐
    monkeypatch.setattr(llm, "select_candidate_paths", lambda q, s: [])
    monkeypatch.setattr(llm, "generate_summary", lambda p, c: f"summary of {p}")

    def no_llm(*a, **kw):
        raise RuntimeError("LLM runtime unavailable")

    monkeypatch.setattr(llm, "_call", no_llm)

    memory.init_db()

    (wiki / "ops").mkdir()
    (wiki / "ops" / "observability.md").write_text(
        "---\ntitle: obs\n---\n\nGrafana and Prometheus setup for the cluster.\n",
        encoding="utf-8",
    )
    (wiki / "ops" / "unrelated.md").write_text(
        "---\ntitle: misc\n---\n\nNotes about coffee brewing ratios.\n",
        encoding="utf-8",
    )
    index.reindex_all()
    return wiki


def test_fts_fallback_returns_results_when_llm_is_down(isolated):
    """LLM이 없어도 FTS가 찾은 문서를 반환해야 한다."""
    from mneme import server

    out = server.wiki_search(query="Grafana", session_id="t1", agent="test")

    paths = [r["path"] for r in out["results"]]
    assert paths, "LLM 부재 시 빈 결과가 아니라 FTS 결과가 나와야 한다"
    assert any("observability" in p for p in paths), paths
    assert not any("unrelated" in p for p in paths), "관련 없는 문서는 안 나와야 한다"


def test_fts_fallback_summary_is_honest_not_empty(isolated):
    """요약 LLM도 없으므로, 빈 문자열 대신 결과 수를 알려야 한다."""
    from mneme import server

    out = server.wiki_search(query="Grafana", session_id="t2", agent="test")
    assert out["summary"], "summary가 비어 있으면 호출자가 '검색 실패'로 오해한다"


def test_fts_fallback_no_match_returns_empty(isolated):
    """FTS도 못 찾으면 빈 결과가 맞다 — 폴백이 아무거나 반환하면 안 된다."""
    from mneme import server

    out = server.wiki_search(query="zzzznonexistent", session_id="t3", agent="test")
    assert out["results"] == []


def test_fts_fallback_survives_fts5_syntax_in_query(isolated):
    """FTS5 특수문자가 든 쿼리로 예외가 새어나가면 안 된다.

    MATCH 는 `"` 나 미완결 구문에 대해 sqlite3.OperationalError 를 던진다.
    사용자 질의는 임의 문자열이므로 폴백 경로가 이를 삼켜야 한다.
    """
    from mneme import server

    out = server.wiki_search(query='Grafana "unclosed', session_id="t4", agent="test")
    assert isinstance(out["results"], list)

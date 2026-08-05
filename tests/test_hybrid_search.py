"""FTS 우선 하이브리드 검색 + 요약 FTS 색인 검증.

배경 (2026-08-05 실측, 위키 33건):
  - FTS는 글자 그대로만 찾는다. 우리 위키는 한국어 문서에 기술용어를 영어로 쓰므로
    '그라파나'·'쿠버네티스'·'시크릿' 질의가 전부 0건이었다(문서엔 Grafana/Kubernetes/Secret).
  - LLM 선별은 그 간극을 1/3만 메웠고(쿠버네티스 ✅ / 그라파나 ❌ / 시크릿 ❌),
    호출당 5~7초를 썼다. 모델 7종을 재봤으나 F1 상한이 37%였다.
  - 기존 구조는 LLM이 1차 관문이고 FTS가 폴백이라 측정 결과와 정반대였다.

그래서: ① 한국어로 고정된 요약을 FTS에도 색인해 한국어 키워드 검색 경로를 만들고,
        ② FTS를 1차로 쓰되 결과가 부족할 때만 LLM 선별로 보강한다.
"""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated(monkeypatch):
    """격리된 임시 WIKI_DIR/DB_PATH. LLM은 테스트별로 주입한다."""
    tmp = tempfile.mkdtemp(prefix="mneme-hybrid-")
    wiki = Path(tmp) / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(Path(tmp) / "state.db"))

    from mneme import memory

    memory.init_db()
    return wiki


# ──────────────────────────────────────────────────────────────
# ① 요약을 FTS에 색인한다 — 한국어 검색 경로
# ──────────────────────────────────────────────────────────────


def test_summary_is_searchable_via_fts(isolated, monkeypatch):
    """원문에 없고 요약에만 있는 단어로도 검색돼야 한다.

    이것이 한국어 질의를 살리는 메커니즘이다: 원문은 'Grafana'라고 쓰지만
    한국어 요약은 '그라파나'라고 쓰므로, 요약을 색인해야 '그라파나'가 걸린다.
    """
    from mneme import index, llm

    monkeypatch.setattr(
        llm, "generate_summary", lambda p, c: "그라파나 대시보드로 지표를 시각화한다."
    )
    (isolated / "obs.md").write_text(
        "---\ntitle: obs\n---\n\nGrafana dashboards for metrics.\n", encoding="utf-8"
    )
    index.reindex_all()

    # 원문엔 '그라파나'가 없다 — 요약이 색인돼야만 잡힌다.
    hits = index.search_fts("그라파나")
    assert [h["path"] for h in hits] == ["obs.md"], hits


def test_original_content_still_searchable(isolated, monkeypatch):
    """요약을 덧붙이더라도 원문 검색이 깨지면 안 된다."""
    from mneme import index, llm

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "요약문")
    (isolated / "obs.md").write_text(
        "---\ntitle: obs\n---\n\nGrafana dashboards for metrics.\n", encoding="utf-8"
    )
    index.reindex_all()

    assert [h["path"] for h in index.search_fts("Grafana")] == ["obs.md"]


def test_reindex_replaces_stale_summary_in_fts(isolated, monkeypatch):
    """요약이 바뀌면 옛 요약 단어로는 더 이상 검색되면 안 된다."""
    from mneme import index, llm

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "첫번째요약어")
    (isolated / "d.md").write_text("---\nt: x\n---\n\nbody\n", encoding="utf-8")
    index.reindex_all()
    assert index.search_fts("첫번째요약어")

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "두번째요약어")
    index.index_file("d.md", force=True)

    assert not index.search_fts("첫번째요약어"), "옛 요약이 FTS에 남아 있다"
    assert index.search_fts("두번째요약어")


# ──────────────────────────────────────────────────────────────
# ② generate_summary는 출력 언어를 지정한다 — 비결정성 제거
# ──────────────────────────────────────────────────────────────


def test_generate_summary_pins_output_language(isolated, monkeypatch):
    """언어를 지정하지 않으면 모델마다 한/중/영이 제각각 나온다(실측 26/33).

    같은 파일의 reflect_episode에는 이미 'Write every field value in Korean.'이 있고,
    server.wiki_search의 최종 요약에도 'Answer in Korean.'이 있다. 여기만 빠져 있었다.
    """
    from mneme import llm

    seen = {}

    def capture(system, user, json_mode=False):
        seen["system"] = system
        return "요약"

    monkeypatch.setattr(llm, "_call", capture)
    llm.generate_summary("p.md", "본문")

    assert "Korean" in seen["system"], seen.get("system")


# ──────────────────────────────────────────────────────────────
# ③ FTS 우선 — 충분하면 LLM을 아예 부르지 않는다
# ──────────────────────────────────────────────────────────────


def _make_docs(wiki: Path, n: int, word: str) -> None:
    for i in range(n):
        (wiki / f"doc{i}.md").write_text(
            f"---\nt: d{i}\n---\n\n{word} appears here.\n", encoding="utf-8"
        )


def test_llm_not_called_when_fts_has_enough(isolated, monkeypatch):
    """FTS가 충분히 찾으면 LLM 선별(5~7초)을 건너뛴다."""
    from mneme import index, llm, server

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "요약")
    _make_docs(isolated, 4, "Prometheus")
    index.reindex_all()

    calls = []
    monkeypatch.setattr(
        llm, "select_candidate_paths", lambda q, s: calls.append(q) or []
    )
    monkeypatch.setattr(llm, "_call", lambda *a, **k: "종합 요약")

    out = server.wiki_search(query="Prometheus", session_id="s1", agent="test")

    assert len(out["results"]) >= 3
    assert calls == [], "FTS로 충분한데 LLM을 불렀다"


def test_llm_supplements_when_fts_is_thin(isolated, monkeypatch):
    """FTS 결과가 부족하면 LLM 선별로 보강한다 — 의미 검색 경로."""
    from mneme import index, llm, server

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "요약")
    (isolated / "only.md").write_text("---\nt: o\n---\n\nGrafana here.\n", encoding="utf-8")
    (isolated / "semantic.md").write_text("---\nt: s\n---\n\n무관한 본문\n", encoding="utf-8")
    index.reindex_all()

    monkeypatch.setattr(llm, "select_candidate_paths", lambda q, s: ["semantic.md"])
    monkeypatch.setattr(llm, "_call", lambda *a, **k: "종합 요약")

    out = server.wiki_search(query="Grafana", session_id="s2", agent="test")
    paths = [r["path"] for r in out["results"]]

    assert "only.md" in paths, "FTS 결과가 빠졌다"
    assert "semantic.md" in paths, "LLM 보강분이 빠졌다"


def test_merged_results_have_no_duplicate_paths(isolated, monkeypatch):
    """FTS와 LLM이 같은 문서를 지목해도 중복 반환하면 안 된다."""
    from mneme import index, llm, server

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "요약")
    (isolated / "dup.md").write_text("---\nt: d\n---\n\nGrafana here.\n", encoding="utf-8")
    index.reindex_all()

    monkeypatch.setattr(llm, "select_candidate_paths", lambda q, s: ["dup.md"])
    monkeypatch.setattr(llm, "_call", lambda *a, **k: "종합 요약")

    out = server.wiki_search(query="Grafana", session_id="s3", agent="test")
    paths = [r["path"] for r in out["results"]]

    assert len(paths) == len(set(paths)), paths


def test_fts_queried_once_not_per_candidate(isolated, monkeypatch):
    """기존 구조는 후보 path마다 동일 인자로 search_fts를 다시 쳤다(N회 중복 조회).

    게다가 limit=3 고정이라 FTS가 4위로 잡은 문서는 후보에 있어도 버려졌다.
    """
    from mneme import index, llm, server

    monkeypatch.setattr(llm, "generate_summary", lambda p, c: "요약")
    (isolated / "a.md").write_text("---\nt: a\n---\n\nGrafana here.\n", encoding="utf-8")
    index.reindex_all()

    real = index.search_fts
    count = {"n": 0}

    def counting(*a, **kw):
        count["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(index, "search_fts", counting)
    monkeypatch.setattr(server.idx, "search_fts", counting)
    monkeypatch.setattr(llm, "select_candidate_paths", lambda q, s: ["a.md", "b.md", "c.md"])
    monkeypatch.setattr(llm, "_call", lambda *a, **k: "종합 요약")

    server.wiki_search(query="Grafana", session_id="s4", agent="test")

    assert count["n"] == 1, f"search_fts를 {count['n']}회 호출했다 (1회여야 함)"

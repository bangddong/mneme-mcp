"""reindex 시 미변경 페이지의 LLM 요약 재생성 스킵 검증.

기동 시 reindex_all()이 매번 전 페이지 요약을 재생성(~4분)하던 문제
(PROGRESS.md 에러 로그 07-03)에 대한 회귀 방지 테스트.
"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated(monkeypatch):
    """격리된 임시 WIKI_DIR/DB_PATH + generate_summary 호출 카운터."""
    tmp = tempfile.mkdtemp(prefix="mneme-reindex-")
    wiki = Path(tmp) / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(Path(tmp) / "state.db"))

    from mneme import memory, index, llm

    calls: list[str] = []

    def fake_summary(path, content):
        calls.append(path)
        return f"summary of {path}"

    monkeypatch.setattr(llm, "generate_summary", fake_summary)
    memory.init_db()
    return wiki, index, calls


def _write(wiki: Path, rel: str, body: str):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_first_reindex_summarizes_all(isolated):
    wiki, index, calls = isolated
    _write(wiki, "cat/a.md", "# A\n내용 A")
    _write(wiki, "cat/b.md", "# B\n내용 B")

    index.reindex_all()

    assert sorted(calls) == ["cat/a.md", "cat/b.md"]


def test_unchanged_pages_are_skipped_on_second_reindex(isolated):
    wiki, index, calls = isolated
    _write(wiki, "cat/a.md", "# A\n내용 A")
    _write(wiki, "cat/b.md", "# B\n내용 B")

    index.reindex_all()
    calls.clear()

    index.reindex_all()  # 아무것도 안 바뀜

    assert calls == []


def test_only_changed_page_is_resummarized(isolated):
    wiki, index, calls = isolated
    _write(wiki, "cat/a.md", "# A\n내용 A")
    _write(wiki, "cat/b.md", "# B\n내용 B")

    index.reindex_all()
    calls.clear()

    _write(wiki, "cat/a.md", "# A\n내용 A 수정됨")  # a만 변경
    index.reindex_all()

    assert calls == ["cat/a.md"]


def test_migration_adds_content_hash_to_legacy_db(monkeypatch):
    """content_hash 컬럼이 없던 구 DB도 init_db가 무손실 마이그레이션한다."""
    import sqlite3

    tmp = tempfile.mkdtemp(prefix="mneme-legacy-")
    wiki = Path(tmp) / "wiki"
    wiki.mkdir(parents=True)
    db = Path(tmp) / "state.db"
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(db))

    # 구 스키마(content_hash 없음)로 wiki_index 선생성 + 기존 행 1개
    legacy = sqlite3.connect(db)
    legacy.executescript(
        "CREATE TABLE wiki_index (path TEXT PRIMARY KEY, summary TEXT, "
        "tags TEXT, updated_at TEXT, updated_by TEXT);"
    )
    legacy.execute(
        "INSERT INTO wiki_index(path, summary) VALUES ('cat/old.md', 'old summary')"
    )
    legacy.commit()
    legacy.close()

    from mneme import memory

    memory.init_db()  # 마이그레이션 발생 — 예외 없이 컬럼 추가

    conn = memory.get_connection()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(wiki_index)")]
    kept = conn.execute(
        "SELECT summary FROM wiki_index WHERE path = 'cat/old.md'"
    ).fetchone()
    conn.close()

    assert "content_hash" in cols
    assert kept["summary"] == "old summary"  # 기존 데이터 보존


def test_search_still_finds_skipped_page(isolated):
    wiki, index, calls = isolated
    _write(wiki, "cat/a.md", "# A\nunique_token_zeta")

    index.reindex_all()
    index.reindex_all()  # 스킵 경로

    hits = index.search_fts("unique_token_zeta")
    assert any(h["path"] == "cat/a.md" for h in hits)

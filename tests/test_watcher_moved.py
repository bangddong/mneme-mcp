"""위키 파일/디렉토리 rename 시 인덱스 정합성 검증 (watcher.on_moved).

on_moved 핸들러가 없어 rename 후 옛 경로가 인덱스에 남고(존재하지 않는 파일이
wiki_search 결과에 노출) 새 경로는 인덱싱되지 않던 문제의 회귀 방지 테스트.
"""
import tempfile
from pathlib import Path

import pytest
from watchdog.events import DirMovedEvent, FileMovedEvent


@pytest.fixture()
def isolated(monkeypatch):
    """격리된 임시 WIKI_DIR/DB_PATH + 감시 루트 밖 디렉토리 + 핸들러."""
    tmp = Path(tempfile.mkdtemp(prefix="mneme-moved-"))
    wiki = tmp / "wiki"
    wiki.mkdir(parents=True)
    outside = tmp / "outside"
    outside.mkdir()
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(tmp / "state.db"))

    from mneme import memory, index, llm, watcher

    monkeypatch.setattr(llm, "generate_summary", lambda path, content: f"summary of {path}")
    memory.init_db()
    return wiki, outside, index, watcher.WikiEventHandler()


def _write(base: Path, rel: str, body: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _indexed(index) -> set[str]:
    return {r["path"] for r in index.get_all_summaries()}


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)


def test_file_rename_moves_index_entry(isolated):
    wiki, _outside, index, handler = isolated
    src = _write(wiki, "cat/old.md", "# Old\nunique_token_alpha")
    index.reindex_all()
    assert _indexed(index) == {"cat/old.md"}

    dest = wiki / "cat" / "new.md"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"cat/new.md"}
    assert [h["path"] for h in index.search_fts("unique_token_alpha")] == ["cat/new.md"]


def test_move_to_another_category_is_reindexed(isolated):
    wiki, _outside, index, handler = isolated
    src = _write(wiki, "cat/page.md", "# P\nunique_token_beta")
    index.reindex_all()

    dest = wiki / "other" / "page.md"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"other/page.md"}


def test_move_outside_wiki_root_only_removes(isolated):
    """dest가 감시 루트 밖이면 remove만 하고 index는 건너뛴다 (ValueError 방어)."""
    wiki, outside, index, handler = isolated
    src = _write(wiki, "cat/page.md", "# P\nunique_token_gamma")
    index.reindex_all()

    dest = outside / "page.md"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == set()
    assert index.search_fts("unique_token_gamma") == []


def test_move_into_wiki_root_only_indexes(isolated):
    """src가 루트 밖이면 index만 한다 (remove 대상 없음)."""
    wiki, outside, index, handler = isolated
    src = _write(outside, "page.md", "# P\nunique_token_delta")

    dest = wiki / "cat" / "page.md"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"cat/page.md"}


def test_rename_from_non_md_indexes_dest(isolated):
    """확장자는 src/dest 독립 판정 — .txt → .md 는 dest만 인덱싱."""
    wiki, _outside, index, handler = isolated
    src = _write(wiki, "cat/draft.txt", "# D\nunique_token_epsilon")

    dest = wiki / "cat" / "draft.md"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"cat/draft.md"}


def test_rename_to_non_md_removes_src(isolated):
    """.md → .txt 는 src만 제거 (dest는 인덱싱하지 않음)."""
    wiki, _outside, index, handler = isolated
    src = _write(wiki, "cat/page.md", "# P\nunique_token_zeta")
    index.reindex_all()

    dest = wiki / "cat" / "page.txt"
    _move(src, dest)
    handler.on_moved(FileMovedEvent(str(src), str(dest)))

    assert _indexed(index) == set()


def test_directory_rename_moves_whole_subtree(isolated):
    """디렉토리 rename — 하위 .md 전체가 옛 경로에서 빠지고 새 경로로 들어온다."""
    wiki, _outside, index, handler = isolated
    _write(wiki, "cat/a.md", "# A\n내용 A")
    _write(wiki, "cat/sub/b.md", "# B\n내용 B")
    _write(wiki, "keep/c.md", "# C\n내용 C")
    index.reindex_all()
    assert _indexed(index) == {"cat/a.md", "cat/sub/b.md", "keep/c.md"}

    src, dest = wiki / "cat", wiki / "renamed"
    _move(src, dest)
    handler.on_moved(DirMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"renamed/a.md", "renamed/sub/b.md", "keep/c.md"}


def test_directory_move_outside_root_removes_subtree(isolated):
    """디렉토리가 루트 밖으로 나가면 하위 .md 전부 인덱스에서 제거된다."""
    wiki, outside, index, handler = isolated
    _write(wiki, "cat/a.md", "# A\n내용 A")
    _write(wiki, "cat/sub/b.md", "# B\n내용 B")
    _write(wiki, "keep/c.md", "# C\n내용 C")
    index.reindex_all()

    src, dest = wiki / "cat", outside / "cat"
    _move(src, dest)
    handler.on_moved(DirMovedEvent(str(src), str(dest)))

    assert _indexed(index) == {"keep/c.md"}


def test_directory_rename_is_idempotent_with_synthetic_subevents(isolated):
    """watchdog은 디렉토리 이동 시 하위 파일 이벤트도 합성해 보낸다 — 중복 처리해도 동일 결과."""
    wiki, _outside, index, handler = isolated
    src_file = _write(wiki, "cat/a.md", "# A\n내용 A")
    index.reindex_all()

    src, dest = wiki / "cat", wiki / "renamed"
    _move(src, dest)
    handler.on_moved(DirMovedEvent(str(src), str(dest)))
    handler.on_moved(  # watchdog이 뒤이어 보내는 합성 FileMovedEvent
        FileMovedEvent(str(src_file), str(dest / "a.md"))
    )

    assert _indexed(index) == {"renamed/a.md"}

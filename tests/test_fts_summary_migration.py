"""기존 DB를 요약-색인 방식으로 한 번만 이관하는 마이그레이션 검증.

이 브랜치 이전에 색인된 DB는 두 가지가 낡았다:
  1. wiki_fts에 요약이 안 들어 있다 (원문만 색인)
  2. 요약 언어가 섞여 있다 (실측 33건: 한글 26 / 중국어 3 / 영어 4)

둘 다 재요약해야 풀린다. index_file은 content_hash가 같으면 재요약을 건너뛰므로,
해시를 무효화해서 다음 reindex_all()이 전량 재생성하게 한다.

⚠️ 반드시 '한 번만' 돌아야 한다. 매 기동마다 무효화하면 서버 시작마다 전량 재요약이
   돌아 132초(실측)를 태운다 — 그래서 schema_version 마커로 멱등성을 보장한다.
"""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def db_path(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="mneme-migration-")
    p = Path(tmp) / "state.db"
    monkeypatch.setenv("DB_PATH", str(p))
    monkeypatch.setenv("WIKI_DIR", str(Path(tmp) / "wiki"))
    return p


def _hashes(conn):
    return [r["content_hash"] for r in conn.execute("SELECT content_hash FROM wiki_index")]


def test_legacy_rows_are_invalidated_once(db_path):
    """구 DB의 content_hash를 비워 다음 reindex가 재요약하게 만든다."""
    from mneme import memory

    memory.init_db()
    conn = memory.get_connection()
    with conn:
        # 마이그레이션 이전 상태를 재현: 해시가 채워진 기존 행
        conn.execute(
            "INSERT INTO wiki_index(path, summary, content_hash, updated_at, updated_by)"
            " VALUES('old.md', '---', 'deadbeef', '2026-01-01', 'system')"
        )
        # 구 버전 마커 제거 — 아직 이관 안 된 DB
        conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.close()

    memory.init_db()

    conn = memory.get_connection()
    assert _hashes(conn) == [None], "구 행의 content_hash가 무효화되지 않았다"
    conn.close()


def test_migration_is_idempotent(db_path):
    """두 번째 init_db는 아무것도 건드리면 안 된다 (매 기동 전량 재요약 방지)."""
    from mneme import memory

    memory.init_db()  # 신규 DB — 여기서 마커가 찍힌다
    conn = memory.get_connection()
    with conn:
        conn.execute(
            "INSERT INTO wiki_index(path, summary, content_hash, updated_at, updated_by)"
            " VALUES('fresh.md', '요약', 'cafebabe', '2026-01-01', 'system')"
        )
    conn.close()

    memory.init_db()  # 재기동

    conn = memory.get_connection()
    assert _hashes(conn) == ["cafebabe"], "이미 이관된 DB를 또 무효화했다 — 매 기동 재요약이 돈다"
    conn.close()


def test_fresh_db_is_marked_current(db_path):
    """새로 만든 DB는 처음부터 최신이므로 마커가 있어야 한다."""
    from mneme import memory

    memory.init_db()
    conn = memory.get_connection()
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    conn.close()

    assert row is not None, "schema_version 마커가 없다"
    assert int(row["value"]) >= memory.SCHEMA_VERSION

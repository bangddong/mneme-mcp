import sqlite3
import hashlib
from datetime import datetime, timezone
from mneme.memory import get_connection
from mneme.wiki import read_file, list_md_files
from mneme import llm


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def index_file(path: str, updated_by: str = "system", force: bool = False):
    data = read_file(path)
    if data is None:
        return

    content = data["content"]
    chash = _content_hash(content)

    conn = get_connection()
    if not force:
        # 미변경 페이지는 값비싼 LLM 재요약을 건너뛴다 (기동 시간 단축).
        # 원문 해시가 같고 FTS에도 남아 있으면 이미 인덱싱된 상태.
        row = conn.execute(
            "SELECT content_hash FROM wiki_index WHERE path = ?", (path,)
        ).fetchone()
        in_fts = conn.execute(
            "SELECT 1 FROM wiki_fts WHERE path = ? LIMIT 1", (path,)
        ).fetchone()
        if row is not None and row["content_hash"] == chash and in_fts is not None:
            conn.close()
            return

    summary = llm.generate_summary(path, content)
    now = datetime.now(tz=timezone.utc).isoformat()

    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute(
            "INSERT INTO wiki_fts(path, content) VALUES (?, ?)",
            (path, content),
        )
        conn.execute(
            """
            INSERT INTO wiki_index(path, summary, content_hash, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                summary = excluded.summary,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (path, summary, chash, now, updated_by),
        )
    conn.close()


def remove_from_index(path: str):
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute("DELETE FROM wiki_index WHERE path = ?", (path,))
    conn.close()


def search_fts(query: str, limit: int = 10) -> list[dict]:
    """FTS5 전문 검색. 사용자 질의가 FTS5 구문으로 깨져도 예외를 내보내지 않는다.

    MATCH 오른쪽은 FTS5 쿼리 문법으로 파싱된다. 사용자·에이전트가 넘기는 문자열은
    임의라서 `"`(unterminated string), `*`, `NEAR(` 같은 토큰이 섞이면
    sqlite3.OperationalError가 난다. 검색 도구가 질의 '모양' 때문에 터지면 안 되므로,
    구문 오류일 때는 질의 전체를 하나의 구(phrase)로 escape해 재시도한다.
    """
    sql = """
        SELECT path, snippet(wiki_fts, 1, '[', ']', '...', 20) AS excerpt
        FROM wiki_fts
        WHERE wiki_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """
    conn = get_connection()
    try:
        try:
            rows = conn.execute(sql, (query, limit)).fetchall()
        except sqlite3.OperationalError:
            # FTS5에서 큰따옴표는 안쪽에서 두 번 써서 escape한다.
            phrase = '"' + query.replace('"', '""') + '"'
            try:
                rows = conn.execute(sql, (phrase, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
    finally:
        conn.close()
    return [{"path": r["path"], "excerpt": r["excerpt"]} for r in rows]


def get_all_summaries() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT path, summary, tags FROM wiki_index ORDER BY path"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reindex_all():
    # 스캐폴딩(_index/_log/CLAUDE.md/.claude)은 인덱싱 제외 — 콘텐츠 페이지만
    for path in list_md_files(content_only=True):
        index_file(path, updated_by="system")

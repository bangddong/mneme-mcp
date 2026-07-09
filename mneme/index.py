from datetime import datetime, timezone
from mneme.memory import get_connection
from mneme.wiki import read_file, list_md_files
from mneme import llm


def index_file(path: str, updated_by: str = "system"):
    data = read_file(path)
    if data is None:
        return

    content = data["content"]
    summary = llm.generate_summary(path, content)
    now = datetime.now(tz=timezone.utc).isoformat()

    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute(
            "INSERT INTO wiki_fts(path, content) VALUES (?, ?)",
            (path, content),
        )
        conn.execute(
            """
            INSERT INTO wiki_index(path, summary, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                summary = excluded.summary,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (path, summary, now, updated_by),
        )
    conn.close()


def remove_from_index(path: str):
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute("DELETE FROM wiki_index WHERE path = ?", (path,))
    conn.close()


def search_fts(query: str, limit: int = 10) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT path, snippet(wiki_fts, 1, '[', ']', '...', 20) AS excerpt
        FROM wiki_fts
        WHERE wiki_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
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

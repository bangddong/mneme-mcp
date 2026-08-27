"""Legacy Wiki indexing implementation retained behind :mod:`mneme.index`."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from mneme import llm
from mneme.core.search.fts import search_table
from mneme.memory import get_connection
from mneme.wiki import list_md_files, read_file


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fts_document(content: str, summary: str | None) -> str:
    """Combine existing Wiki content with its generated summary for FTS."""
    summary = (summary or "").strip()
    return f"{content}\n\n{summary}" if summary else content


def index_file(path: str, updated_by: str = "system", force: bool = False):
    data = read_file(path)
    if data is None:
        return

    content = data["content"]
    content_hash = _content_hash(content)
    conn = get_connection()
    if not force:
        row = conn.execute(
            "SELECT content_hash FROM wiki_index WHERE path = ?", (path,)
        ).fetchone()
        in_fts = conn.execute(
            "SELECT 1 FROM wiki_fts WHERE path = ? LIMIT 1", (path,)
        ).fetchone()
        if row is not None and row["content_hash"] == content_hash and in_fts is not None:
            conn.close()
            return

    summary = llm.generate_summary(path, content)
    now = datetime.now(tz=timezone.utc).isoformat()
    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute(
            "INSERT INTO wiki_fts(path, content) VALUES (?, ?)",
            (path, _fts_document(content, summary)),
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
            (path, summary, content_hash, now, updated_by),
        )
    conn.close()


def remove_from_index(path: str):
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM wiki_fts WHERE path = ?", (path,))
        conn.execute("DELETE FROM wiki_index WHERE path = ?", (path,))
    conn.close()


def search_fts(query: str, limit: int = 10) -> list[dict]:
    """Return legacy dict-shaped Wiki FTS results via deterministic Core search."""
    conn = get_connection()
    try:
        hits = search_table(conn, "wiki_fts", query, limit)
    finally:
        conn.close()
    return [{"path": hit.path, "excerpt": hit.excerpt} for hit in hits]


def get_all_summaries() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT path, summary, tags FROM wiki_index ORDER BY path"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def reindex_all():
    for path in list_md_files(content_only=True):
        index_file(path, updated_by="system")

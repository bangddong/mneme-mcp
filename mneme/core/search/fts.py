"""Deterministic SQLite FTS5 query helpers for Core search."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from .korean import expand_query


_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SearchHit:
    path: str
    excerpt: str
    source: str | None = None


def search_table(
    conn: sqlite3.Connection,
    table: str,
    query: str,
    limit: int,
    *,
    source: str | None = None,
) -> list[SearchHit]:
    """Search an FTS5 table without importing optional generation services.

    FTS5 table identifiers cannot be query parameters, so only ordinary SQLite
    identifiers are accepted. Query text and the result limit remain bound
    parameters. Invalid FTS syntax is retried once as a literal phrase.
    """
    if not _TABLE_IDENTIFIER.fullmatch(table):
        raise ValueError(f"Unsafe FTS table identifier: {table!r}")

    sql = f"""
        SELECT path, snippet({table}, 1, '[', ']', '...', 20) AS excerpt
        FROM {table}
        WHERE {table} MATCH ?
        ORDER BY rank
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, (expand_query(query), limit)).fetchall()
    except sqlite3.OperationalError:
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(sql, (phrase, limit)).fetchall()
        except sqlite3.OperationalError:
            return []

    return [
        SearchHit(path=row["path"], excerpt=row["excerpt"], source=source)
        for row in rows
    ]

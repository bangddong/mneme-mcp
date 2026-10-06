import sqlite3
import sys

import pytest


def _connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "fts.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(path, content, tokenize='unicode61')"
    )
    conn.execute(
        "INSERT INTO docs VALUES (?, ?)",
        ("ops.md", "Grafana dashboard 비용 최적화"),
    )
    return conn


def test_search_table_handles_korean_and_broken_syntax_without_llm(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mneme.llm", None)

    from mneme.core.search.fts import search_table

    conn = _connection(tmp_path)
    try:
        assert [hit.path for hit in search_table(conn, "docs", "그라파나", 10)] == [
            "ops.md"
        ]
        assert isinstance(search_table(conn, "docs", 'Grafana "bad', 10), list)
    finally:
        conn.close()


def test_search_table_marks_hits_with_the_given_source(tmp_path):
    from mneme.core.search.fts import search_table

    conn = _connection(tmp_path)
    try:
        hits = search_table(conn, "docs", "Grafana", 10, source="wiki")
    finally:
        conn.close()

    assert [(hit.path, hit.source) for hit in hits] == [("ops.md", "wiki")]


def test_search_table_supports_default_sqlite_tuple_rows(tmp_path):
    from mneme.core.search.fts import search_table

    conn = sqlite3.connect(tmp_path / "tuple-rows.db")
    conn.execute("CREATE VIRTUAL TABLE docs USING fts5(path, content)")
    conn.execute("INSERT INTO docs VALUES (?, ?)", ("ops.md", "Grafana dashboard"))
    try:
        hits = search_table(conn, "docs", "Grafana", 10)
    finally:
        conn.close()

    assert [(hit.path, hit.excerpt) for hit in hits] == [
        ("ops.md", "[Grafana] dashboard")
    ]


@pytest.mark.parametrize("limit", [0, -1])
def test_search_table_returns_no_hits_for_non_positive_limits(tmp_path, limit):
    from mneme.core.search.fts import search_table

    conn = _connection(tmp_path)
    try:
        assert search_table(conn, "docs", "Grafana", limit) == []
    finally:
        conn.close()


@pytest.mark.parametrize("table", ["docs; DROP TABLE docs", "docs-name", '"docs"'])
def test_search_table_rejects_unsafe_table_identifiers(tmp_path, table):
    from mneme.core.search.fts import search_table

    conn = _connection(tmp_path)
    try:
        with pytest.raises(ValueError, match="table"):
            search_table(conn, table, "Grafana", 10)
    finally:
        conn.close()

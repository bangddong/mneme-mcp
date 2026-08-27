import pytest


LEGACY_TABLES = (
    "wiki_fts",
    "wiki_index",
    "facts",
    "episodes",
    "skills",
    "working",
    "loop_cycles",
    "self_model",
    "growth_actions",
    "meta",
)


def snapshot_legacy_tables(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in LEGACY_TABLES
    }


def test_search_without_llm_keeps_shape_cache_and_episode(legacy_runtime, monkeypatch):
    from mneme import llm, server

    monkeypatch.setattr(llm, "select_candidate_paths", lambda *_: [])
    monkeypatch.setattr(
        llm,
        "_call",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    out = server.wiki_search("Grafana", session_id="char-search", agent="characterizer")
    assert set(out) == {"results", "summary"}
    assert out["results"] and out["summary"]
    conn = legacy_runtime.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM working").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    conn.close()


def test_korean_and_broken_fts_queries_remain_safe(legacy_runtime):
    from mneme import index

    assert index.search_fts("그라파나")
    assert isinstance(index.search_fts('Grafana "unclosed'), list)


def test_inject_rejects_invalid_page_without_writing(legacy_runtime):
    from mneme import server, wiki

    out = server.wiki_inject("bad.md", "not OKF", "characterizer", "char-write")
    assert out["action"] == "rejected"
    assert wiki.read_file("bad.md") is None


def test_server_main_preserves_startup_and_cleanup_order(monkeypatch):
    from mneme import server

    calls = []
    monkeypatch.setattr(server, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(server.idx, "reindex_all", lambda: calls.append("reindex"))
    monkeypatch.setattr(server, "start_watcher", lambda: calls.append("watcher-start"))
    monkeypatch.setattr(server, "start_scheduler", lambda: calls.append("scheduler-start"))
    monkeypatch.setattr(server, "stop_scheduler", lambda: calls.append("scheduler-stop"))
    monkeypatch.setattr(server, "stop_watcher", lambda: calls.append("watcher-stop"))
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    with pytest.raises(RuntimeError, match="stop"):
        server.main()
    assert calls == [
        "init", "reindex", "watcher-start", "scheduler-start",
        "scheduler-stop", "watcher-stop",
    ]


def test_reinitialization_preserves_every_legacy_table_row(legacy_runtime):
    from mneme import memory

    conn = memory.get_connection()
    with conn:
        conn.execute("INSERT INTO facts(content) VALUES('keep-fact')")
        conn.execute("INSERT INTO episodes(agent, tool) VALUES('keep-agent', 'keep-tool')")
        conn.execute("INSERT INTO skills(name, description) VALUES('keep-skill', 'keep')")
        conn.execute(
            "INSERT INTO working(session_id, key, value, expires_at) VALUES(?, ?, ?, ?)",
            ("keep-session", "keep-key", "keep-value", "2099-01-01"),
        )
        conn.execute("INSERT INTO loop_cycles(episodes_processed) VALUES(1)")
        conn.execute("INSERT INTO self_model(episodes_seen) VALUES(1)")
        conn.execute("INSERT INTO growth_actions(kind, status) VALUES('keep-growth', 'open')")
        conn.execute("INSERT INTO meta(key, value) VALUES('keep-meta', 'keep-value')")
    before = snapshot_legacy_tables(conn)
    assert all(before.values())
    conn.close()
    memory.init_db()
    conn = memory.get_connection()
    after = snapshot_legacy_tables(conn)
    conn.close()
    assert after == before

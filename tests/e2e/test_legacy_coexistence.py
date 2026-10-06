"""Lossless local legacy staging and coexistence with the opt-in Vault path."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest


def _create_legacy_sources(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    db_path = tmp_path / "legacy" / "state.db"
    db_path.parent.mkdir()
    episode = {
        "session_id": "legacy-session-17",
        "agent": "legacy-agent",
        "tool": "wiki_search",
        "query": "full legacy query payload",
        "result_summary": "full legacy result payload",
        "success": 1,
        "score": 3.25,
        "what_worked": "preserved every field",
        "what_failed": b"\x00\xfflegacy-failure",
        "next_hint": "review before promotion",
        "skills_used": '["careful-migration"]',
        "created_at": "2026-08-26T00:00:01Z",
    }
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                agent TEXT,
                tool TEXT,
                query TEXT,
                result_summary TEXT,
                success INTEGER,
                score REAL,
                what_worked TEXT,
                what_failed TEXT,
                next_hint TEXT,
                skills_used TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE future_durable_data (
                entry_id TEXT PRIMARY KEY,
                payload BLOB
            );
            """
        )
        connection.execute(
            """
            INSERT INTO episodes(
                session_id, agent, tool, query, result_summary, success, score,
                what_worked, what_failed, next_hint, skills_used, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(episode.values()),
        )
        connection.execute(
            "INSERT INTO future_durable_data(entry_id, payload) VALUES (?, ?)",
            ("unknown-1", b"\xca\xfe\x00\x10"),
        )
        connection.commit()
    finally:
        connection.close()

    wiki = tmp_path / "legacy-wiki"
    wiki.mkdir()
    (wiki / "Home.md").write_text(
        "# Legacy Wiki\n\nThe legacy path remains authoritative.\n",
        encoding="utf-8",
    )
    return db_path, wiki, episode


def _portable_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _table_export(bundle: Path, manifest: dict, table_name: str) -> tuple[dict, dict]:
    table = next(item for item in manifest["tables"] if item["name"] == table_name)
    export_path = bundle / table["export_path"]
    assert sha256(export_path.read_bytes()).hexdigest() == table["sha256"]
    return table, json.loads(export_path.read_text(encoding="utf-8"))


def _decoded_values(row: dict[str, object]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for item in row["values"]:
        value = item["value"]
        if item["storage_class"] == "NULL":
            decoded[item["column"]] = None
        elif item["storage_class"] == "INTEGER":
            decoded[item["column"]] = int(value)
        elif item["storage_class"] == "REAL":
            decoded[item["column"]] = float.fromhex(value)
        elif item.get("encoding") == "base64":
            decoded[item["column"]] = base64.b64decode(value)
        else:
            decoded[item["column"]] = value
    return decoded


def test_legacy_database_stages_losslessly_and_promotes_only_reviewed_semantics(
    tmp_path
):
    """Staging must preserve mixed legacy payloads locally and never auto-promote."""
    from mneme.core.contracts import CoreCommand
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault
    from mneme.migration.legacy import stage_legacy

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    db_path, wiki, episode = _create_legacy_sources(tmp_path)
    database_before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    wiki_before = _portable_snapshot(wiki)
    portable_before = _portable_snapshot(vault.root)

    report = stage_legacy(
        db_path, wiki, vault, vault.local_root / "pending"
    )

    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == database_before
    assert _portable_snapshot(wiki) == wiki_before
    assert _portable_snapshot(vault.root) == portable_before
    assert report.database_sha256 == sha256(database_before[0]).hexdigest()
    assert report.bundle_path.is_relative_to(vault.local_root)
    assert not report.bundle_path.is_relative_to(vault.root)
    assert (
        report.bundle_path / "source" / "state.db"
    ).read_bytes() == database_before[0]
    assert not list(report.bundle_path.rglob("*.md"))
    assert not list((vault.root / "memory").glob("*.md"))

    manifest = json.loads(
        (report.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    episodes_manifest, episodes_export = _table_export(
        report.bundle_path, manifest, "episodes"
    )
    unknown_manifest, unknown_export = _table_export(
        report.bundle_path, manifest, "future_durable_data"
    )
    assert episodes_manifest["classification"] == "candidate-durable-local"
    assert episodes_manifest["destination"] == "local-review"
    assert episodes_manifest["review_status"] == "needs-review"
    assert episodes_manifest["portable"] is False
    assert episodes_manifest["accepted"] is False
    assert unknown_manifest["classification"] == "needs-review"
    assert unknown_manifest["destination"] == "local-review"
    assert unknown_manifest["blocks_portable_action"] is True
    assert manifest["portable_action"]["status"] == "blocked"
    assert manifest["portable_action"]["blocking_tables"] == [
        "future_durable_data"
    ]

    episode_values = _decoded_values(episodes_export["rows"][0])
    assert episode_values == {"id": 1, **episode}
    unknown_values = _decoded_values(unknown_export["rows"][0])
    assert unknown_values == {
        "entry_id": "unknown-1",
        "payload": b"\xca\xfe\x00\x10",
    }

    service = CoreService(vault)
    submitted = service.execute(
        CoreCommand(
            1,
            "submit_memory",
            {
                "memory_id": "reviewed-legacy-lesson",
                "kind": "lesson",
                "scope": {"type": "personal-global"},
                "authority": "personal",
                "portability": "personal-vault",
                "storage_class": "portable",
                "body": (
                    "Review legacy records locally and promote only a sanitized "
                    "semantic lesson."
                ),
                "rationale": "Explicit human-reviewed migration output.",
            },
        )
    )
    assert submitted.ok is True
    promoted = service.execute(
        CoreCommand(
            1,
            "promote_memory",
            {
                "memory_id": "reviewed-legacy-lesson",
                "expected_generation": 0,
                "storage_class": "portable",
            },
        )
    )
    assert promoted.ok is True
    memory_files = list((vault.root / "memory").glob("*.md"))
    assert memory_files == [vault.root / "memory" / "reviewed-legacy-lesson.md"]
    promoted_text = memory_files[0].read_text(encoding="utf-8")
    assert "sanitized semantic lesson" in promoted_text
    for raw_value in (
        episode["session_id"],
        episode["query"],
        episode["result_summary"],
        episode["what_worked"],
        "unknown-1",
    ):
        assert raw_value not in promoted_text


def test_legacy_http_wiki_db_watcher_scheduler_and_growth_still_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """The opt-in Vault path must not replace any historical runtime surface."""
    runtime_wiki = tmp_path / "runtime-wiki"
    runtime_wiki.mkdir()
    monkeypatch.setenv("WIKI_DIR", str(runtime_wiki))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "runtime-state.db"))

    from mneme import growth, memory, scheduler, server, watcher, wiki
    from mneme.transports import legacy_http

    memory.init_db()
    connection = memory.get_connection()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO episodes(session_id, agent, tool, query, result_summary, success)
                VALUES ('coexist-session', 'legacy-agent', 'wiki_get', 'query', 'result', 1)
                """
            )
        assert connection.execute(
            "SELECT result_summary FROM episodes WHERE session_id='coexist-session'"
        ).fetchone()[0] == "result"
    finally:
        connection.close()

    wiki.write_file("coexist.md", "# Legacy coexistence\n")
    assert wiki.read_file("coexist.md")["content"] == "# Legacy coexistence\n"
    handler = watcher.WikiEventHandler()
    assert handler._to_relative(str(runtime_wiki / "coexist.md")) == "coexist.md"

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        scheduler.outer_loop,
        "run_cycle",
        lambda *, force: calls.append(("outer-loop", force)) or {"ran": False},
    )
    monkeypatch.setattr(
        scheduler.self_model,
        "assess",
        lambda *, force: calls.append(("self-model", force)) or {"ran": False},
    )
    scheduler._tick()
    assert calls == [("outer-loop", False), ("self-model", False)]

    action_id = growth.record(
        "migration", "warn", "coexistence", "legacy path is active", "legacy-active"
    )
    assert growth.open_actions()[0]["id"] == action_id
    assert server is legacy_http
    response = server.wiki_get("coexist.md")
    assert set(response) == {"path", "content", "updated_at", "updated_by"}
    assert response["path"] == "coexist.md"
    assert response["content"] == "# Legacy coexistence\n"
    assert response["updated_at"]
    assert response["updated_by"] == "unknown"
    assert callable(server.main)

    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "0")
    boundary_calls: list[object] = []
    monkeypatch.setattr(server, "init_db", lambda: boundary_calls.append("init"))
    monkeypatch.setattr(
        server.idx, "reindex_all", lambda: boundary_calls.append("reindex")
    )
    monkeypatch.setattr(
        server, "start_watcher", lambda: boundary_calls.append("watcher-start")
    )
    monkeypatch.setattr(
        server, "start_scheduler", lambda: boundary_calls.append("scheduler-start")
    )
    monkeypatch.setattr(
        server, "stop_scheduler", lambda: boundary_calls.append("scheduler-stop")
    )
    monkeypatch.setattr(
        server, "stop_watcher", lambda: boundary_calls.append("watcher-stop")
    )

    def stop_at_transport_boundary(**kwargs):
        boundary_calls.append(("run", kwargs))
        raise RuntimeError("stop legacy http boundary")

    monkeypatch.setattr(server.mcp, "run", stop_at_transport_boundary)
    with pytest.raises(RuntimeError, match="stop legacy http boundary"):
        server.main()

    assert boundary_calls == [
        "init",
        "reindex",
        "watcher-start",
        "scheduler-start",
        (
            "run",
            {
                "transport": "streamable-http",
                "host": "127.0.0.1",
                "port": 0,
            },
        ),
        "scheduler-stop",
        "watcher-stop",
    ]

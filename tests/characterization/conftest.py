from pathlib import Path

import pytest


@pytest.fixture()
def legacy_runtime(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "state.db"))
    from mneme import index, llm, memory

    monkeypatch.setattr(llm, "generate_summary", lambda *_: "그라파나 운영 문서")
    memory.init_db()
    (wiki / "ops").mkdir()
    (wiki / "ops" / "grafana.md").write_text(
        "---\ntype: concept\ntitle: Grafana\ntimestamp: 2026-08-26\ntags: [ops]\n---\n"
        "## Summary\nGrafana\n## Details\nDashboard\n## Sources\n- source\n## Related\n- none\n",
        encoding="utf-8",
    )
    index.reindex_all()
    return memory

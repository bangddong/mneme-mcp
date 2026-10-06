# Mneme to Madi Incremental Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the existing Mneme runtime into the approved person-owned Madi Core without a Big Bang rewrite, while preserving legacy behavior and every potentially durable legacy datum.

**Architecture:** Keep the `mneme` repository and Python package names during this migration. First freeze current behavior, then extract deterministic search/validation/source seams behind compatibility facades, add the D3 Vault as an opt-in parallel path, migrate transports and adapters, isolate Growth Lab, and only then provide explicit non-destructive legacy export tooling. Every phase leaves the legacy HTTP MCP path runnable.

**Tech Stack:** Python 3.11+, standard library (`argparse`, `dataclasses`, `enum`, `pathlib`, `sqlite3`, `subprocess`), PyYAML, SQLite FTS5, FastMCP, watchdog, pytest, Git CLI

**Spec:** `docs/superpowers/specs/2026-08-26-madi-d3-vault-design.md` — **binding authority**. If this plan and the approved D3 differ, stop execution and amend the plan; do not reinterpret the spec in code.

## Global Constraints

- Do not rename the repository, distribution, or `mneme` Python package in this plan.
- Do not implement on `main` or `master`. Execution starts in an isolated worktree on branch `feat/madi-core-migration`.
- Do not perform a Big Bang rewrite. Compatibility facades remain until a separately approved removal plan.
- Do not delete, truncate, rewrite, or silently reclassify legacy `state.db`, external Wiki Markdown, or Growth data.
- Run the focused test before and after each implementation step, then run the full suite before every task commit.
- Keep the current `mneme.server:main` HTTP MCP path runnable after every task.
- Madi Core must not import `mneme.llm`, FastMCP, watchdog, APScheduler, Claude, Codex, or Growth Lab modules.
- A generation LLM, embedding provider, daemon, and agent hook are optional; their absence cannot break Core recall or ordinary agent work.
- Portable canonical state is limited to Registry artifacts, immutable Session revisions, and versioned Memory records. CURRENT, PROFILE, SQLite, vector data, locks, and logs are generated/local.
- Portable and local-only Registry, Session, and Memory artifacts use the same schemas through an explicit `StorageClass`/`ArtifactLocation` seam. Resolver/context code is read-only with respect to canonical artifacts.
- Portable artifacts may never refer to local-only IDs, paths, counts, hashes, or existence. Local state may refer to portable state.
- Candidate bodies are CAS-mutable. Accepted semantic bodies are immutable; material change creates a new Memory ID with `supersedes`.
- A successful checkpoint writes an immutable Session revision before advancing a registry head. Git commit/push policy remains separate.
- All portable writes enforce the most restrictive inherited portability ceiling and record a portable-safe policy evaluation receipt.
- Active policy revisions and project/source policy assignments change only through Core CAS APIs. Generated-index policy fields are hints, never security authority; every output and sync preflight authorizes against current canonical policy pointers.
- All new file I/O explicitly uses UTF-8. Atomic writers use same-directory temporary files and `os.replace`, or exclusive creation for immutable artifacts.
- No task uses automatic semantic merge, force push, or last-write-wins for registry, session, memory, or policy conflicts.
- The only automatic registry merge is a bounded structural retry for proven-disjoint additions of valid session heads while all non-head fields remain unchanged. Same-session changes, removals, lifecycle changes, policy changes, and preferred-head changes conflict explicitly.
- Legacy `episodes` and unknown SQLite tables are potentially durable until reviewed. They are staged losslessly in local-only storage and are never discarded or promoted automatically; the original database remains untouched.
- Existing `docs/superpowers/specs/2026-08-01-recall-mounts-design.md` and its plan are design evidence only: `mneme/mounts.py`, `mneme/recall.py`, and mount tables do not exist on current `main`.
- Current workstation observation on 2026-08-26: `python`, `py`, `uv`, and `pytest` are unavailable. Task 0 must provision or select Python and establish a fresh baseline before any production edit.

---

## Current-to-Target File Map

No legacy production file is deleted in this plan.

| Current file | Action | Target/responsibility | Compatibility rule |
|---|---|---|---|
| `mneme/index.py` | **MOVE + WRAP + DEPRECATE coupling** | LLM-free query/search mechanics move to `mneme/core/search/fts.py`; legacy Wiki summary/index orchestration moves to `mneme/legacy/wiki_index.py` | `mneme.index` aliases the implementation module so monkeypatch behavior remains unchanged |
| `mneme/korean.py` | **MOVE + WRAP** | `mneme/core/search/korean.py` | `mneme.korean` aliases the Core module and preserves constants/functions |
| `mneme/lint.py` | **MOVE + WRAP** | Existing OKF Wiki lint moves to `mneme/core/validation/wiki.py`; Vault validation is separate in `mneme/core/validation/vault.py` | `mneme.lint` preserves CLI and private helpers currently used by `server.py` |
| `mneme/wiki.py` | **SPLIT + WRAP** | Generic read-only filesystem source goes to `mneme/core/sources/filesystem.py`; existing Wiki mutation/scaffolding moves to `mneme/legacy/wiki.py` | `mneme.wiki` preserves all seven legacy functions; Core source API exposes no write/delete function |
| `mneme/server.py` | **MOVE + WRAP + DEPRECATE lifecycle** | Existing tools and always-on HTTP lifecycle move to `mneme/transports/legacy_http.py`; new stdio transport is `mneme/transports/mcp_stdio.py` | `mneme.server` aliases the legacy module for imports and delegates `python -m`; old entry point stays runnable |
| `mneme/llm.py` | **SPLIT + WRAP + DEPRECATE Core use** | OpenAI-compatible HTTP client moves to `mneme/providers/openai_compatible.py`; legacy semantic helpers move to `mneme/legacy/llm.py` | `mneme.llm` aliases the legacy module so existing monkeypatches still intercept calls |
| `mneme/cib.py` | **MOVE + WRAP** | `mneme/growth_lab/cib.py` | Top-level import remains through a deprecation facade |
| `mneme/outer_loop.py` | **MOVE + WRAP** | `mneme/growth_lab/outer_loop.py` | Top-level import remains through a deprecation facade |
| `mneme/self_model.py` | **MOVE + WRAP** | `mneme/growth_lab/self_model.py` | Top-level import remains through a deprecation facade |
| `mneme/constitution.py`, `constitution.yaml`, `skills.py`, `growth.py`, `scheduler.py`, `notify.py`, `log.py` | **MOVE + WRAP where imported** | Complete Growth Lab dependency closure under `mneme/growth_lab/` | Legacy server tools keep working; Core tests run with Growth imports blocked |
| `mneme/memory.py` | **KEEP + DEPRECATE authority** | Legacy mixed SQLite schema remains untouched; D3 generated index uses a separate local DB through `mneme/core/search/index.py` | Never point new Core at a legacy DB or drop legacy tables |
| `mneme/watcher.py` | **KEEP legacy-only** | Continues to serve the external Wiki HTTP path | No Core startup dependency |
| `integration/install.py` and current templates | **KEEP + DEPRECATE after parallel adapters work** | New opt-in integrations live under `integration/madi/` | Existing Claude projects are not modified automatically |

Compatibility files whose attributes are monkeypatched by the current tests use a
module-object alias (`sys.modules[__name__] = implementation`) rather than copied
function bindings. CLI-capable facades (`mneme.server`, `mneme.lint`) call the
implementation's `main()` when executed with `python -m`. This preserves both
runtime global lookup and legacy module execution.

### Planned package structure

```text
mneme/
├── core/
│   ├── artifacts.py
│   ├── contracts.py
│   ├── context.py
│   ├── doctor.py
│   ├── errors.py
│   ├── fs.py
│   ├── git_sync.py
│   ├── memories.py
│   ├── policy.py
│   ├── registries.py
│   ├── resolver.py
│   ├── service.py
│   ├── sessions.py
│   ├── storage.py
│   ├── vault.py
│   ├── search/{fts.py,index.py,korean.py}
│   ├── sources/{filesystem.py,registry.py}
│   └── validation/{vault.py,wiki.py}
├── adapters/{base.py,claude.py,codex.py}
├── growth_lab/
├── legacy/{llm.py,server_tools.py,wiki.py,wiki_index.py}
├── migration/legacy.py
├── providers/openai_compatible.py
├── transports/{legacy_http.py,mcp_stdio.py}
├── cli.py
└── server.py
```

## Phase Gates and File Disposition

| Gate | Runnable state | KEEP | MOVE | WRAP | DEPRECATE | DELETE |
|---|---|---|---|---|---|---|
| Phase 0 | Current HTTP MCP and 42 collected legacy tests characterized | All production files | None | None | None | None |
| Phase 1 | Compatibility facades pass the full legacy suite | `memory.py`, `watcher.py` | deterministic search/lint/read seams, legacy server/provider implementations | all old import paths | LLM-bound indexing and always-on lifecycle as Core concepts | None |
| Phase 2 | Opt-in Vault can checkpoint, resolve, and remember without changing legacy startup | Legacy HTTP path | None | None | legacy SQLite as new authority | None |
| Phase 3 | CLI/stdio MCP can recall from Vault and read-only sources with no LLM/daemon | Legacy Wiki path | None | MCP legacy tools | mandatory daemon/provider assumptions | None |
| Phase 4 | Claude/Codex adapters and optional Growth Lab are isolated | Existing installer, legacy Growth tools | Growth closure | top-level Growth imports | Claude-only installer as default | None |
| Phase 5 | Explicit dry-run/export and end-to-end migration evidence exist | Original Wiki and DB | None | migration reports | direct legacy writes for new Madi workflows | None |

### Execution order and dependency gates

Tasks execute in numeric order in one isolated migration worktree. Each task is a
single subagent-driven-development unit with its own red/green/full-suite/commit
cycle; later tasks may not borrow uncommitted code from an earlier task.

| Task range | Hard dependency | Gate unlocked |
|---|---|---|
| 0 | Approved D3 and this plan | Isolated branch, reproducible baseline, exact existing CI matrix recorded |
| 1–6 | Task 0, then previous task commit | Legacy behavior characterized; deterministic modules and legacy HTTP transport separated behind facades |
| 7 | Tasks 1–6 | Read-only, fail-safe durable-data inventory before any Vault migration path |
| 8 | Task 7 | Explicit portable/local storage locations and safe filesystem primitives |
| 9 | Task 8 | Pure policy evaluation and immutable admission receipts |
| 10 | Task 9 | Immutable policy revision creation and active-pointer CAS |
| 11 | Task 10 | Project/source/workstream registries, policy assignment APIs, disjoint-head structural retry |
| 12 | Task 11 | Immutable checkpoint revisions using the storage and head-update seams |
| 13–14 | Task 12, then previous task commit | Read-only CURRENT resolver and Memory/PROFILE lifecycle over both storage classes |
| 15 | Tasks 13–14 | Rebuildable FTS/source recall with candidate-hit provenance |
| 16–18 | Task 15, then previous task commit | Doctor, CLI, and explicit Git/sync preflight |
| 19 | Tasks 10–18 | Live-policy security gate proven across every generated/read/sync consumer without reindex |
| 20 | Task 19 | Agent-neutral lifecycle/command/event contract and stdio MCP |
| 21–22 | Task 20, then previous adapter task | Non-blocking Claude adapter, then pinned native Codex `PreCompact` translator |
| 23 | Task 22 | Complete legacy Growth characterization before any Growth move |
| 24 | Task 23 | Pure Growth dependency move with compatibility facades |
| 25 | Task 24 | Scheduler/notify/legacy-server Growth integration isolated and reviewed separately |
| 26 | Tasks 7, 11, 14, 17, 25 | Lossless local legacy staging with no automatic promotion/deletion |
| 27 | All prior task commits | Clean/shallow clone, concurrency, compact, stale-policy privacy, migration, compatibility, and CI release evidence |

---

### Task 0: Isolated Worktree and Reproducible Baseline

**Files:**
- Modify: none

**Interfaces:**
- Produces: isolated worktree on `feat/madi-core-migration`
- Produces: recorded baseline command, Python version, and test result in the execution log
- Verifies: existing `.github/workflows/ci.yml` matrix is the cross-platform release gate

- [ ] **Step 1: Create isolation with the required skill**

Invoke `superpowers:using-git-worktrees`. Create a worktree from the plan-bearing commit on branch `feat/madi-core-migration`. Do not switch the primary checkout away from `main`.

- [ ] **Step 2: Prove the branch is not protected**

Run:

```powershell
git branch --show-current
git status --short
```

Expected: branch is `feat/madi-core-migration`; status is clean. Stop if the branch is `main` or `master`.

- [ ] **Step 3: Select Python before editing**

Run:

```powershell
python --version
python -m pip install -e ".[dev]"
```

Expected: Python 3.11 or 3.13 and successful editable install. If `python` is unavailable, provision it outside the repository and repeat this step; do not start Task 1.

- [ ] **Step 4: Record the unmodified baseline**

Run:

```powershell
python -m pytest --collect-only -q
python -m pytest -q
```

Expected: 42 current test functions are collected and the suite exits 0. If collection or tests fail, record the exact failure and repair the environment or create a separate baseline-fix plan before migration edits.

- [ ] **Step 5: Verify the existing CI matrix exactly**

Inspect `.github/workflows/ci.yml` and record this repository evidence in the
execution log:

- job `test` runs `matrix.os = [ubuntu-latest, windows-latest]`;
- `matrix.python-version = ["3.11", "3.13"]`;
- each combination uses `actions/setup-python@v5`, installs `.[dev]`, and runs
  `pytest -q`;
- job `ci-ok` depends on the full matrix and fails unless its aggregate result is
  `success`.

The workflow already satisfies the requested Python/OS matrix, so this plan does
not add a redundant CI-construction task. Execution must still open a PR and wait
for all four matrix cells plus `ci-ok`; a local test run is not evidence for the
Windows/Linux matrix.

- [ ] **Step 6: Do not commit environment-only setup**

Run `git status --short`. Expected: clean.

---

### Task 1: Freeze Five Legacy Behavior Contracts

**Files:**
- Create: `tests/characterization/conftest.py`
- Create: `tests/characterization/test_legacy_contracts.py`

**Interfaces:**
- Consumes: current `mneme.server`, `mneme.memory`, `mneme.index`, and `mneme.wiki`
- Produces: five black-box contracts that later facades must preserve

- [ ] **Step 1: Add the isolated fixture**

```python
# tests/characterization/conftest.py
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
```

- [ ] **Step 2: Add five black-box characterization tests**

```python
# tests/characterization/test_legacy_contracts.py
import pytest


def test_search_without_llm_keeps_shape_cache_and_episode(legacy_runtime, monkeypatch):
    from mneme import llm, server

    monkeypatch.setattr(llm, "select_candidate_paths", lambda *_: [])
    monkeypatch.setattr(
        llm, "_call",
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
        server.mcp, "run",
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
        conn.execute("INSERT INTO growth_actions(kind, status) VALUES('keep-growth', 'open')")
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("facts", "episodes", "skills", "growth_actions")
    }
    conn.close()
    memory.init_db()
    conn = memory.get_connection()
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    conn.close()
    assert after == before
```

- [ ] **Step 3: Run characterization against unmodified production**

Run: `python -m pytest tests/characterization/test_legacy_contracts.py -v`

Expected: all five pass. A failure means the test described the current code incorrectly; fix the test, not production behavior.

- [ ] **Step 4: Run the full baseline**

Run: `python -m pytest -q`

Expected: all legacy and five characterization tests pass.

- [ ] **Step 5: Commit**

```powershell
git add tests/characterization
git commit -m "test: freeze legacy Mneme behavior contracts"
```

---

### Task 2: Move Korean Normalization Behind a Compatibility Facade

**Files:**
- Create: `mneme/core/__init__.py`
- Create: `mneme/core/search/__init__.py`
- Move: `mneme/korean.py` to `mneme/core/search/korean.py`
- Create: `mneme/korean.py`
- Create: `tests/core/search/test_korean_facade.py`

**Interfaces:**
- Produces: `mneme.core.search.korean.{has_hangul,stem,expand_token,expand_query}`
- Preserves: `mneme.korean` public constants and functions

- [ ] **Step 1: Write the facade parity test**

```python
def test_legacy_and_core_korean_apis_match():
    from mneme import korean as legacy
    from mneme.core.search import korean as core

    for query in ("비용은", "그라파나 대시보드", 'Grafana "unclosed'):
        assert legacy.expand_query(query) == core.expand_query(query)
    assert legacy is core
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/core/search/test_korean_facade.py -v`

Expected: import failure for `mneme.core.search.korean`.

- [ ] **Step 3: Move the implementation and add the facade**

```python
# mneme/korean.py
"""Compatibility module alias; use mneme.core.search.korean for new code."""
import sys
from mneme.core.search import korean as _implementation

sys.modules[__name__] = _implementation
```

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/test_korean_query.py tests/core/search/test_korean_facade.py -v
python -m pytest -q
```

Expected: all pass with both import paths.

- [ ] **Step 5: Commit**

```powershell
git add mneme/korean.py mneme/core tests/core/search
git commit -m "refactor: move Korean search normalization into Core"
```

---

### Task 3: Separate Read-Only Sources and Wiki Validation from Legacy Writes

**Files:**
- Create: `mneme/core/sources/__init__.py`
- Create: `mneme/core/sources/filesystem.py`
- Move: `mneme/lint.py` to `mneme/core/validation/wiki.py`
- Create: `mneme/core/validation/__init__.py`
- Create: `mneme/lint.py`
- Move: `mneme/wiki.py` to `mneme/legacy/wiki.py`
- Create: `mneme/legacy/__init__.py`
- Create: `mneme/wiki.py`
- Create: `tests/core/sources/test_filesystem.py`
- Create: `tests/core/validation/test_wiki_facade.py`

**Interfaces:**
- Produces: `FileSystemSource(root: Path).list_markdown() -> list[str]`
- Produces: `FileSystemSource.read(path: str) -> str | None`
- Preserves: all current `mneme.wiki` and `mneme.lint` signatures

- [ ] **Step 1: Test path safety and structural read-only access**

```python
def test_filesystem_source_is_read_only_and_blocks_escape(tmp_path):
    from mneme.core.sources.filesystem import FileSystemSource

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A", encoding="utf-8")
    source = FileSystemSource(tmp_path)
    assert source.list_markdown() == ["docs/a.md"]
    assert source.read("docs/a.md") == "# A"
    assert source.read("../secret.md") is None
    assert {"write", "write_file", "delete", "delete_file", "save"}.isdisjoint(dir(source))
```

- [ ] **Step 2: Verify the source test fails**

Run: `python -m pytest tests/core/sources/test_filesystem.py -v`

Expected: import failure for `FileSystemSource`.

- [ ] **Step 3: Implement the source and compatibility facades**

`FileSystemSource` stores `root.resolve()`, normalizes separators, rejects absolute and `..` paths, verifies `candidate.resolve().is_relative_to(root)`, reads UTF-8, and sorts `rglob("*.md")` results.

The `mneme.wiki` facade aliases `mneme.legacy.wiki`. The `mneme.lint`
facade aliases `mneme.core.validation.wiki` for imports and delegates to its
`main()` under `python -m`. This preserves private helper monkeypatching as well as
the current CLI.

- [ ] **Step 4: Verify old writes and new read-only access**

```powershell
python -m pytest tests/core/sources/test_filesystem.py tests/core/validation/test_wiki_facade.py tests/characterization/test_legacy_contracts.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core mneme/legacy mneme/wiki.py mneme/lint.py tests/core
git commit -m "refactor: separate read-only sources from legacy Wiki writes"
```

---

### Task 4: Extract LLM-Free FTS Query Mechanics

**Files:**
- Create: `mneme/core/search/fts.py`
- Move: `mneme/index.py` to `mneme/legacy/wiki_index.py`
- Create: `mneme/index.py`
- Modify: `mneme/legacy/wiki_index.py`
- Create: `tests/core/search/test_fts.py`

**Interfaces:**
- Produces: `SearchHit(path: str, excerpt: str, source: str | None)`
- Produces: `search_table(conn, table: str, query: str, limit: int, *, source: str | None = None) -> list[SearchHit]`
- Preserves: `mneme.index.index_file`, `remove_from_index`, `search_fts`, `get_all_summaries`, `reindex_all`

- [ ] **Step 1: Test deterministic search with LLM imports blocked**

```python
def test_search_table_handles_korean_and_broken_syntax_without_llm(tmp_path, monkeypatch):
    import sqlite3
    import sys

    monkeypatch.setitem(sys.modules, "mneme.llm", None)
    from mneme.core.search.fts import search_table

    conn = sqlite3.connect(tmp_path / "fts.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE VIRTUAL TABLE docs USING fts5(path, content, tokenize='unicode61')")
    conn.execute("INSERT INTO docs VALUES('ops.md', 'Grafana dashboard 비용 최적화')")
    assert [h.path for h in search_table(conn, "docs", "그라파나", 10)] == ["ops.md"]
    assert isinstance(search_table(conn, "docs", 'Grafana "bad', 10), list)
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/core/search/test_fts.py -v`

- [ ] **Step 3: Implement safe search and the legacy facade**

Allow only table names matching `^[A-Za-z_][A-Za-z0-9_]*$`. Use `core.search.korean.expand_query`, retry malformed syntax as a quoted original phrase, and return an empty list after a second `sqlite3.OperationalError`. Do not import a provider or generation module.

Move legacy indexing to `legacy/wiki_index.py`; replace only its search SQL with `search_table(..., "wiki_fts", ...)` and convert dataclasses back to the existing dict shape. `mneme.index` aliases the implementation module.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/search/test_fts.py tests/test_hybrid_search.py tests/test_korean_query.py tests/test_wiki_search_fts_fallback.py -v
python -m pytest -q
```

Expected: legacy summary indexing is unchanged; Core search works with generation unavailable.

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/search mneme/legacy/wiki_index.py mneme/index.py tests/core/search
git commit -m "refactor: extract deterministic FTS mechanics into Core"
```

---

### Task 5: Isolate the Generation Provider from Semantic Helpers

**Files:**
- Create: `mneme/providers/__init__.py`
- Create: `mneme/providers/openai_compatible.py`
- Move: `mneme/llm.py` to `mneme/legacy/llm.py`
- Create: `mneme/llm.py`
- Modify: `mneme/legacy/llm.py`
- Create: `tests/providers/test_openai_compatible.py`
- Create: `tests/core/test_no_generation_imports.py`

**Interfaces:**
- Produces: `OpenAICompatibleProvider.is_available() -> bool`
- Produces: `OpenAICompatibleProvider.complete(system: str, user: str, json_mode: bool = False) -> str`
- Preserves: all nine current `mneme.llm` functions

- [ ] **Step 1: Test the Core import boundary**

```python
def test_core_source_does_not_import_generation_provider():
    from pathlib import Path

    offenders = []
    for path in Path("mneme/core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "mneme.llm" in text or "mneme.providers" in text or "httpx" in text:
            offenders.append(str(path))
    assert offenders == []
```

Provider tests monkeypatch `httpx.get` and `httpx.post` and assert the current URL, model, timeout, JSON response, and failure behavior.

- [ ] **Step 2: Verify provider tests fail**

Run: `python -m pytest tests/providers/test_openai_compatible.py tests/core/test_no_generation_imports.py -v`

- [ ] **Step 3: Move transport code and preserve semantic helpers**

`legacy.llm` owns `select_candidate_paths`, `judge_conflict`, `score_coherence`, `reflect_episode`, `assess_episode`, and `generate_summary`. Its `_call`, `_base_url`, `_model`, and `is_available` delegate to one `OpenAICompatibleProvider`. The top-level facade aliases `legacy.llm`, so current monkeypatches of `_call` and `generate_summary` still reach implementation globals.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/providers tests/test_hybrid_search.py tests/test_wiki_search_fts_fallback.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/providers mneme/legacy/llm.py mneme/llm.py tests/providers tests/core/test_no_generation_imports.py
git commit -m "refactor: isolate optional generation provider from Core"
```

---

### Task 6: Turn the Existing HTTP Server into a Compatibility Transport

**Files:**
- Create: `mneme/transports/__init__.py`
- Move: `mneme/server.py` to `mneme/transports/legacy_http.py`
- Create: `mneme/server.py`
- Create: `tests/transports/test_legacy_http_facade.py`

**Interfaces:**
- Produces: `mneme.transports.legacy_http.main() -> None`
- Preserves: `mneme.server.mcp`, constants, all existing tool callables, and `main`

- [ ] **Step 1: Write API identity test**

```python
def test_server_facade_exports_legacy_transport():
    from mneme import server
    from mneme.transports import legacy_http

    assert server is legacy_http
```

- [ ] **Step 2: Verify import failure before the move**

Run: `python -m pytest tests/transports/test_legacy_http_facade.py -v`

- [ ] **Step 3: Move implementation and explicitly re-export the old surface**

Keep `[project.scripts] mneme = "mneme.server:main"` unchanged. For normal imports,
replace `sys.modules[__name__]` with `legacy_http`; under `python -m mneme.server`,
call `legacy_http.main()`. Do not alter HTTP startup, reindex, watcher, scheduler,
or MCP tool registration in this task.

- [ ] **Step 4: Run characterization and full tests**

```powershell
python -m pytest tests/transports/test_legacy_http_facade.py tests/characterization/test_legacy_contracts.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit the Phase 1 gate**

```powershell
git add mneme/server.py mneme/transports tests/transports
git commit -m "refactor: wrap legacy HTTP MCP as an optional transport"
```

Expected Phase 1 state: the old command and tests behave identically; Core search, validation, and sources import no generation provider, scheduler, watcher, or FastMCP.

---

### Task 7: Inventory Legacy SQLite Without Mutation

**Files:**
- Create: `mneme/migration/__init__.py`
- Create: `mneme/migration/legacy.py`
- Create: `tests/migration/test_inventory.py`

**Interfaces:**
- Produces: `LegacyInventory(db_path: Path, tables: dict[str, LegacyTable], sha256: str)`
- Produces: `inspect_legacy_db(path: Path) -> LegacyInventory`
- Produces classifications: `generated`, `local-transient`, `candidate-durable-local`, `growth-local`, `needs-review`

- [ ] **Step 1: Test read-only inventory and classifications**

```python
def test_inventory_is_read_only_and_classifies_mixed_state(legacy_db):
    from hashlib import sha256
    from mneme.migration.legacy import inspect_legacy_db

    before = sha256(legacy_db.read_bytes()).hexdigest()
    report = inspect_legacy_db(legacy_db)
    after = sha256(legacy_db.read_bytes()).hexdigest()
    assert before == after == report.sha256
    assert report.tables["wiki_fts"].classification == "generated"
    assert report.tables["facts"].classification == "candidate-durable-local"
    assert report.tables["episodes"].classification == "candidate-durable-local"
    assert report.tables["skills"].classification == "growth-local"
```

Add a table unknown to current Mneme and assert it is `needs-review`, never
`generated` or `local-transient`. Record each classification reason and the exact
table schema so later staging can be audited without reopening it read-write.

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/migration/test_inventory.py -v`

Expected: import failure for `mneme.migration.legacy`.

- [ ] **Step 3: Implement read-only inspection**

Open SQLite with URI `file:<absolute-path>?mode=ro`, query `sqlite_master`, column
metadata, and row counts, never call `init_db`, and close before hashing. Classify
`wiki_fts` and `wiki_index` as generated; known runtime `working`/`meta` rows as
`local-transient` only where their schema and semantics are recognized; `facts`
and `episodes` as `candidate-durable-local`; and `skills`, `loop_cycles`,
`self_model`, and `growth_actions` as `growth-local`. Unknown tables are
`needs-review`. Classification controls staging defaults, never deletion, and no
classification raises portability above local-only.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/migration/test_inventory.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/migration tests/migration
git commit -m "feat: inventory legacy state without mutating durable data"
```

---

### Task 8: Add Vault Paths, Deterministic Codecs, and Atomic Writes

**Files:**
- Create: `mneme/core/errors.py`
- Create: `mneme/core/fs.py`
- Create: `mneme/core/artifacts.py`
- Create: `mneme/core/storage.py`
- Create: `mneme/core/vault.py`
- Create: `mneme/core/validation/vault.py`
- Create: `tests/core/test_vault.py`
- Create: `tests/core/test_fs.py`
- Create: `tests/core/test_storage.py`

**Interfaces:**
- Produces: `Vault.open(root: Path, state_home: Path) -> Vault`
- Produces: `Vault.initialize(root: Path, state_home: Path, owner_id: str) -> Vault`
- Produces: `write_new(path: Path, text: str) -> None`
- Produces: `write_yaml_cas(path: Path, value: dict, expected_generation: int) -> None`
- Produces: `read_frontmatter(path: Path) -> tuple[dict, str]`
- Produces: `StorageClass.PORTABLE | LOCAL_ONLY`
- Produces: `ArtifactLocation(storage_class, root, relative_path)` and `StorageRouter`
- Produces: canonical `ArtifactStore`/`ArtifactReader` and generated-only `ViewStore` boundaries

- [ ] **Step 1: Test exact portable/local layout**

```python
def test_initialize_creates_only_portable_registries_in_vault(tmp_path):
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    assert (vault.root / ".madi/schema-version").read_text(encoding="utf-8") == "1\n"
    assert (vault.root / ".madi/vault.yaml").exists()
    assert vault.local_root == tmp_path / "state" / "vaults" / vault.id
    assert not (vault.root / "CURRENT.md").exists()
    assert not (vault.root / "state.db").exists()
```

Add tests that `write_new` raises `ArtifactExists`, CAS rejects a stale generation, YAML serialization is stable, and Markdown frontmatter round-trips UTF-8.

Add storage-boundary tests that portable and local-only Registry, Session, and
Memory locations use the same family codecs/schema; only their roots differ.
Require every canonical writer to receive an explicit `StorageClass` or
`ArtifactLocation`. Reject a portable artifact whose metadata or body references
any local-only ID, path, hash, count, label, or existence at write time. A local
artifact may reference a portable artifact. Assert `ViewStore` cannot write under
either canonical artifact tree.

- [ ] **Step 2: Verify missing imports**

Run: `python -m pytest tests/core/test_vault.py tests/core/test_fs.py tests/core/test_storage.py -v`

- [ ] **Step 3: Implement minimal Vault mechanics**

Initialize `.madi/schema-version`, `.madi/vault.yaml`, an empty generation-0
`.madi/policy-index.yaml`, `.madi/policies/`, and empty `projects/`, `sources/`,
`workstreams/`, `memory/`. Create local `bindings`, `overlays`, `evidence`,
`pending`, `views`, `index`, `cache`, `locks`, and `logs` outside the Vault. Store
typed owner `{type: person, id: owner_id}`. `StorageRouter` maps canonical families
to portable or local roots, while `ViewStore` maps only to `local_root/views`.
Resolver and context modules will receive read interfaces later and never receive
an `ArtifactStore`. Do not create a policy revision through a bootstrap-only file
shortcut; Task 10 creates and activates the default through the official API. Do
not create a database.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/test_vault.py tests/core/test_fs.py tests/core/test_storage.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core tests/core
git commit -m "feat: add portable Vault and local-state filesystem primitives"
```

---

### Task 9: Implement Privacy Policy Inheritance and Receipts

**Files:**
- Create: `mneme/core/policy.py`
- Create: `tests/core/test_policy.py`

**Interfaces:**
- Produces: `Portability.LOCAL_ONLY < PERSONAL_VAULT < SHAREABLE`
- Produces: `PolicyRef(policy_id: str, revision: str, digest: str)`
- Produces: `PolicyEvaluation(requested, effective_ceiling, allowed, evaluated_at, evaluator_version, refs, semantic_hash)`
- Produces: `evaluate_portability(requested: Portability, inputs: tuple[PolicyRule, ...], semantic_hash: str) -> PolicyEvaluation`

- [ ] **Step 1: Write ceiling and fail-closed tests**

```python
def test_agent_can_reduce_but_not_raise_source_ceiling():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    source = PolicyRule("source", "7", Portability.LOCAL_ONLY, "digest-source")
    denied = evaluate_portability(Portability.PERSONAL_VAULT, (source,), "body-hash")
    assert denied.allowed is False
    lowered = evaluate_portability(Portability.LOCAL_ONLY, (source,), "body-hash")
    assert lowered.allowed is True


def test_unknown_policy_fails_closed_for_portable_write():
    from mneme.core.policy import Portability, UnknownPolicy, evaluate_portability

    with pytest.raises(UnknownPolicy):
        evaluate_portability(Portability.PERSONAL_VAULT, (), "body-hash")
```

Add a test that a portable-safe opaque attestation can replace a confidential source ID and that later reevaluation reports non-compliance without mutating the original receipt.

- [ ] **Step 2: Verify policy tests fail**

Run: `python -m pytest tests/core/test_policy.py -v`

- [ ] **Step 3: Implement pure evaluation and immutable receipts**

Use the most restrictive ceiling and accept already-loaded immutable PolicyRule
values as inputs. Keep original receipt fields immutable. Return structured
violations; do not attempt Git-history deletion. Task 10 owns revision persistence,
active pointers, and retaining older referenced revisions.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/test_policy.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/policy.py tests/core/test_policy.py
git commit -m "feat: enforce inherited portability ceilings with receipts"
```

---

### Task 10: Add Immutable Policy Revision and Active-Pointer APIs

**Files:**
- Modify: `mneme/core/policy.py`
- Modify: `mneme/core/vault.py`
- Create: `tests/core/test_policy_store.py`

**Interfaces:**
- Produces: `PolicyStore.create_revision(policy_id, revision, rule, storage_class) -> PolicyRef`
- Produces: `PolicyStore.activate(policy_ref, expected_generation) -> PolicyIndex`
- Produces: `PolicyStore.load_active(policy_id, storage_class) -> PolicyRef`
- Produces: `PolicyStore.bootstrap_default() -> PolicyRef`, implemented only by composing create + activate

- [ ] **Step 1: Test immutable creation and active-revision CAS**

Create revision `1`, activate it at policy-index generation 0, create a stricter
revision `2`, and assert activation with stale generation 0 fails without changing
the active pointer. Activate revision `2` with the current generation and assert
revision `1` remains readable. A second write to either revision path, including
different content under the same revision name, must fail rather than overwrite.

Repeat creation and activation for `StorageClass.LOCAL_ONLY` and assert the same
policy schema is stored below the local overlay root. Assert a portable policy
index cannot reference a local-only revision.

Assert a newly initialized Vault obtains its default active revision only through
`PolicyStore.bootstrap_default`, whose observable writes are exactly one immutable
revision plus one generation-CAS activation. Re-running bootstrap is idempotent
only when the existing immutable content/digest and active pointer match.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/core/test_policy_store.py -v`

- [ ] **Step 3: Implement the official policy lifecycle path**

Create policy revision files only through `PolicyStore.create_revision`. Change an
active pointer only through `PolicyStore.activate` using policy-index generation
CAS. Reuse deterministic codecs and the storage router; never edit YAML through a
service or adapter shortcut. Revision creation and activation are separate
operations, so an interrupted activation leaves an unreferenced immutable revision
that `doctor` can report without attaching automatically.

Project/source assignment is the corresponding registry mutation and is added in
Task 11 after those registries exist. From Task 11 onward, tests and E2E scenarios
must use these APIs plus registry assignment APIs; direct YAML mutation is allowed
only in corruption/doctor fixtures.

Wire Vault initialization to `bootstrap_default`; it must not grow a second
private policy-write implementation.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/test_policy.py tests/core/test_policy_store.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/policy.py mneme/core/vault.py tests/core/test_policy_store.py
git commit -m "feat: add immutable policy lifecycle APIs"
```

---

### Task 11: Add Project, Source, and Workstream Registries with CAS

**Files:**
- Create: `mneme/core/registries.py`
- Create: `mneme/core/sources/registry.py`
- Create: `tests/core/test_registries.py`

**Interfaces:**
- Produces: `HeadRef(session: str, revision: str)`
- Produces: `WorkstreamRegistry(id, generation, project, status, mode, active_heads, preferred_head)`
- Produces: `RegistryStore.create_workstream`, `load_workstream`, `update_workstream`
- Produces: `add_active_head(head, observed_base, head_validator) -> WorkstreamRegistry`
- Produces: `register_project`, `register_source`, `bind_source`
- Produces: `assign_project_policy(project_id, policy_ref, expected_generation)`
- Produces: `assign_source_policy(source_id, policy_ref, expected_generation)`

- [ ] **Step 1: Test CAS and authority boundaries**

```python
def test_workstream_update_requires_expected_generation(vault):
    from mneme.core.registries import RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    ws = store.create_workstream("ws-1", project=None, mode="single")
    changed = store.update_workstream(ws.with_status("paused"), expected_generation=0)
    assert changed.generation == 1
    with pytest.raises(RegistryConflict):
        store.update_workstream(changed.with_status("active"), expected_generation=0)
```

Add tests that portable project/source registries reject absolute paths and
credential-like values, a local binding accepts an absolute path outside the
Vault, and a confidential-only project creates no portable stub. Construct both
portable and local-only Registry stores through `StorageRouter` and assert they
use the same codec/schema.

Create policy revisions through `PolicyStore.create_revision`; assign them with
`assign_project_policy` and `assign_source_policy`. Each assignment validates that
the referenced immutable revision exists, rejects portable-to-local references,
and CAS-updates only the subject registry. Assert stale assignment generations
fail and no test changes registry YAML directly.

Add a two-writer test from the same registry generation: writer A adds head
`session-a/000001`, then writer B adds the distinct valid head
`session-b/000001`. `add_active_head` must reload and structurally retry so the
final registry contains both. Add negative cases for the same session with a
different revision, a concurrent preferred-head change, a removal, lifecycle or
policy change; each remains an explicit `RegistryConflict`. Use an injected fake
head validator in this registry unit test; Task 12 supplies the real immutable
Session reader/validator once that artifact exists.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/core/test_registries.py -v`

- [ ] **Step 3: Implement bounded registries and the proven-disjoint retry**

Serialize only IDs, lifecycle, explicit resolution mode, active/preferred heads,
safe locator, authority, and policy refs. Project registries describe personal
association, never project facts. Source binding is written only under local
state. Every store is created with an explicit artifact location.

`add_active_head` performs a bounded reload + CAS retry (maximum three CAS
attempts) only when all of these are proven from the caller's observed base and
the reloaded registry:

1. the requested mutation is a pure addition of one distinct session head;
2. registry identity, project, status, resolution mode, preferred head, and policy
   references are unchanged;
3. existing heads were only added, never removed or changed;
4. the requested session has no different head; and
5. the referenced immutable Session revision exists and validates.

On each retry, CAS against the newly loaded generation. Exhaustion or any failed
predicate returns a structured conflict. This is a structural set-union of proven
disjoint references, not semantic merging or last-write-wins. The Registry layer
does not invent or parse Session semantics; it requires the caller-supplied head
validator.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/test_registries.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/registries.py mneme/core/sources/registry.py tests/core/test_registries.py
git commit -m "feat: add explicit CAS-protected Vault registries"
```

---

### Task 12: Persist Immutable Session Revisions as Checkpoints

**Files:**
- Create: `mneme/core/sessions.py`
- Create: `tests/core/test_sessions.py`

**Interfaces:**
- Produces: `CheckpointRequest(workstream_id, session_id, storage_class, expected_parent, expected_registry_generation, body, relations, policy_evaluation)`
- Produces: `SessionRevisionRef(session: str, revision: str)`
- Produces: `SessionStore.create_revision(request: CheckpointRequest) -> SessionRevisionRef`

- [ ] **Step 1: Test write-before-head and immutability**

```python
def test_checkpoint_writes_revision_then_advances_head(vault, valid_checkpoint):
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    ref = store.create_revision(valid_checkpoint("ws-1", "ses-1"))
    assert ref.revision == "000001"
    assert (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()
    assert store.registries.load_workstream("ws-1").active_heads == (ref.as_head(),)
```

Add tests that a second write to `000001.md` fails, a true registry conflict leaves
an orphan revision without changing the head, revision 2 is self-contained, and
multiple handoff relations do not terminate the source session. Test portable and
local-only revisions through the same Session codec/store with explicit storage
class, and reject a portable revision containing a local-only relation or
provenance reference before any file is created.

Add an integrated checkpoint race: two writers create revisions in different
session directories from the same workstream generation. The second head update
must use Task 11's proven-disjoint retry, leaving both valid heads registered and
neither revision orphaned. A same-session lineage race and a concurrent preferred
head change still produce an explicit orphan/conflict for doctor review.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/core/test_sessions.py -v`

- [ ] **Step 3: Implement checkpoint ordering**

Validate the host-composed body and one-way reference invariant, write `000001.md`
with exclusive creation through `ArtifactStore`, then call
`RegistryStore.add_active_head` with the real Session revision reader as its head
validator. Include session/workstream/adapter identity,
objective, current state, verified facts, completed work, blockers, next actions,
source refs, relations, semantic hash, and policy receipt. Never call Git or a
generation provider.

- [ ] **Step 4: Run focused and full tests**

```powershell
python -m pytest tests/core/test_sessions.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/sessions.py tests/core/test_sessions.py
git commit -m "feat: persist immutable session revisions as checkpoints"
```

---

### Task 13: Resolve Workstreams and Compose Portable/Local CURRENT

**Files:**
- Create: `mneme/core/resolver.py`
- Create: `mneme/core/context.py`
- Create: `tests/core/test_resolver.py`
- Create: `tests/core/test_current_overlay.py`

**Interfaces:**
- Produces: `ResolutionState` values `resolved`, `divergent`, `degraded`, `invalid`
- Produces: `resolve_workstream(registry, load_revision) -> ResolvedWorkstream`
- Produces: `render_current(readers, workstream_id: str, mode: Literal["portable", "effective-local"]) -> ContextView`

- [ ] **Step 1: Encode the resolution truth table**

```python
@pytest.mark.parametrize(("mode", "heads", "preferred", "expected"), [
    ("single", ["a"], None, "resolved"),
    ("preferred", ["a", "b"], "b", "resolved"),
    ("parallel", ["a", "b"], None, "divergent"),
    ("preferred", ["a", "b"], None, "invalid"),
    ("single", [], None, "invalid"),
])
def test_resolution_states(registry_factory, mode, heads, preferred, expected):
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory(mode, heads, preferred)
    loaded = {head: object() for head in heads}
    assert resolve_workstream(registry, loaded.get).state.value == expected
```

Add tests for valid closed-empty, missing optional mount (`degraded`), missing active revision (`invalid`), and deterministic head ordering.

- [ ] **Step 2: Test one-way overlay composition**

Create a portable revision plus a confidential local overlay. Assert portable mode contains neither local text nor local ID/hash/count; effective-local mode labels portable base and local foreground separately; an invalid optional overlay falls back to portable with `degraded`; and a local preferred head never writes the portable registry.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_resolver.py tests/core/test_current_overlay.py -v`

- [ ] **Step 4: Implement deterministic resolution and generated views**

Use only registry-declared heads. Never infer from timestamps or all sessions.
Resolver/context receive read interfaces only and return a `ContextView`; they do
not own any artifact or view write. A separate `ViewStore` may persist the returned
projection only to `local_root/views/CURRENT.md`. Portable rendering never reads
overlay paths. Effective-local output exposes `portable_status`, `overlay_status`,
and `effective_status` exactly as D3 section 7.1 defines.

- [ ] **Step 5: Run focused/full tests and commit**

```powershell
python -m pytest tests/core/test_resolver.py tests/core/test_current_overlay.py -v
python -m pytest -q
git add mneme/core/resolver.py mneme/core/context.py tests/core
git commit -m "feat: resolve explicit heads into generated CURRENT views"
```

---

### Task 14: Add Candidate and Immutable Accepted Memory Lifecycles

**Files:**
- Create: `mneme/core/memories.py`
- Create: `tests/core/test_memories.py`
- Create: `tests/core/test_profile.py`

**Interfaces:**
- Produces independent: `MemoryKind`, `MemoryStatus`, `MemoryScope`, `MemoryAuthority`, `Portability`
- Produces: `MemoryStore.submit_candidate`, `edit_candidate`, `promote`, `supersede`, `retire`
- Produces: `render_profile(readers, scope) -> ContextView`

- [ ] **Step 1: Test independent axes and candidate portability**

```python
def test_portable_candidate_is_not_implicitly_accepted(memory_store, receipt):
    record = memory_store.submit_candidate(
        kind="knowledge",
        scope={"type": "personal-global"},
        authority="personal",
        portability="personal-vault",
        body="FTS first remains useful without a model.",
        receipt=receipt,
    )
    assert record.status.value == "candidate"
    assert (memory_store.vault.root / f"memory/{record.id}.md").exists()
```

Add tests for local-only accepted memory through the same schema/codec, candidate
CAS editing, promotion freezing semantic hash, semantic edit rejection after
acceptance, a new ID with `supersedes`, derived reverse link,
retirement-envelope mutation, and authorized privacy deletion not claiming Git
erasure. A portable candidate or accepted memory that references a local-only
artifact must fail before creation; the corresponding local-to-portable reference
is valid.

- [ ] **Step 2: Test generated PROFILE**

Create accepted preference, lesson, and retired preference records. Assert PROFILE
contains only the active accepted preference with its ID and shows unresolved
conflicting preferences rather than choosing one. `render_profile` returns a view
without writing; only `ViewStore` may persist it under local views.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_memories.py tests/core/test_profile.py -v`

- [ ] **Step 4: Implement lifecycle and projection**

Freeze `kind`, body, rationale, scope, authority, portability, provenance, original policy receipt, and semantic hash at acceptance. Permit CAS changes only to status, retired timestamp, and reason. A substantive change calls `supersede(old_id, candidate)` and writes a new file; do not require a backlink mutation on the old file.

- [ ] **Step 5: Run focused/full tests and commit**

```powershell
python -m pytest tests/core/test_memories.py tests/core/test_profile.py -v
python -m pytest -q
git add mneme/core/memories.py tests/core
git commit -m "feat: add candidate and immutable accepted memory records"
```

---

### Task 15: Register Read-Only Project Sources and Build Rebuildable Recall

**Files:**
- Create: `mneme/core/search/index.py`
- Create: `mneme/core/service.py`
- Create: `tests/core/test_sources.py`
- Create: `tests/core/test_recall.py`
- Create: `tests/core/test_reindex.py`

**Interfaces:**
- Produces: `GeneratedIndex(db_path: Path).rebuild(vault, bound_sources) -> IndexReport`
- Produces: `GeneratedIndex.search(query: str, limit: int, scope=None) -> list[RecallHit]`
- Produces: `CoreService.recall`, `context`, `reindex`

- [ ] **Step 1: Test source authority and no-write API**

Register a project source, bind it to a temporary checkout, index its Markdown, and assert every hit carries source ID, path, `authority="external-reference"`, policy status, and excerpt. Assert Core has no API that writes to the mounted project.

- [ ] **Step 2: Test DB deletion and no-LLM recall**

```python
def test_generated_db_can_be_deleted_and_rebuilt(vault_with_memory, monkeypatch):
    import sys
    from mneme.core.search.index import GeneratedIndex

    monkeypatch.setitem(sys.modules, "mneme.llm", None)
    index = GeneratedIndex(vault_with_memory.local_root / "index/state.db")
    index.rebuild(vault_with_memory, ())
    first = index.search("checkpoint", 10)
    index.db_path.unlink()
    index.rebuild(vault_with_memory, ())
    assert index.search("checkpoint", 10) == first
```

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_sources.py tests/core/test_recall.py tests/core/test_reindex.py -v`

- [ ] **Step 4: Implement deterministic indexing and progressive disclosure**

Index Registry metadata, Session revisions, Memory records, and available source
chunks in separate FTS rows. Store content hash, authority, portability, policy
status, source ID/path, artifact ID/revision, and excerpt offsets. Those stored
policy fields are diagnostic/cache hints only; `GeneratedIndex.search` returns
candidate hits, and `CoreService.recall` authorizes them against current canonical
artifacts and policy pointers before exposure. Default recall is FTS/BM25 with
Korean expansion. Missing mounts produce diagnostics and `degraded`, not failed
startup. Task 19 locks the no-reindex tightening case across every consumer.

- [ ] **Step 5: Run focused/full tests and commit**

```powershell
python -m pytest tests/core/test_sources.py tests/core/test_recall.py tests/core/test_reindex.py -v
python -m pytest -q
git add mneme/core/search/index.py mneme/core/service.py tests/core
git commit -m "feat: add rebuildable FTS recall over Vault and mounted sources"
```

---

### Task 16: Add Deterministic Doctor and Safe Repair Boundaries

**Files:**
- Create: `mneme/core/doctor.py`
- Create: `tests/core/test_doctor.py`

**Interfaces:**
- Produces: `Doctor.run() -> DoctorReport`

- [ ] **Step 1: Test doctor classifications and non-destructive repair**

Test orphan revision reporting, missing active head as `invalid`, missing optional source as `degraded`, stale policy reevaluation, generated DB absence as rebuildable, and portable-to-local reference rejection. `doctor` may regenerate index/views; it may not attach or delete semantic artifacts.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/core/test_doctor.py -v`

- [ ] **Step 3: Implement read-only diagnosis and bounded repairs**

Return issues with stable code, severity, artifact reference, and portable-safe detail. Repair actions are limited to rebuilding generated index/views and removing stale local locks. Semantic orphan attachment, artifact deletion, head choice, policy reclassification, and conflict merge require an explicit later command.

- [ ] **Step 4: Implement and verify**

```powershell
python -m pytest tests/core/test_doctor.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/doctor.py tests/core/test_doctor.py
git commit -m "feat: add deterministic Vault doctor"
```

---

### Task 17: Add the Provisional Vault CLI Without Renaming the Package

**Files:**
- Create: `mneme/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces provisional command: `python -m mneme.cli {status,doctor,context,recall,remember,checkpoint,project,reindex}`

- [ ] **Step 1: Test CLI envelopes**

Run handlers in-process. Success writes JSON to stdout and exits 0; schema/policy/conflict errors write structured JSON to stderr and exit 2; unavailable optional source returns `degraded` context and exit 0. Verify checkpoint accepts a host-composed UTF-8 payload file and remember requires explicit kind/scope/authority/portability.

- [ ] **Step 2: Test migration-safe naming**

Keep the existing `mneme` script bound to `mneme.server:main`. Add only `mneme-vault = "mneme.cli:main"`; assert package metadata still says `mneme-mcp` and no `madi` package exists.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/test_cli.py -v`

- [ ] **Step 4: Implement and verify**

```powershell
python -m pytest tests/test_cli.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/cli.py pyproject.toml tests/test_cli.py
git commit -m "feat: add provisional Vault CLI beside legacy server"
```

---

### Task 18: Add Explicit Git Sync Primitives

**Files:**
- Create: `mneme/core/git_sync.py`
- Modify: `mneme/cli.py`
- Create: `tests/core/test_git_sync.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `inspect_sync`, `sync_preflight`, `fetch`, `commit_paths`, `fast_forward`, `push`
- Produces CLI: `python -m mneme.cli sync {status,fetch,commit,fast-forward,push}`

- [ ] **Step 1: Test refusal paths**

Initialize local Git repositories under `tmp_path`. Assert `fast_forward` refuses
dirty state and divergence, `commit_paths` includes only explicit
doctor-and-policy-validated Vault paths, `push` has no force mode, and checkpoint
never invokes a Git function.

- [ ] **Step 2: Test checkpoint/commit separation**

Create a checkpoint without a Git repository and assert success. Then initialize Git and explicitly call `commit_paths`; assert the commit contains the revision and registry but excludes local state. No `commit_on_checkpoint` setting is accepted.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_git_sync.py -v`

- [ ] **Step 4: Implement and verify**

Use argument-list `subprocess.run` calls, never shell strings. Fetch before
fast-forward, refuse merge/rebase/force automatically, return structured
divergence/conflict status, and allow commit/push only after doctor and a live
policy-aware sync preflight succeed. Task 19 adds the stale-index security
regression once every generated consumer exists.

```powershell
python -m pytest tests/core/test_git_sync.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/core/git_sync.py mneme/cli.py tests/core/test_git_sync.py tests/test_cli.py
git commit -m "feat: add explicit non-merging Git sync primitives"
```

---

### Task 19: Enforce Current Policy Independently of Stale Generated State

**Files:**
- Create: `mneme/core/security.py`
- Modify: `mneme/core/service.py`
- Modify: `mneme/core/context.py`
- Modify: `mneme/core/memories.py`
- Modify: `mneme/core/git_sync.py`
- Modify: `mneme/core/storage.py`
- Create: `tests/core/test_policy_staleness.py`

**Interfaces:**
- Produces: `PolicyAuthorizer.authorize_current(artifact_ref, operation) -> PolicyDecision`
- Produces: live authorization gates for `recall`, `context`, `profile`, `export`, and `sync_preflight`
- Treats: index rows and generated-view policy status as non-authoritative cache metadata

- [ ] **Step 1: Write the no-reindex tightening regression**

Using only the official APIs from Tasks 10 and 11:

1. create and activate a policy revision permitting `personal-vault`;
2. assign it to a source and project;
3. create a portable Session revision and accepted preference Memory derived from
   that source;
4. build FTS, CURRENT, and PROFILE while the artifacts are allowed;
5. create and activate a stricter local-only revision and CAS-assign it to the
   source/project;
6. deliberately do **not** reindex and do not manually edit any YAML.

Assert that `CoreService.recall`, portable CURRENT, PROFILE, export, and sync
preflight expose none of the now-disallowed body or provenance. Sync preflight
blocks commit/push of the affected current-tree artifact and emits only a
local-safe remediation code. The artifact and its original admission receipt
remain physically present; the result repeats D3's no-retroactive-erasure limit.

- [ ] **Step 2: Prove caches cannot grant authority**

Seed or tamper a generated FTS row and cached view metadata to say `allowed` under
the older policy generation. Assert the live result is still denied. Conversely,
a stale generated `denied` value cannot replace a current live allow decision;
Core may request rebuild but must evaluate canonical policy itself. Direct YAML
edits are used only in this cache-corruption fixture, never to tighten the source
policy.

Assert `ViewStore.load` refuses a CURRENT/PROFILE projection whose recorded live
authorization-input fingerprint differs from current canonical vault policy,
project/source policy assignments, or their immutable revision digests. Checking
only the global policy-index generation is insufficient because a project/source
assignment can change independently. The service rerenders through read-only
context/profile functions; it never asks the resolver to mutate canonical state.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_policy_staleness.py -v`

- [ ] **Step 4: Implement one live authorization gate**

Resolve each candidate artifact's current vault/project/source policy pointers
from Registry/Policy stores, compare them with the immutable write receipt, and
authorize the requested operation. All outward paths call this same gate after an
index lookup and before rendering or Git action. Unknown, missing, or unreadable
current policy fails closed. Keep remediation details local-only and do not delete
or rewrite Git history.

- [ ] **Step 5: Run focused and full tests, then commit**

```powershell
python -m pytest tests/core/test_policy.py tests/core/test_policy_store.py tests/core/test_policy_staleness.py -v
python -m pytest -q
git add mneme/core tests/core/test_policy_staleness.py
git commit -m "feat: enforce live policy over stale generated state"
```

---

### Task 20: Separate Lifecycle Events, Core Commands, and MCP Transport

**Files:**
- Create: `mneme/core/contracts.py`
- Create: `mneme/transports/mcp_stdio.py`
- Create: `tests/core/test_contracts.py`
- Create: `tests/transports/test_mcp_stdio.py`

**Interfaces:**
- Produces: `LifecycleEvent`, `CoreCommand`, `DomainEvent`, `OperationalEvent`
- Produces: `CoreService.execute(command: CoreCommand) -> CommandResult`
- Produces MCP tools: `madi_context`, `madi_recall`, `madi_remember`, `madi_checkpoint`, `madi_decision`

Lifecycle names are `session_started`, `session_resumed`, `pre_compact`,
`milestone_reached`, `session_ended`, and `agent_switched`. Command names are
`get_context`, `open_session`, `create_session_revision`, `submit_memory`,
`promote_memory`, `retire_memory`, `set_active_heads`, `set_preferred_head`, and
`register_source`; authorized administrative commands are `create_policy_revision`,
`activate_policy_revision`, `assign_project_policy`, and `assign_source_policy`.
Successful mutations return the corresponding D3 Domain event;
source/index/sync availability produces an Operational event only.

- [ ] **Step 1: Test vocabulary separation**

```python
def test_lifecycle_event_cannot_mutate_core(service):
    from mneme.core.contracts import LifecycleEvent

    event = LifecycleEvent(
        version=1, name="pre_compact", adapter="codex", session_id="s1",
    )
    result = service.observe(event)
    assert result.checkpoint_required is True
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []
```

Add tests that `create_session_revision` returns `session_revision_created`,
operational `source_unavailable` is local-only, and no Domain event log becomes a
fourth canonical artifact. Assert policy lifecycle commands delegate only to the
Task 10/11 APIs, preserve their authorization/CAS failures, and cannot accept a
raw filesystem path as an update shortcut.

- [ ] **Step 2: Test stdio tools by direct function call**

Construct the FastMCP app around an injected `CoreService`; call each registered handler directly and assert structured envelopes. Importing `mneme.core` with `fastmcp` blocked must still work.

- [ ] **Step 3: Verify tests fail**

Run: `python -m pytest tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -v`

- [ ] **Step 4: Implement transport-only registration**

Lifecycle observations never write canonical state. Only explicit commands do. `mcp_stdio.main()` runs on-demand stdio and does not initialize the legacy DB, reindex at startup, start watcher/scheduler, or bind an HTTP port. Keep `legacy_http` untouched.

- [ ] **Step 5: Run full tests and commit**

```powershell
python -m pytest tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -v
python -m pytest -q
git add mneme/core/contracts.py mneme/transports/mcp_stdio.py tests
git commit -m "feat: add agent-neutral Core contract and stdio MCP transport"
```

---

### Task 21: Add a Non-Blocking Claude Adapter in Parallel with Legacy Integration

**Files:**
- Create: `mneme/adapters/__init__.py`
- Create: `mneme/adapters/base.py`
- Create: `mneme/adapters/claude.py`
- Create: `integration/madi/claude/CLAUDE.md.template`
- Create: `integration/madi/claude/mcp-stdio.json`
- Create: `integration/madi/install_claude.py`
- Create: `tests/adapters/test_claude.py`
- Create: `tests/integration/test_install_claude.py`

**Interfaces:**
- Produces adapter envelope: `{version, event, adapter, session_id, workstream_id, checkpoint_file?}`
- Produces: `ClaudeAdapter.handle(envelope) -> AdapterResult`
- Produces: idempotent opt-in installer; does not modify current `integration/install.py`

- [ ] **Step 1: Test adapter failure is non-blocking**

```python
def test_claude_adapter_failure_warns_without_blocking(unavailable_service):
    from mneme.adapters.claude import ClaudeAdapter

    result = ClaudeAdapter(unavailable_service).handle({
        "version": 1,
        "event": "pre_compact",
        "adapter": "claude",
        "session_id": "claude-1",
        "workstream_id": "ws-1",
    })
    assert result.ok is False
    assert result.block_host is False
    assert result.warning
```

- [ ] **Step 2: Test semantic payload ownership**

A bare lifecycle envelope returns `checkpoint_required` and writes nothing. Supplying a host-composed checkpoint file issues `create_session_revision`; the adapter does not summarize transcript or tool output.

- [ ] **Step 3: Test installer idempotence**

In a temporary project, assert existing `.mcp.json` servers and `CLAUDE.md` content are preserved, Madi stdio config/rules are added once, and no absolute Vault path or credential is written into the project.

- [ ] **Step 4: Implement and verify**

```powershell
python -m pytest tests/adapters/test_claude.py tests/integration/test_install_claude.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/adapters integration/madi/claude integration/madi/install_claude.py tests
git commit -m "feat: add opt-in non-blocking Claude adapter"
```

---

### Task 22: Bind the Official Codex PreCompact Schema to the Agent-Neutral Contract

**Files:**
- Create: `mneme/adapters/codex.py`
- Create: `tests/fixtures/codex/pre_compact.native.json`
- Create: `tests/fixtures/codex/pre_compact.normalized.json`
- Create: `integration/madi/codex/AGENTS.md.template`
- Create: `integration/madi/codex/hooks.json`
- Create: `integration/madi/codex/pre_compact_hook.py`
- Create: `integration/madi/codex/README.md`
- Create: `integration/madi/install_codex.py`
- Create: `tests/adapters/test_codex.py`
- Create: `tests/integration/test_install_codex.py`

**Interfaces:**
- Produces: `CodexAdapter.handle(envelope) -> AdapterResult`
- Produces: `translate_pre_compact(native_payload, local_binding) -> LifecycleEvent`
- Consumes: the same versioned adapter envelope and Core commands as Claude

- [ ] **Step 1: Pin the official native contract and its provenance**

Treat the current official `PreCompact` command-hook payload as a supported native
contract, not a discovery condition. Record all of the following in
`integration/madi/codex/README.md`:

- adapter schema ID: `openai-codex-hooks/pre-compact@2026-08-26+a26f1806`;
- release-behavior source, checked 2026-08-26:
  `https://learn.chatgpt.com/docs/hooks`;
- generated input schema pinned to OpenAI Codex commit
  `a26f1806a4f4b8cfec2ea1be129963815a61e58c`:
  `https://github.com/openai/codex/blob/a26f1806a4f4b8cfec2ea1be129963815a61e58c/codex-rs/hooks/schema/generated/pre-compact.command.input.schema.json`;
- the official warning that the release documentation is behavioral authority and
  `main` schemas may contain unreleased fields.

The native fixture contains the seven required fields from that contract:
`session_id: string`, `transcript_path: string|null`, `cwd: string`,
`hook_event_name: "PreCompact"`, `model: string`, `turn_id: string`, and
`trigger: "manual"|"auto"`. The pinned generated schema also permits optional
`agent_id` and `agent_type`; the translator may ignore them. Do not invent a
workstream field: resolve session/workstream association only from machine-local
adapter bindings.

- [ ] **Step 2: Test native-to-normalized translation**

Load `pre_compact.native.json` and assert translation yields the normalized
`LifecycleEvent(version=1, name="pre_compact", adapter="codex", session_id=...)`
plus non-canonical trigger/turn metadata. Assert `transcript_path`, `cwd`, `model`,
and optional agent fields never enter a canonical command, Session body, Memory,
or portable Registry. Missing required fields, the wrong event name, and an
unknown trigger return a structured non-blocking adapter error.

- [ ] **Step 3: Test normalized behavior against Core**

```python
def test_codex_precompact_uses_same_core_command(service, checkpoint_file):
    from mneme.adapters.codex import CodexAdapter

    result = CodexAdapter(service).handle({
        "version": 1,
        "event": "pre_compact",
        "adapter": "codex",
        "session_id": "codex-1",
        "workstream_id": "ws-1",
        "checkpoint_file": str(checkpoint_file),
    })
    assert result.ok is True
    assert result.domain_events[0].name == "session_revision_created"
```

Add agent-switch coverage: Codex creates its own session with `continues_from` the
Claude revision and changes `preferred_head` only through an explicit Core
command. A lifecycle event without host-composed semantic checkpoint material
requests a checkpoint and writes no canonical artifact.

- [ ] **Step 4: Test the native hook runner, installer, and fallback**

Feed the native fixture to the hook runner over stdin and assert it invokes the
translator. Assert `hooks.json` registers `PreCompact` for `manual|auto`, and the
installer preserves existing hooks/config while adding Madi once. `AGENTS.md`
instructions identify Madi as optional, use no credential or absolute Vault path,
and tell Codex to continue ordinary work if checkpoint fails. Core/unavailable or
invalid payload returns warning diagnostics without blocking normal Codex work.

- [ ] **Step 5: Implement and verify**

```powershell
python -m pytest tests/adapters/test_codex.py tests/integration/test_install_codex.py -v
python -m pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add mneme/adapters/codex.py integration/madi/codex integration/madi/install_codex.py tests
git commit -m "feat: translate official Codex PreCompact hooks"
```

---

### Task 23: Characterize the Complete Legacy Growth Surface

**Files:**
- Create: `tests/characterization/test_legacy_growth.py`

**Interfaces:**
- Preserves: all current Growth tool/module signatures
- Produces: executable characterization evidence before any Growth file moves

- [ ] **Step 1: Characterize Growth before moving**

Add deterministic tests for `cib.clip`, seed-protected negative reward rejection,
Outer Loop insufficient-episode gate, `self_model.assess` no-LLM graceful path,
Growth DB/log shapes, scheduler tick order, notify failure behavior, and legacy MCP
Growth response shapes. Stub the generation provider and network notifier, and
isolate `DB_PATH`.

- [ ] **Step 2: Run characterization before edits**

Run: `python -m pytest tests/characterization/test_legacy_growth.py -v`

Expected: all pass on the current top-level modules.

- [ ] **Step 3: Run the full unchanged suite**

Run: `python -m pytest -q`

Expected: the new tests describe current behavior without any production change.

- [ ] **Step 4: Commit characterization only**

```powershell
git add tests/characterization/test_legacy_growth.py
git commit -m "test: characterize legacy Growth behavior"
```

---

### Task 24: Move Pure Growth Dependencies Behind Compatibility Facades

**Files:**
- Create: `mneme/growth_lab/__init__.py`
- Move: `mneme/cib.py`, `constitution.py`, `constitution.yaml`, `skills.py`, `outer_loop.py`, `self_model.py`, `growth.py`, `log.py` under `mneme/growth_lab/`
- Create: compatibility facades at each moved top-level module path
- Create: `tests/growth_lab/test_facades.py`
- Create: `tests/core/test_growth_optional.py`

**Interfaces:**
- Preserves: current pure Growth functions, database seams, resources, and import paths
- Produces: import boundary where `mneme.core` succeeds with Growth modules blocked

- [ ] **Step 1: Test the move boundary before moving**

Add tests that old and new import paths expose the same objects/signatures,
top-level monkeypatches still affect runtime lookup, `constitution.yaml` resolves
relative to the new package, and importing Core with every `mneme.growth_lab.*`
module blocked succeeds. Do not include scheduler, notifier, or legacy-server
wiring in this task.

- [ ] **Step 2: Verify the focused test fails**

Run: `python -m pytest tests/growth_lab/test_facades.py tests/core/test_growth_optional.py -v`

- [ ] **Step 3: Move the pure dependency closure and add facades**

Move the listed modules and update intra-Growth imports to
`mneme.growth_lab.*`. Keep `scheduler.py` and `notify.py` at their top-level paths
for Task 25; when `outer_loop` needs notify, retain that compatibility import until
the integration task. Use module-object aliases for monkeypatch-sensitive facades.
Do not modify `mneme/transports/legacy_http.py` here.

- [ ] **Step 4: Verify characterization and optionality**

```powershell
python -m pytest tests/characterization/test_legacy_growth.py tests/growth_lab/test_facades.py tests/core/test_growth_optional.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/growth_lab mneme/cib.py mneme/constitution.py mneme/skills.py mneme/outer_loop.py mneme/self_model.py mneme/growth.py mneme/log.py tests
git commit -m "refactor: move pure Growth dependencies behind facades"
```

---

### Task 25: Isolate Scheduler, Notify, and Legacy Growth Integration

**Files:**
- Move: `mneme/scheduler.py`, `mneme/notify.py` under `mneme/growth_lab/`
- Create: compatibility facades at `mneme/scheduler.py` and `mneme/notify.py`
- Modify: `mneme/growth_lab/outer_loop.py`
- Modify: `mneme/transports/legacy_http.py`
- Create: `tests/growth_lab/test_legacy_integration.py`

**Interfaces:**
- Preserves: scheduler/notify functions, lifecycle order, and legacy MCP Growth response shapes
- Ensures: scheduler, notifier, and legacy HTTP imports do not enter Madi Core/stdio paths

- [ ] **Step 1: Add focused integration tests**

Reuse Task 23 characterization fixtures to assert scheduler start/stop and tick
order, notify success/failure with network stubs, and every legacy HTTP Growth tool
response. Assert importing/running Core and stdio MCP with scheduler, notify,
FastMCP legacy server, and Growth modules blocked still succeeds.

- [ ] **Step 2: Verify the new package-path tests fail**

Run: `python -m pytest tests/growth_lab/test_legacy_integration.py -v`

- [ ] **Step 3: Move only integration dependencies**

Move scheduler and notify, update Growth internal imports and the legacy HTTP
transport, and leave top-level compatibility facades. Preserve watcher and legacy
HTTP startup order. Growth/scheduler/notify failure remains confined to the legacy
or optional Growth surface and never prevents Core recall/context.

- [ ] **Step 4: Verify characterization and full compatibility**

```powershell
python -m pytest tests/characterization/test_legacy_growth.py tests/growth_lab tests/core/test_growth_optional.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/growth_lab mneme/scheduler.py mneme/notify.py mneme/transports/legacy_http.py tests
git commit -m "refactor: isolate optional Growth runtime integration"
```

---

### Task 26: Stage Legacy Wiki and SQLite Data Without Promotion or Deletion

**Files:**
- Modify: `mneme/migration/legacy.py`
- Modify: `mneme/cli.py`
- Create: `tests/migration/test_stage_export.py`
- Create: `docs/MIGRATION-MNEME-TO-MADI.md`

**Interfaces:**
- Produces: `stage_legacy(db_path, wiki_path, vault, local_pending) -> MigrationReport`
- Produces CLI: `python -m mneme.cli migrate inspect` and `migrate stage`
- Produces: local-only `pending/legacy/<db-sha256>/` lossless staging bundle
- Does not produce accepted portable memories

- [ ] **Step 1: Test byte-for-byte preservation**

Create a closed legacy DB containing rows in every table and an external Wiki. Run
`stage_legacy`; assert DB and Wiki file hashes are unchanged, no table is dropped,
and no source file is moved. Assert local
`pending/legacy/<db-sha256>/source/state.db` is byte-for-byte identical to the
original and the manifest records both hashes. The original path remains the
legacy runtime authority and is never replaced by the staged copy.

Include NULL, INTEGER, REAL, TEXT, and BLOB episode values. In addition to the
byte-exact DB snapshot, assert the review export preserves every table/column,
SQLite storage class, value (BLOB as tagged base64), schema SQL, row count, and a
deterministic row locator/ordinal. Compare per-table typed-export checksums. If the
source hash changes during snapshot, staging fails without publishing a partial
bundle.

- [ ] **Step 2: Test classification destinations**

Assert:

- Wiki becomes a read-only source registry plus a machine-local binding, not copied project truth.
- `facts` are `candidate-durable-local` review inputs under the local staging bundle, never accepted memory.
- `episodes` are `candidate-durable-local`/`needs-review`; every original field and payload is preserved in the local-only snapshot and typed review export, never reduced to metadata-only and never put in Git automatically.
- skills/loops/self-model/growth-actions remain a local Growth Lab export.
- generated tables are reported rebuildable and not exported as durable Markdown.
- unknown tables are `needs-review`: they are included losslessly in the local DB snapshot and typed export, then block any portable registration/promotion with a fail-closed report.

All files in this bundle live below `MADI_STATE_HOME`, not the Vault, and are
treated as potentially confidential. No row count, table name, hash, local ID, or
bundle existence leaks into a portable artifact unless a later policy-authorized
human action explicitly supplies a portable-safe value.

- [ ] **Step 3: Test promotion remains separate**

Calling `migrate stage` cannot create `memory/*.md` or Session revisions. A later
human/host-reviewed `remember` command supplies sanitized content and a fresh
policy evaluation. Candidate classification is not acceptance and never raises
portability. Wiki source registration, if authorized, uses the Task 10/11 policy
and source APIs rather than direct YAML edits.

- [ ] **Step 4: Implement and verify**

```powershell
python -m pytest tests/migration/test_inventory.py tests/migration/test_stage_export.py -v
python -m pytest -q
```

- [ ] **Step 5: Commit**

```powershell
git add mneme/migration/legacy.py mneme/cli.py tests/migration docs/MIGRATION-MNEME-TO-MADI.md
git commit -m "feat: stage legacy Mneme data for reviewed Madi migration"
```

---

### Task 27: Prove Clean Clone, Concurrency, Compact, Privacy, and Compatibility

**Files:**
- Create: `tests/e2e/test_clean_clone.py`
- Create: `tests/e2e/test_long_lived_session.py`
- Create: `tests/e2e/test_concurrent_agents.py`
- Create: `tests/e2e/test_privacy_boundary.py`
- Create: `tests/e2e/test_legacy_coexistence.py`
- Create: `tests/fixtures/vault-clean/`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/INTEGRATION.md`
- Modify: `docs/SETUP-NEW-PC.md`
- Modify: `PROGRESS.md`

**Interfaces:**
- Produces: release-gate evidence for D3 invariants
- Preserves: legacy HTTP MCP, Wiki, DB, watcher, scheduler, and Growth paths

- [ ] **Step 1: Test clean and shallow clone reconstruction**

Create a Vault with policy revisions, an accepted memory plus superseding record, and three Session revisions. Commit to a temporary bare remote, clone with `--depth 1` using a file URI, delete generated state, run doctor/reindex/context, and assert current-tree semantics are identical. Assert the source machine's local overlay is absent.

- [ ] **Step 2: Test the long-lived session sequence**

Simulate Claude revisions `000001` through `000004` across three PreCompact commands; create a handoff relation; start Codex with `continues_from`; explicitly prefer Codex; sync/clone; resume on a second machine. Assert no SessionEnd is required and no checkpoint depends on Git history.

- [ ] **Step 3: Test concurrency and conflict behavior**

Create two agents in separate session directories from the same registry
generation. Checkpoint both and assert the second disjoint head addition takes the
bounded reload/CAS path: both immutable revisions are active and neither is an
orphan. With `parallel` mode and no preferred head, assert `divergent`, not
invalid. Race two preferred-head CAS updates and assert one explicit conflict.
Race the same Session lineage/revision and assert no overwrite. Verify the retry
rejects removals and non-head-field changes and that no semantic auto-merge path
exists.

- [ ] **Step 4: Test privacy and unavailable Madi**

Seed local evidence with a credential, customer identifier, internal URL, source
snippet, and log. Assert portable Git contains none of the sensitive values or
local IDs/hashes/counts. Create/activate/assign a stricter source policy through
the Task 10/11 APIs—never direct YAML—then deliberately skip reindex. Assert
recall, portable CURRENT, PROFILE, export, and sync preflight block the affected
artifact despite stale generated `allowed` state, while the report says prior Git
propagation cannot be erased. Stop Core and assert both adapters return
`block_host=False`.

Stage a legacy database containing a full episode payload and an unknown table.
Assert the original hash is unchanged, the local snapshot/export is lossless, the
rows are `candidate-durable-local`/`needs-review`, and portable memory remains
empty until explicit reviewed promotion.

- [ ] **Step 5: Run the release matrix**

```powershell
python -m pytest -q
python -m pytest tests/e2e -v
python -m mneme.cli doctor --vault tests/fixtures/vault-clean
python -m mneme.cli reindex --vault tests/fixtures/vault-clean
python -m mneme.cli context --vault tests/fixtures/vault-clean --mode portable
python -c "import mneme.server; assert callable(mneme.server.main)"
```

Expected: tests pass; doctor has no invalid issue; reindex works with generation unavailable; legacy server remains importable.

Open the PR and require `.github/workflows/ci.yml` job `test` to pass on all four
cells—Ubuntu/Windows × Python 3.11/3.13—and require the aggregate `ci-ok` job.
Record the workflow run URL/IDs in the review evidence.

- [ ] **Step 6: Update coexistence documentation**

Document the opt-in Vault path, generated local state, official policy lifecycle
commands/APIs, migration inspection/lossless local staging, provisional
`mneme-vault` command, legacy HTTP compatibility, pinned Codex hook schema,
no-retroactive-deletion guarantee, and rollback: stop using new adapters and
continue the untouched legacy path. Do not announce repository/package rename or
legacy-data deletion.

- [ ] **Step 7: Commit**

```powershell
git add tests/e2e tests/fixtures/vault-clean README.md docs/ARCHITECTURE.md docs/INTEGRATION.md docs/SETUP-NEW-PC.md PROGRESS.md
git commit -m "test: verify incremental Mneme to Madi migration invariants"
```

---

## D3 Coverage Matrix

| Approved D3 section | Implemented/verified by |
|---|---|
| 1–5: artifact families, ownership, layout, mutation | Tasks 8–14 |
| 6: project/source/workstream registries and resolution states | Tasks 11, 13, 15 |
| 7: portable/local overlay and exact CURRENT composition | Tasks 8, 11–14, 19, 27 |
| 8: immutable Session checkpoint and handoff relations | Tasks 12, 20–22, 27 |
| 9: independent Memory axes, acceptance, supersession, PROFILE | Tasks 14, 19 |
| 10: privacy ceiling, policy receipt, policy-time limitation | Tasks 9–11, 14, 19, 27 |
| 11: concurrency and Git conflict policy | Tasks 11–13, 18, 27 |
| 12: lifecycle/command/domain/operational vocabularies | Task 20 |
| 13: compact lifecycle and Claude/Codex handoff | Tasks 12, 20–22, 27 |
| 14: progressive FTS retrieval and provenance | Tasks 4, 15, 19 |
| 15: clean/shallow clone and failure scenarios | Tasks 16, 18, 19, 27 |
| 16: typed personal owner and future scope compatibility | Tasks 8, 11, 14 |
| 17: non-destructive Mneme migration | Tasks 1–7, 23–27 |
| 18: all testable invariants | Task 0 CI evidence and Task 27 release matrix |
| 19–20: accepted trade-offs and authoritative verdict | Global constraints and every review gate |

Self-review verdict: every binding D3 section has an implementation task and a
verification task. No approved invariant requires a repository/package rename,
legacy-data deletion, mandatory generation provider, daemon, or semantic merge.
The one automatic concurrency path is the D3-approved structural union of proven
disjoint head additions. It cannot change semantic bodies, preferred heads,
policies, lifecycle, or existing session lineage.

---

## Final Verification and Review Gate

After Task 27, invoke `superpowers:verification-before-completion`, then
`superpowers:requesting-code-review`. Review must compare the branch against the
approved D3, not only this plan.

Run fresh:

```powershell
python -m pytest -q
python -m pytest tests/e2e -v
git diff --check main...HEAD
git status --short
git log --oneline --decorate main..HEAD
```

The branch is ready for integration review only when:

- every task commit exists and the worktree is clean;
- legacy and new tests pass in the existing `.github/workflows/ci.yml` four-cell matrix: Python 3.11/3.13 on `ubuntu-latest`/`windows-latest`, followed by `ci-ok`;
- `mneme.server:main` remains runnable;
- Core recall/context works with generation and Growth imports blocked;
- a deleted generated DB rebuilds from Markdown and current source mounts;
- a shallow clone restores synced portable semantics;
- disjoint concurrent session heads survive bounded structural retry, while same-session/preferred-head conflicts remain explicit;
- compact/checkpoint survives repeated compaction and agent switch;
- portable/local writers share schemas but portable-to-local references fail before write;
- policy revisions and assignments use official CAS APIs, and tightening blocks recall/CURRENT/PROFILE/export/sync without reindex;
- legacy DB/Wiki hashes prove no mutation or deletion, and episodes/unknown tables remain losslessly staged local review data;
- the Codex native fixture/translator matches the pinned official `PreCompact` schema and source recorded in its README;
- no repository/package rename or destructive cleanup appears in the diff.

Only after code review should `superpowers:finishing-a-development-branch` be used to choose PR/integration handling. Direct merge or push to protected `main` is not part of this plan.

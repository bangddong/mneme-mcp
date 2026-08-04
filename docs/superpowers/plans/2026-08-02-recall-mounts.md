# 읽기 전용 마운트와 recall 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 위키 밖 저장소의 마크다운을 읽기 전용으로 색인하고, MCP 서버 없이도 전문 검색할 수 있게 한다.

**Architecture:** 기존 `wiki_index`/`wiki_fts`와 분리된 `mount_index`/`mount_fts` 테이블을 둔다. `summary` 컬럼이 없으므로 LLM을 호출할 자리가 구조적으로 없고, 기존 `wiki_search`의 후보 선별(`get_all_summaries()` → `llm.select_candidate_paths()`)이 영향을 받지 않는다. 마운트에는 쓰기 함수를 정의하지 않아 문서 훼손이 규율이 아니라 구조로 불가능하다. 검색은 SQLite 파일을 직접 읽는 CLI로도 제공해 서버 기동과 무관하게 동작한다.

**Tech Stack:** Python 3.11+, SQLite FTS5, fastmcp, pytest

## Global Constraints

- Python `>=3.11` (`pyproject.toml`의 `requires-python`)
- 새 런타임 의존성을 추가하지 않는다. 표준 라이브러리와 기존 의존성만 사용한다.
- 모든 파일 I/O는 `encoding="utf-8"`을 명시한다. 생략 금지.
- 주석과 독스트링은 한국어로 작성한다(기존 코드베이스 관례).
- 테스트는 `monkeypatch`로 `WIKI_DIR`·`DB_PATH`를 격리한다(`tests/test_reindex_skip.py` 패턴).
- **`main`에 직접 푸시할 수 없다.** 작업 브랜치 `feat/recall-mounts`에서 진행하고 PR로 머지한다. 필수 체크는 `ci-ok`.
- 커밋은 각 Task 끝에서 한다. 커밋 메시지에 `Co-Authored-By` 트레일러를 넣어도 된다(이 저장소는 표준 트레일러를 쓴다).

---

## File Structure

| 파일 | 책임 |
|---|---|
| `mneme/console.py` (신규) | 콘솔 UTF-8 강제 1곳. `log.py`·`growth.py`·`lint.py`가 각각 다른 방식으로 하던 것을 통합 |
| `mneme/episodes.py` (신규) | `episodes` 테이블 기록 1곳. `server.py`의 생 SQL INSERT 3곳을 대체 |
| `mneme/mounts.py` (신규) | 마운트 설정 파싱, 파일 열거, 경로 이탈 차단, 읽기. **쓰기 함수 없음** |
| `mneme/recall.py` (신규) | 동기화(`sync`), 검색(`search`), CLI 진입점(`main`) |
| `mneme/memory.py` (수정) | `mount_fts`·`mount_index` 테이블 추가 |
| `mneme/server.py` (수정) | `recall` MCP tool 추가, 에피소드 기록 3곳을 `episodes.record()`로 교체 |
| `.claude/skills/recall.md` (신규) | `/recall` 스킬 — CLI를 호출 |
| `tests/test_console.py` (신규) | Task 1 |
| `tests/test_episodes.py` (신규) | Task 2 |
| `tests/test_mounts.py` (신규) | Task 3 |
| `tests/test_recall_sync.py` (신규) | Task 4 |
| `tests/test_recall_search.py` (신규) | Task 5 |

---

## Task 0: 작업 브랜치 생성

**Files:** 없음

- [ ] **Step 1: 브랜치를 만들고 최신 main에서 시작한다**

```bash
cd /e/development/mneme-mcp
git switch main
git pull --ff-only origin main
git switch -c feat/recall-mounts
```

- [ ] **Step 2: 브랜치를 확인한다**

Run: `git rev-parse --abbrev-ref HEAD`
Expected: `feat/recall-mounts`

---

## Task 1: 콘솔 UTF-8 강제 통합

`log.py:48`, `growth.py:118`은 `io.TextIOWrapper`로, `lint.py:309`는 `sys.stdout.reconfigure`로 각각 다르게 처리하고 있다. CLI가 하나 더 늘어나는 시점이므로 여기서 추출한다.

**Files:**
- Create: `mneme/console.py`
- Create: `tests/test_console.py`
- Modify: `mneme/log.py` (46-50행 부근), `mneme/growth.py` (116-120행 부근), `mneme/lint.py` (307-311행 부근)

**Interfaces:**
- Produces: `mneme.console.force_utf8_stdout() -> None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_console.py
"""콘솔 UTF-8 강제 헬퍼 검증."""
import io
import sys


def test_force_utf8_stdout_sets_utf8(monkeypatch):
    from mneme import console

    fake = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
    monkeypatch.setattr(sys, "stdout", fake)

    console.force_utf8_stdout()

    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"


def test_force_utf8_stdout_is_idempotent(monkeypatch):
    from mneme import console

    fake = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
    monkeypatch.setattr(sys, "stdout", fake)

    console.force_utf8_stdout()
    console.force_utf8_stdout()

    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"


def test_force_utf8_stdout_survives_missing_buffer(monkeypatch):
    """buffer 속성이 없는 stdout(pytest capture 등)에서도 예외를 던지지 않는다."""
    from mneme import console

    class NoBuffer:
        encoding = "cp949"

        def write(self, s):
            return len(s)

    monkeypatch.setattr(sys, "stdout", NoBuffer())

    console.force_utf8_stdout()  # 예외 없이 통과해야 한다
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_console.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mneme.console'`

- [ ] **Step 3: 최소 구현을 작성한다**

```python
# mneme/console.py
"""콘솔 출력 인코딩 강제.

Windows 콘솔 기본 코드페이지(한국어 환경은 cp949)에서 한국어·기호 출력이
UnicodeEncodeError로 죽는 것을 막는다. CLI 진입점마다 제각각 처리하던 것을
여기 한 곳으로 모은다.
"""
import io
import sys


def force_utf8_stdout() -> None:
    """stdout을 UTF-8로 재설정한다. 실패해도 예외를 전파하지 않는다."""
    try:
        if getattr(sys.stdout, "encoding", "").lower().replace("-", "") == "utf8":
            return
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
            return
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            sys.stdout = io.TextIOWrapper(buffer, encoding="utf-8")
    except Exception:
        # 출력 인코딩 조정 실패가 명령 자체를 죽여서는 안 된다.
        pass
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_console.py -v`
Expected: 3 passed

- [ ] **Step 5: 기존 3곳을 헬퍼로 교체한다**

`mneme/log.py` — 현재:

```python
    # Windows 콘솔 cp949에서 한국어/중국어 깨짐 방지
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
```

교체 후:

```python
    console.force_utf8_stdout()
```

`mneme/log.py` 상단 import를 정리한다. `io`가 다른 곳에서 쓰이지 않으면 제거한다:

```python
from mneme import console
from mneme.memory import get_connection
```

`mneme/growth.py`도 동일하게 교체한다(같은 `io.TextIOWrapper` 블록).

`mneme/lint.py` — 현재:

```python
        sys.stdout.reconfigure(encoding="utf-8")
```

교체 후:

```python
    console.force_utf8_stdout()
```

`mneme/lint.py` 상단에 `from mneme import console`을 추가한다.

- [ ] **Step 6: 전체 테스트가 깨지지 않았는지 확인한다**

Run: `pytest -q`
Expected: 기존 테스트 전부 통과 + 신규 3건 통과

- [ ] **Step 7: 커밋**

```bash
git add mneme/console.py tests/test_console.py mneme/log.py mneme/growth.py mneme/lint.py
git commit -m "refactor: 콘솔 UTF-8 강제를 console 모듈로 통합

log/growth/lint가 각각 다른 방식으로 stdout 인코딩을 조정하던 것을
force_utf8_stdout() 한 곳으로 모은다. CLI 추가 시 네 번째 임시방편이
생기는 것을 막는다."
```

---

## Task 2: 에피소드 기록 헬퍼

`server.py`의 세 tool(`wiki_search`, `wiki_inject`, `episode_reflect`)에 생 SQL `INSERT INTO episodes`가 중복돼 있다. CLI와 MCP가 같은 형식으로 기록해야 하므로 하나로 모은다.

위치는 행 번호가 아니라 조회로 찾는다 — 계획서를 쓴 뒤 `server.py`가 바뀌면 행 번호는 어긋난다
(실제로 어긋났다: 초안의 110/230/398행은 `wiki_search` FTS 폴백 추가(#4) 이후 각각 +12 밀렸다):

```bash
grep -n "INSERT INTO episodes" mneme/server.py
```

**Files:**
- Create: `mneme/episodes.py`
- Create: `tests/test_episodes.py`
- Modify: `mneme/server.py` (위 세 곳)

**Interfaces:**
- Produces: `mneme.episodes.record(agent, tool, query, result_summary, session_id=None, **extra) -> None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_episodes.py
"""에피소드 기록 공용 헬퍼 검증."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="mneme-episodes-")
    monkeypatch.setenv("WIKI_DIR", str(Path(tmp) / "wiki"))
    monkeypatch.setenv("DB_PATH", str(Path(tmp) / "state.db"))
    from mneme import memory

    memory.init_db()
    return memory


def test_record_inserts_row(isolated):
    from mneme import episodes

    episodes.record(agent="cli", tool="recall", query="트랜잭션",
                    result_summary="2 hits")

    conn = isolated.get_connection()
    rows = conn.execute(
        "SELECT agent, tool, query, result_summary FROM episodes"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["agent"] == "cli"
    assert rows[0]["tool"] == "recall"
    assert rows[0]["query"] == "트랜잭션"
    assert rows[0]["result_summary"] == "2 hits"


def test_record_accepts_optional_reflection_fields(isolated):
    from mneme import episodes

    episodes.record(agent="orchestrator", tool="episode_reflect",
                    query="작업", result_summary="완료",
                    success=1, score=0.8, skills_used="study-k8s-ingress")

    conn = isolated.get_connection()
    row = conn.execute(
        "SELECT success, score, skills_used FROM episodes"
    ).fetchone()
    conn.close()

    assert row["success"] == 1
    assert row["score"] == pytest.approx(0.8)
    assert row["skills_used"] == "study-k8s-ingress"


def test_record_truncates_long_result_summary(isolated):
    from mneme import episodes

    episodes.record(agent="cli", tool="recall", query="q",
                    result_summary="x" * 500)

    conn = isolated.get_connection()
    row = conn.execute("SELECT result_summary FROM episodes").fetchone()
    conn.close()

    assert len(row["result_summary"]) == 200
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_episodes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mneme.episodes'`

- [ ] **Step 3: 최소 구현을 작성한다**

```python
# mneme/episodes.py
"""에피소드 기록 — 어떤 에이전트가 어떤 tool로 무엇을 묻고 뭐라 답했나.

CLI와 MCP tool이 같은 형식으로 남기도록 기록 지점을 한 곳으로 모은다.
조회는 mneme.log가 담당한다.
"""
from mneme.memory import get_connection

# episodes 테이블에서 record()가 채울 수 있는 선택 컬럼.
_OPTIONAL = ("success", "score", "what_worked", "what_failed",
             "next_hint", "skills_used")

_SUMMARY_MAX = 200


def record(agent: str, tool: str, query: str, result_summary: str,
           session_id: str | None = None, **extra) -> None:
    """에피소드 1건을 기록한다.

    result_summary는 200자로 자른다(기존 server.py의 `[:200]` 관례).
    extra로 success/score/skills_used 등 회고 필드를 넘길 수 있다.
    """
    cols = ["session_id", "agent", "tool", "query", "result_summary"]
    vals = [session_id, agent, tool, query,
            (result_summary or "")[:_SUMMARY_MAX]]

    for key in _OPTIONAL:
        if key in extra:
            cols.append(key)
            vals.append(extra[key])

    placeholders = ",".join("?" for _ in cols)
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                f"INSERT INTO episodes({','.join(cols)}) VALUES({placeholders})",
                vals,
            )
    finally:
        conn.close()
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_episodes.py -v`
Expected: 3 passed

- [ ] **Step 5: `server.py`의 세 곳을 교체한다**

상단 import에 추가한다:

```python
from mneme import episodes as episode_log
```

`wiki_search` — 현재 (아래 블록을 그대로 찾아 치환한다):

```python
    # 이력 기록
    with conn:
        conn.execute(
            "INSERT INTO episodes(session_id, agent, tool, query, result_summary) VALUES(?,?,?,?,?)",
            (session_id, agent, "wiki_search", query, summary[:200]),
        )
    conn.close()
    return output
```

교체 후:

```python
    conn.close()
    episode_log.record(agent=agent, tool="wiki_search", query=query,
                       result_summary=summary, session_id=session_id)
    return output
```

`wiki_inject` — 현재:

```python
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO episodes(session_id, agent, tool, query, result_summary) VALUES(?,?,?,?,?)",
            (session_id, source_agent, "wiki_inject", norm, action),
        )
    conn.close()
```

교체 후:

```python
    episode_log.record(agent=source_agent, tool="wiki_inject", query=norm,
                       result_summary=action, session_id=session_id)
```

`episode_reflect` — 현재:

```python
    conn = get_connection()
    with conn:
        conn.execute(
            """INSERT INTO episodes(session_id, agent, tool, query, result_summary,
                                    success, score, what_worked, what_failed, next_hint, skills_used)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, agent, "episode_reflect", task, outcome[:200],
             1 if success else 0, score,
             reflection["what_worked"], reflection["what_failed"], reflection["next_hint"],
             ",".join(skills_used)),
        )
    conn.close()
```

교체 후:

```python
    episode_log.record(
        agent=agent, tool="episode_reflect", query=task,
        result_summary=outcome, session_id=session_id,
        success=1 if success else 0, score=score,
        what_worked=reflection["what_worked"],
        what_failed=reflection["what_failed"],
        next_hint=reflection["next_hint"],
        skills_used=",".join(skills_used),
    )
```

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest -q`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add mneme/episodes.py tests/test_episodes.py mneme/server.py
git commit -m "refactor: 에피소드 기록을 episodes.record()로 통합

server.py 세 곳에 중복된 생 SQL INSERT를 헬퍼 하나로 모은다.
CLI와 MCP tool이 같은 형식으로 기록하기 위한 선행 작업."
```

---

## Task 3: 마운트 설정과 파일 접근

**Files:**
- Create: `mneme/mounts.py`
- Create: `tests/test_mounts.py`

**Interfaces:**
- Produces:
  - `mneme.mounts.Mount` — `name: str`, `root: Path`, `includes: tuple[str, ...]`
  - `mneme.mounts.parse_mounts() -> dict[str, Mount]`
  - `mneme.mounts.list_files(mount: Mount) -> list[str]` (루트 기준 상대경로, `/` 구분자, 정렬됨)
  - `mneme.mounts.abs_path(mount: Mount, path: str) -> Path`
  - `mneme.mounts.read(mount: Mount, path: str) -> str | None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_mounts.py
"""마운트 설정 파싱과 파일 접근 검증."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def repo():
    """마운트 대상 저장소 흉내."""
    tmp = Path(tempfile.mkdtemp(prefix="mneme-mount-"))
    (tmp / "docs").mkdir()
    (tmp / "docs" / "a.md").write_text("# A\n결정 A", encoding="utf-8")
    (tmp / "docs" / "b.md").write_text("# B\n결정 B", encoding="utf-8")
    (tmp / "README.md").write_text("# 리드미", encoding="utf-8")
    (tmp / "noise.txt").write_text("무시", encoding="utf-8")
    return tmp


def test_parse_single_mount(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md,README.md")
    result = mounts.parse_mounts()

    assert list(result) == ["proj"]
    assert result["proj"].root == repo.resolve()
    assert result["proj"].includes == ("docs/**/*.md", "README.md")


def test_parse_keeps_windows_drive_letter(monkeypatch, repo):
    """이름과 루트는 첫 ':'에서만 분리한다 — 드라이브 문자가 잘리면 안 된다."""
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|README.md")
    result = mounts.parse_mounts()

    assert result["proj"].root == repo.resolve()


def test_parse_skips_entry_without_includes(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}")
    assert mounts.parse_mounts() == {}


def test_parse_skips_missing_root(monkeypatch):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", "gone:/no/such/dir|docs/*.md")
    assert mounts.parse_mounts() == {}


def test_parse_multiple_entries(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv(
        "MNEME_MOUNTS",
        f"a:{repo}|README.md;b:{repo}|docs/**/*.md",
    )
    result = mounts.parse_mounts()

    assert sorted(result) == ["a", "b"]


def test_parse_bad_entry_does_not_kill_good_one(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"broken;ok:{repo}|README.md")
    result = mounts.parse_mounts()

    assert list(result) == ["ok"]


def test_list_files_applies_includes(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md,README.md")
    mount = mounts.parse_mounts()["proj"]

    assert mounts.list_files(mount) == ["README.md", "docs/a.md", "docs/b.md"]


def test_list_files_excludes_paths_outside_root(monkeypatch, repo, tmp_path):
    """루트를 벗어나는 include는 결과에서 제외한다."""
    from mneme import mounts

    outside = tmp_path / "secret.md"
    outside.write_text("비밀", encoding="utf-8")

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|../*.md,README.md")
    mount = mounts.parse_mounts()["proj"]

    files = mounts.list_files(mount)
    assert files == ["README.md"]
    assert all(not f.startswith("..") for f in files)


def test_read_returns_utf8_content(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md")
    mount = mounts.parse_mounts()["proj"]

    assert mounts.read(mount, "docs/a.md") == "# A\n결정 A"


def test_read_missing_file_returns_none(monkeypatch, repo):
    from mneme import mounts

    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md")
    mount = mounts.parse_mounts()["proj"]

    assert mounts.read(mount, "docs/nope.md") is None


def test_module_exposes_no_write_function():
    """마운트 문서 훼손을 구조로 차단한다 — 쓰기 함수가 존재하면 안 된다."""
    from mneme import mounts

    forbidden = {"write", "write_file", "delete", "delete_file", "save"}
    assert forbidden.isdisjoint(dir(mounts))
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_mounts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mneme.mounts'`

- [ ] **Step 3: 최소 구현을 작성한다**

```python
# mneme/mounts.py
"""읽기 전용 마운트 — 위키 밖 저장소의 마크다운을 색인 대상으로 노출한다.

설정: 환경변수 MNEME_MOUNTS, 항목은 ';'로 구분한다.

    이름:루트경로|include글롭[,include글롭...]

이름과 루트는 첫 ':'에서만 분리한다. Windows 드라이브 문자(E:/...)가 ':'를
포함하므로 모든 ':'로 분리하면 경로가 잘린다.

include는 필수다. 기본값을 "전부"로 두면 의존성 디렉토리와 빌드 산출물이
섞이므로 화이트리스트만 허용한다.

이 모듈은 쓰기 함수를 정의하지 않는다. 마운트 문서의 소유권은 각 저장소에
있고, 훼손 가능성을 규율이 아니라 구조로 차단한다.
"""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mount:
    name: str
    root: Path
    includes: tuple[str, ...]


def _parse_entry(entry: str) -> Mount | None:
    """항목 1개를 파싱한다. 형식 오류·존재하지 않는 루트는 None을 반환한다."""
    entry = entry.strip()
    if not entry:
        return None

    spec, sep, globs = entry.partition("|")
    if not sep:
        return None  # include 누락

    name, sep2, root = spec.partition(":")  # 첫 ':' 에서만 분리
    name, root = name.strip(), root.strip()
    if not sep2 or not name or not root:
        return None

    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return None

    includes = tuple(g.strip() for g in globs.split(",") if g.strip())
    if not includes:
        return None

    return Mount(name=name, root=root_path.resolve(), includes=includes)


def parse_mounts() -> dict[str, Mount]:
    """MNEME_MOUNTS를 파싱한다. 잘못된 항목은 건너뛰고 나머지를 살린다."""
    raw = os.getenv("MNEME_MOUNTS", "").strip()
    mounts: dict[str, Mount] = {}
    for entry in raw.split(";"):
        mount = _parse_entry(entry)
        if mount is not None:
            mounts[mount.name] = mount
    return mounts


def list_files(mount: Mount) -> list[str]:
    """include 글롭을 확장해 루트 기준 상대경로 목록을 반환한다.

    resolve() 후 루트 안에 있는지 검사해 심볼릭 링크를 포함한 경로 이탈을 막는다.
    """
    found: set[str] = set()
    for pattern in mount.includes:
        for path in mount.root.glob(pattern):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(mount.root):
                continue
            rel = resolved.relative_to(mount.root)
            found.add(str(rel).replace("\\", "/"))
    return sorted(found)


def abs_path(mount: Mount, path: str) -> Path:
    """루트 기준 상대경로를 절대경로로 바꾼다."""
    return mount.root / path


def read(mount: Mount, path: str) -> str | None:
    """마운트 파일을 UTF-8로 읽는다. 없으면 None."""
    full = abs_path(mount, path)
    if not full.is_file():
        return None
    return full.read_text(encoding="utf-8")
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_mounts.py -v`
Expected: 11 passed

- [ ] **Step 5: 커밋**

```bash
git add mneme/mounts.py tests/test_mounts.py
git commit -m "feat: 읽기 전용 마운트 설정 파싱과 파일 접근 추가

MNEME_MOUNTS로 위키 밖 저장소를 등록한다. 이름과 루트는 첫 ':'에서만
분리해 Windows 드라이브 문자를 보존하고, include 글롭을 필수로 요구한다.
루트 이탈 경로는 resolve() 후 검사해 제외한다. 쓰기 함수는 두지 않는다."
```

---

## Task 4: 스키마와 동기화

**Files:**
- Modify: `mneme/memory.py` (`init_db()`의 `executescript` 블록)
- Create: `mneme/recall.py` (`sync`만. `search`/`main`은 Task 5)
- Create: `tests/test_recall_sync.py`

**Interfaces:**
- Consumes: `mneme.mounts.parse_mounts`, `list_files`, `abs_path`, `read` (Task 3)
- Produces: `mneme.recall.sync(source: str | None = None) -> dict` — `{"added": int, "updated": int, "removed": int, "skipped": int}`

**주의:** `mount_fts`의 `source`·`path`는 `UNINDEXED`로 선언한다. 그러지 않으면 `docs` 같은 질의가 경로에 `docs`를 포함한 모든 행에 매치돼 결과가 오염된다. `UNINDEXED` 컬럼도 SELECT로는 정상 조회된다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_recall_sync.py
"""마운트 동기화 검증.

핵심 회귀 방지 2건:
- 동기화 중 LLM이 호출되지 않는다 (설계 전제: 색인 비용 0)
- 기존 wiki_index/wiki_fts가 변하지 않는다 (설계 전제: wiki_search 무영향)
"""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mneme-sync-"))
    wiki = tmp / "wiki"
    wiki.mkdir()
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text("# A\n캐시 전략을 정했다", encoding="utf-8")
    (repo / "docs" / "b.md").write_text("# B\n인덱스를 걸었다", encoding="utf-8")

    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(tmp / "state.db"))
    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md")

    from mneme import memory

    memory.init_db()
    return memory, repo


def _mount_rows(memory):
    conn = memory.get_connection()
    rows = conn.execute(
        "SELECT source, path, content_hash FROM mount_index ORDER BY path"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_first_sync_indexes_all(env):
    memory, repo = env
    from mneme import recall

    stats = recall.sync()

    assert stats["added"] == 2
    assert [r["path"] for r in _mount_rows(memory)] == ["docs/a.md", "docs/b.md"]


def test_unchanged_files_are_skipped(env):
    memory, repo = env
    from mneme import recall

    recall.sync()
    stats = recall.sync()

    assert stats["added"] == 0
    assert stats["updated"] == 0
    assert stats["skipped"] == 2


def test_changed_file_is_reindexed(env):
    memory, repo = env
    from mneme import recall

    recall.sync()
    before = {r["path"]: r["content_hash"] for r in _mount_rows(memory)}

    (repo / "docs" / "a.md").write_text("# A\n생각이 바뀌었다", encoding="utf-8")
    stats = recall.sync()

    after = {r["path"]: r["content_hash"] for r in _mount_rows(memory)}
    assert stats["updated"] == 1
    assert after["docs/a.md"] != before["docs/a.md"]
    assert after["docs/b.md"] == before["docs/b.md"]


def test_deleted_file_is_removed(env):
    memory, repo = env
    from mneme import recall

    recall.sync()
    (repo / "docs" / "b.md").unlink()
    stats = recall.sync()

    assert stats["removed"] == 1
    assert [r["path"] for r in _mount_rows(memory)] == ["docs/a.md"]

    conn = memory.get_connection()
    left = conn.execute(
        "SELECT COUNT(*) FROM mount_fts WHERE path = 'docs/b.md'"
    ).fetchone()[0]
    conn.close()
    assert left == 0


def test_new_file_is_added(env):
    memory, repo = env
    from mneme import recall

    recall.sync()
    (repo / "docs" / "c.md").write_text("# C\n새 결정", encoding="utf-8")
    stats = recall.sync()

    assert stats["added"] == 1
    assert [r["path"] for r in _mount_rows(memory)] == [
        "docs/a.md", "docs/b.md", "docs/c.md",
    ]


def test_sync_never_calls_llm(env, monkeypatch):
    """설계 전제 — 마운트 색인에 LLM 비용이 들지 않는다."""
    memory, repo = env
    from mneme import llm, recall

    def boom(*args, **kwargs):
        raise AssertionError("마운트 동기화가 LLM을 호출했다")

    monkeypatch.setattr(llm, "generate_summary", boom)
    monkeypatch.setattr(llm, "_call", boom)

    recall.sync()


def test_sync_does_not_touch_wiki_tables(env):
    """설계 전제 — 기존 wiki_search 경로에 영향을 주지 않는다."""
    memory, repo = env
    from mneme import recall

    conn = memory.get_connection()
    before_index = conn.execute("SELECT COUNT(*) FROM wiki_index").fetchone()[0]
    before_fts = conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0]
    conn.close()

    recall.sync()

    conn = memory.get_connection()
    after_index = conn.execute("SELECT COUNT(*) FROM wiki_index").fetchone()[0]
    after_fts = conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0]
    conn.close()

    assert (after_index, after_fts) == (before_index, before_fts)


def test_sync_filters_by_source(env, monkeypatch, tmp_path):
    memory, repo = env
    from mneme import recall

    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    (other / "docs" / "x.md").write_text("# X", encoding="utf-8")
    monkeypatch.setenv(
        "MNEME_MOUNTS",
        f"proj:{repo}|docs/**/*.md;other:{other}|docs/**/*.md",
    )

    recall.sync(source="other")

    assert {r["source"] for r in _mount_rows(memory)} == {"other"}
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_recall_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mneme.recall'`

- [ ] **Step 3: 스키마를 추가한다**

`mneme/memory.py`의 `init_db()` 안 `executescript` 문자열 끝에 아래를 덧붙인다:

```sql
            CREATE VIRTUAL TABLE IF NOT EXISTS mount_fts USING fts5(
                source UNINDEXED,   -- 검색 대상이 아니라 조회용 (경로 토큰 오염 방지)
                path UNINDEXED,
                content,
                tokenize = 'unicode61'
            );

            CREATE TABLE IF NOT EXISTS mount_index (
                source       TEXT NOT NULL,
                path         TEXT NOT NULL,   -- 마운트 루트 기준 상대경로
                content_hash TEXT,            -- 원문 sha256
                mtime        REAL,            -- 빠른 변경 판정용
                size         INTEGER,
                indexed_at   TEXT,
                PRIMARY KEY (source, path)
            );
```

- [ ] **Step 4: `sync`를 구현한다**

```python
# mneme/recall.py
"""마운트 문서 동기화와 검색.

위키(wiki_index/wiki_fts)와 분리된 mount_index/mount_fts를 쓴다.
summary 컬럼이 없으므로 LLM을 호출할 자리가 없고, 색인 비용이 0이다.
"""
import hashlib
from datetime import datetime, timezone

from mneme import mounts as mounts_mod
from mneme.memory import get_connection


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sync(source: str | None = None) -> dict:
    """마운트 파일 목록과 인덱스를 대조해 변경분만 갱신한다.

    mtime과 size가 모두 같으면 읽지 않고 건너뛴다. 다르면 읽어서 해시를
    비교하고, 해시까지 같으면 메타데이터만 갱신한다.
    """
    stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
    active = mounts_mod.parse_mounts()
    if source is not None:
        active = {k: v for k, v in active.items() if k == source}

    conn = get_connection()
    try:
        for name, mount in active.items():
            current = mounts_mod.list_files(mount)
            known = {
                row["path"]: row
                for row in conn.execute(
                    "SELECT path, content_hash, mtime, size FROM mount_index "
                    "WHERE source = ?",
                    (name,),
                ).fetchall()
            }

            for rel in current:
                try:
                    stat = mounts_mod.abs_path(mount, rel).stat()
                except OSError:
                    continue

                prev = known.get(rel)
                if (prev is not None
                        and prev["mtime"] == stat.st_mtime
                        and prev["size"] == stat.st_size):
                    stats["skipped"] += 1
                    continue

                content = mounts_mod.read(mount, rel)
                if content is None:
                    continue
                chash = _hash(content)

                if prev is not None and prev["content_hash"] == chash:
                    with conn:
                        conn.execute(
                            "UPDATE mount_index SET mtime=?, size=? "
                            "WHERE source=? AND path=?",
                            (stat.st_mtime, stat.st_size, name, rel),
                        )
                    stats["skipped"] += 1
                    continue

                now = datetime.now(tz=timezone.utc).isoformat()
                with conn:
                    conn.execute(
                        "DELETE FROM mount_fts WHERE source=? AND path=?",
                        (name, rel),
                    )
                    conn.execute(
                        "INSERT INTO mount_fts(source, path, content) VALUES(?,?,?)",
                        (name, rel, content),
                    )
                    conn.execute(
                        """INSERT INTO mount_index(source, path, content_hash,
                                                   mtime, size, indexed_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(source, path) DO UPDATE SET
                               content_hash = excluded.content_hash,
                               mtime        = excluded.mtime,
                               size         = excluded.size,
                               indexed_at   = excluded.indexed_at""",
                        (name, rel, chash, stat.st_mtime, stat.st_size, now),
                    )
                stats["added" if prev is None else "updated"] += 1

            for rel in set(known) - set(current):
                with conn:
                    conn.execute(
                        "DELETE FROM mount_fts WHERE source=? AND path=?",
                        (name, rel),
                    )
                    conn.execute(
                        "DELETE FROM mount_index WHERE source=? AND path=?",
                        (name, rel),
                    )
                stats["removed"] += 1
    finally:
        conn.close()

    return stats
```

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_recall_sync.py -v`
Expected: 8 passed

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest -q`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add mneme/memory.py mneme/recall.py tests/test_recall_sync.py
git commit -m "feat: 마운트 색인 테이블과 동기화 추가

wiki_index와 분리된 mount_index/mount_fts를 둔다. summary 컬럼이 없어
LLM 호출 경로가 존재하지 않고, 기존 wiki_search가 영향을 받지 않는다.
mtime+size로 1차 판정하고 다를 때만 읽어서 해시를 비교한다."
```

---

## Task 5: 검색과 CLI

**Files:**
- Modify: `mneme/recall.py` (`search`, `main` 추가)
- Create: `tests/test_recall_search.py`

**Interfaces:**
- Consumes: `mneme.recall.sync` (Task 4), `mneme.episodes.record` (Task 2), `mneme.console.force_utf8_stdout` (Task 1)
- Produces:
  - `mneme.recall.RecallQueryError` — FTS 질의 문법 오류
  - `mneme.recall.search(query: str, source: str | None = None, limit: int = 10) -> list[dict]` — `[{"source", "path", "excerpt"}]`
  - `mneme.recall.main() -> None` — CLI 진입점

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_recall_search.py
"""마운트 검색과 CLI 검증."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mneme-search-"))
    (tmp / "wiki").mkdir()
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "cache.md").write_text(
        "# 캐시\n캐시 전략은 write-through로 정했다", encoding="utf-8")
    (repo / "docs" / "index.md").write_text(
        "# 인덱스\nB-tree 인덱스를 걸었다", encoding="utf-8")

    monkeypatch.setenv("WIKI_DIR", str(tmp / "wiki"))
    monkeypatch.setenv("DB_PATH", str(tmp / "state.db"))
    monkeypatch.setenv("MNEME_MOUNTS", f"proj:{repo}|docs/**/*.md")

    from mneme import memory

    memory.init_db()
    return memory, repo


def test_search_finds_content(env):
    from mneme import recall

    recall.sync()
    hits = recall.search("write-through")

    assert len(hits) == 1
    assert hits[0]["source"] == "proj"
    assert hits[0]["path"] == "docs/cache.md"
    assert "write-through" in hits[0]["excerpt"]


def test_search_filters_by_source(env, monkeypatch, tmp_path):
    from mneme import recall

    _, repo = env
    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    (other / "docs" / "z.md").write_text("# Z\n캐시 이야기", encoding="utf-8")
    monkeypatch.setenv(
        "MNEME_MOUNTS",
        f"proj:{repo}|docs/**/*.md;other:{other}|docs/**/*.md",
    )
    recall.sync()  # 둘 다 색인한 상태에서 필터가 실제로 거르는지 본다

    assert {h["source"] for h in recall.search("캐시")} == {"proj", "other"}
    assert [h["source"] for h in recall.search("캐시", source="other")] == ["other"]


def test_search_respects_limit(env):
    from mneme import recall

    recall.sync()
    assert len(recall.search("인덱스 OR 캐시", limit=1)) == 1


def test_path_tokens_do_not_match(env):
    """source/path가 UNINDEXED이므로 경로 문자열로는 매치되지 않는다."""
    from mneme import recall

    recall.sync()
    assert recall.search("docs") == []


def test_malformed_query_raises_recall_error(env):
    from mneme import recall

    recall.sync()
    with pytest.raises(recall.RecallQueryError):
        recall.search('"unbalanced')


def test_cli_records_one_episode(env, monkeypatch, capsys):
    """도그푸딩 계측 — recall 1회에 episodes 1행."""
    memory, _ = env
    from mneme import recall

    monkeypatch.setattr("sys.argv", ["mneme.recall", "write-through"])
    recall.main()

    conn = memory.get_connection()
    rows = conn.execute(
        "SELECT agent, tool, query FROM episodes WHERE tool = 'recall'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["agent"] == "cli"
    assert rows[0]["query"] == "write-through"


def test_cli_prints_hits(env, monkeypatch, capsys):
    from mneme import recall

    monkeypatch.setattr("sys.argv", ["mneme.recall", "write-through"])
    recall.main()

    out = capsys.readouterr().out
    assert "docs/cache.md" in out
    assert "proj" in out


def test_cli_reports_no_hits(env, monkeypatch, capsys):
    from mneme import recall

    monkeypatch.setattr("sys.argv", ["mneme.recall", "존재하지않는단어xyz"])
    recall.main()

    assert "결과 없음" in capsys.readouterr().out
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_recall_search.py -v`
Expected: FAIL — `AttributeError: module 'mneme.recall' has no attribute 'search'`

- [ ] **Step 3: `search`와 CLI를 구현한다**

`mneme/recall.py` 상단 import에 추가한다:

```python
import argparse
import sqlite3
import sys

from mneme import console
from mneme import episodes as episode_log
```

파일 끝에 아래를 덧붙인다:

```python
class RecallQueryError(ValueError):
    """FTS5 질의 문법 오류."""


def search(query: str, source: str | None = None, limit: int = 10) -> list[dict]:
    """마운트 전문 검색. 원문 발췌를 그대로 돌려준다(요약 생성하지 않음)."""
    sql = [
        "SELECT source, path,",
        "       snippet(mount_fts, 2, '[', ']', '...', 20) AS excerpt",
        "FROM mount_fts WHERE mount_fts MATCH ?",
    ]
    params: list = [query]
    if source is not None:
        sql.append("AND source = ?")
        params.append(source)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(limit)

    conn = get_connection()
    try:
        rows = conn.execute(" ".join(sql), params).fetchall()
    except sqlite3.OperationalError as exc:
        raise RecallQueryError(str(exc)) from exc
    finally:
        conn.close()

    return [
        {"source": r["source"], "path": r["path"], "excerpt": r["excerpt"]}
        for r in rows
    ]
```

> **`index.search_fts`와 계약이 다르다 — 의도된 것이다.** (이 계획 작성 이후 #4에서 갈라졌다)
>
> | | 문법 오류 질의(`Grafana "unclosed`) |
> |---|---|
> | `index.search_fts` | 질의 전체를 구(phrase)로 escape해 **재시도** → 결과 반환 |
> | `recall.search` | `RecallQueryError` **raise** |
>
> `recall`은 CLI가 1차 사용자이고 `--help`가 "검색어 (FTS5 문법)"이라고 명시하므로,
> 사용자가 직접 쓴 문법의 오류는 조용히 삼키는 것보다 알려주는 편이 낫다.
> 대신 **MCP tool 경로에서는 예외를 밖으로 내보내지 않는다** — Task 6이 이를 잡아
> `{"hits": [], "error": ...}`로 변환한다. 임의 문자열을 넘기는 에이전트가
> 질의 '모양' 때문에 터지면 안 된다는 원칙은 양쪽이 같다.
>
> 결과적으로 같은 질의에 `wiki_search`는 결과를, `recall`은 0건+에러 문구를 준다.
> 도구를 통합하게 되면 이 차이부터 정리한다.

```python
def main() -> None:
    console.force_utf8_stdout()

    parser = argparse.ArgumentParser(
        description="마운트된 저장소 문서에서 결정·맥락을 검색한다"
    )
    parser.add_argument("query", help="검색어 (FTS5 문법)")
    parser.add_argument("-s", "--source", help="특정 마운트로 제한")
    parser.add_argument("-n", "--limit", type=int, default=10, help="최대 건수 (기본 10)")
    parser.add_argument("--agent", default="cli", help="에피소드에 기록할 호출자 (기본 cli)")
    parser.add_argument("--no-sync", action="store_true",
                        help="검색 전 동기화를 건너뛴다")
    args = parser.parse_args()

    if not args.no_sync:
        sync(source=args.source)

    try:
        hits = search(args.query, source=args.source, limit=args.limit)
    except RecallQueryError as exc:
        print(f"질의 문법 오류: {exc}")
        sys.exit(2)

    if not hits:
        print("결과 없음")
    else:
        for hit in hits:
            print(f"[{hit['source']}] {hit['path']}")
            print(f"    {hit['excerpt']}")
        print(f"\n— {len(hits)}건")

    episode_log.record(
        agent=args.agent, tool="recall", query=args.query,
        result_summary=f"{len(hits)} hits: "
                       + ", ".join(f"{h['source']}/{h['path']}" for h in hits[:3]),
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_recall_search.py -v`
Expected: 8 passed

- [ ] **Step 5: 실제로 돌려본다**

```bash
MNEME_MOUNTS="mneme:$PWD|docs/**/*.md,PROGRESS.md" python -m mneme.recall "마운트"
```

Expected: `[mneme] docs/superpowers/specs/2026-08-01-recall-mounts-design.md` 가 포함된 결과

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest -q`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add mneme/recall.py tests/test_recall_search.py
git commit -m "feat: recall 검색과 CLI 추가

python -m mneme.recall 로 MCP 서버 없이 마운트 문서를 검색한다.
source/path는 UNINDEXED라 경로 토큰이 결과를 오염시키지 않는다.
호출마다 episodes에 tool=recall 로 기록해 실사용을 관측한다."
```

---

## Task 6: MCP tool과 스킬

**Files:**
- Modify: `mneme/server.py` (tool 추가)
- Create: `.claude/skills/recall.md`

**Interfaces:**
- Consumes: `mneme.recall.sync`, `search`, `RecallQueryError` (Tasks 4-5), `mneme.episodes.record` (Task 2)
- Produces: MCP tool `recall(query, source=None, limit=10, agent="unknown") -> dict`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_recall_search.py` 끝에 덧붙인다:

```python
def _tool(server, name):
    """MCP tool의 원본 함수를 얻는다.

    설치된 fastmcp(3.4.x)에서 @mcp.tool()은 함수를 그대로 반환하지만,
    버전에 따라 FunctionTool을 반환하고 원본이 .fn에 붙는 경우가 있다.
    pyproject가 fastmcp>=2.0.0으로 열려 있으므로 양쪽을 모두 받는다.
    """
    obj = getattr(server, name)
    return getattr(obj, "fn", obj)


def test_mcp_tool_returns_hits_and_records_episode(env):
    memory, _ = env
    from mneme import recall
    import mneme.server as server

    recall.sync()
    result = _tool(server, "recall")(query="write-through", agent="claude-code")

    assert result["hits"][0]["path"] == "docs/cache.md"

    conn = memory.get_connection()
    rows = conn.execute(
        "SELECT agent FROM episodes WHERE tool = 'recall'"
    ).fetchall()
    conn.close()
    assert [r["agent"] for r in rows] == ["claude-code"]


def test_mcp_tool_returns_error_for_malformed_query(env):
    import mneme.server as server

    result = _tool(server, "recall")(query='"unbalanced')

    assert "error" in result
    assert result["hits"] == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_recall_search.py -k mcp_tool -v`
Expected: FAIL — `AttributeError: module 'mneme.server' has no attribute 'recall'`

- [ ] **Step 3: MCP tool을 추가한다**

`mneme/server.py` 상단 import에 추가한다:

```python
from mneme import recall as recall_mod
```

`wiki_search` tool 정의 아래에 추가한다:

```python
@mcp.tool()
def recall(query: str, source: str | None = None, limit: int = 10,
           agent: str = "unknown") -> dict:
    """마운트된 저장소 문서(각 레포의 결정·진행 기록)에서 원문을 검색한다.

    위키 검색(wiki_search)과 달리 LLM 요약을 거치지 않고 원문 발췌를
    그대로 돌려준다. 마운트는 읽기 전용이다.
    """
    recall_mod.sync(source=source)
    try:
        hits = recall_mod.search(query, source=source, limit=limit)
    except recall_mod.RecallQueryError as exc:
        return {"hits": [], "error": f"질의 문법 오류: {exc}"}

    episode_log.record(
        agent=agent, tool="recall", query=query,
        result_summary=f"{len(hits)} hits: "
                       + ", ".join(f"{h['source']}/{h['path']}" for h in hits[:3]),
    )
    return {"hits": hits}
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_recall_search.py -v`
Expected: 10 passed

- [ ] **Step 5: 스킬 파일을 만든다**

```markdown
<!-- .claude/skills/recall.md -->
---
name: recall
description: 지난 결정과 맥락을 마운트된 저장소 문서에서 찾는다. "왜 이렇게 정했더라", "예전에 뭘 시도했더라" 같은 질문에 쓴다.
---

# recall

마운트된 저장소의 마크다운(각 레포의 `docs/`, `PROGRESS.md`, 프로젝트 컨텍스트
문서)에서 원문을 검색한다. MCP 서버가 꺼져 있어도 동작한다.

## 사용

```bash
python -m mneme.recall "검색어"
python -m mneme.recall "검색어" -s <소스이름> -n 5
```

검색어는 SQLite FTS5 문법을 따른다. 여러 단어는 AND로 묶이고,
`OR`·`NOT`·`"구문 검색"`을 쓸 수 있다.

## 결과 해석

`[소스] 경로` 다음 줄에 원문 발췌가 나온다. 발췌만으로 부족하면 그 경로를
직접 읽는다. 요약이 아니라 원문이므로 그대로 신뢰해도 된다.

## 주의

- 마운트는 **읽기 전용**이다. 여기서 찾은 문서를 고치려면 해당 저장소에서
  직접 편집한다.
- 마운트 대상은 `.env`의 `MNEME_MOUNTS`가 정한다. 결과가 비어 있으면 먼저
  그 설정을 확인한다.
```

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest -q`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add mneme/server.py .claude/skills/recall.md tests/test_recall_search.py
git commit -m "feat: recall MCP tool과 /recall 스킬 추가

서버가 켜져 있을 때는 MCP tool로, 꺼져 있을 때는 CLI로 같은 코어를 쓴다.
두 표면 모두 episodes에 tool=recall 로 기록한다."
```

---

## Task 7: 문서화와 PR

**Files:**
- Modify: `.env.example`, `README.md`, `PROGRESS.md`

- [ ] **Step 1: `.env.example`에 설정 예시를 넣는다**

파일 끝에 덧붙인다:

```
# 읽기 전용 마운트 — 위키 밖 저장소의 마크다운을 검색 대상에 포함한다.
# 형식: 이름:루트경로|include글롭[,글롭...]   (항목 구분자 ';')
# 이름과 루트는 첫 ':'에서만 분리하므로 Windows 드라이브 문자를 써도 된다.
# include는 필수다 — 생략하면 해당 항목을 건너뛴다.
MNEME_MOUNTS=
```

- [ ] **Step 2: `README.md`에 절을 추가한다**

기존 tool 목록 절 아래에 덧붙인다:

```markdown
### 읽기 전용 마운트와 recall

위키 밖 저장소의 마크다운을 색인해 전문 검색할 수 있다. 각 저장소의
`docs/`·`PROGRESS.md` 같은 결정 기록을 옮기지 않고 그 자리에 둔 채 찾는다.

`.env`에 마운트를 등록한다:

```
MNEME_MOUNTS=myproj:/path/to/myproj|docs/**/*.md,README.md
```

검색은 MCP 서버 없이도 된다:

```bash
python -m mneme.recall "검색어" -s myproj
```

마운트는 읽기 전용이다. mneme은 마운트 파일을 쓰지 않는다.
```

- [ ] **Step 3: `PROGRESS.md`를 갱신한다**

"3. TODO" 절의 "인프라 / 기타" 아래에 완료 항목으로 추가한다:

```markdown
- [x] **읽기 전용 마운트 + recall(2026-08-02)**: 위키 밖 저장소의 마크다운을
  `mount_index`/`mount_fts`에 색인하고 `python -m mneme.recall`로 검색.
  summary 컬럼이 없어 LLM 비용 0, 기존 `wiki_search` 무영향.
  설계는 `docs/superpowers/specs/2026-08-01-recall-mounts-design.md`.
```

- [ ] **Step 4: 전체 테스트를 마지막으로 돌린다**

Run: `pytest -q`
Expected: 전부 통과

- [ ] **Step 5: 커밋하고 푸시한다**

```bash
git add .env.example README.md PROGRESS.md
git commit -m "docs: 읽기 전용 마운트와 recall 사용법 추가"
git push -u origin feat/recall-mounts
```

- [ ] **Step 6: PR을 연다**

```bash
gh pr create --title "feat: 읽기 전용 마운트와 recall" --body "설계: docs/superpowers/specs/2026-08-01-recall-mounts-design.md

위키 밖 저장소(각 레포의 docs/, PROGRESS.md 등)의 마크다운을 읽기 전용으로
색인하고 전문 검색한다. 결정 기록을 위키로 옮기지 않고 git에 둔 채로 찾는다.

- wiki_index와 분리된 mount_index/mount_fts. summary 컬럼이 없어 LLM 호출
  경로가 존재하지 않고 기존 wiki_search가 영향받지 않는다.
- 마운트에 쓰기 함수를 두지 않아 문서 훼손이 구조적으로 불가능하다.
- CLI(python -m mneme.recall)는 SQLite를 직접 읽으므로 MCP 서버가 꺼져
  있어도 동작한다.
- 부수 정리: 콘솔 UTF-8 강제를 console.py로, 에피소드 기록을 episodes.py로
  통합(각각 3곳에 중복돼 있었다)."
```

- [ ] **Step 7: CI를 확인하고 머지한다**

```bash
gh pr checks --watch
```

Expected: `ci-ok` PASS

체크가 통과하면 Squash Merge 한다:

```bash
gh pr merge --squash --delete-branch
```

---

## 완료 기준

- `pytest -q` 전부 통과 (신규 30건 이상)
- `python -m mneme.recall "검색어"`가 서버를 끈 상태에서 결과를 반환한다
- `python -m mneme.log -t recall`로 호출 이력이 보인다
- `wiki_search`가 이전과 동일하게 동작한다
- PR이 `ci-ok` 통과 후 Squash Merge 됨

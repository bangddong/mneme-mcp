# 읽기 전용 마운트와 recall — 설계

- 상태: 승인됨 (구현 전)
- 작성: 2026-08-01

## 문제

mneme은 단일 `WIKI_DIR` 하나만 색인한다. 그런데 프로젝트를 진행하며 내려진 결정은 각
저장소의 git 마크다운(`docs/DECISIONS.md`, `PROGRESS.md`, `CLAUDE.md`, 프로젝트별 컨텍스트
문서)에 쌓인다. 이 문서들은 위키 밖에 있으므로 `wiki_search`로 찾을 수 없다.

결과적으로 "지난번에 왜 이렇게 정했더라"를 되짚을 때 mneme을 쓸 이유가 없다. 실제 사용
기록도 이를 뒷받침한다 — 누적 에피소드가 소수에 그치고, 그나마 대부분이 개발 세션의
부산물인 `wiki_inject`다.

## 목표

1. 여러 저장소의 마크다운을 **읽기 전용**으로 색인해 전문 검색 가능하게 한다.
2. 검색이 **MCP 서버 없이도** 동작한다. 서버는 상시 기동하지 않는 온디맨드 스택이다.
3. 기존 `wiki_search` 경로에 **영향을 주지 않는다**.
4. 색인에 **LLM 비용이 들지 않는다**.

## 비목표

- 마운트 문서에 대한 쓰기. 결정 기록의 소유권은 각 저장소에 있다.
- 스킬 성향·Outer Loop 등 성장 계층의 활성화. 그것은 `episode_reflect`가 필요한 별개 문제다.
- 세션 시작 시 자동 검색 주입. 수동 호출이 실제로 쓰이는지 먼저 관측한다.

## 설계 근거

### 왜 별도 테이블인가

`wiki_index.summary`는 `get_all_summaries()`를 거쳐 `llm.select_candidate_paths()`의 프롬프트로
들어간다. 여기에 요약 없는 행을 섞으면 기존 `wiki_search`의 후보 선별이 열화된다.

또한 위키와 마운트는 성격이 다르다. 위키는 OKF 규약·요약·큐레이션을 갖춘 지식 베이스이고,
마운트는 원문 그대로의 저장소 문서다. 한 테이블에 넣으면 `summary`가 nullable이 되고
`is_content_page()`가 소스별로 분기하며 `write_file()`에 방어 가드가 필요해진다.

별도 테이블은 이 셋을 모두 없앤다. 특히 **마운트에는 쓰기 함수를 만들지 않으므로, 마운트
문서 훼손이 규율이 아니라 구조로 불가능하다.**

### 왜 읽기 전용인가

- 위키 병합이 문서를 훼손한 선례가 있다(frontmatter 구분자 소실, 문장부호 치환). 그것은
  mneme이 소유한 파일에서 벌어진 일이다. 소유하지 않은 저장소는 더 엄격해야 한다.
- 각 저장소는 자기 문서 규약을 갖는다. 외부에서 이를 알 수 없다.
- 되돌리기 비용이 비대칭이다. 쓰기를 나중에 추가하는 것은 모듈 추가이지만, 이미 쓰인 것을
  걷어내는 것은 여러 저장소의 이력을 뒤지는 일이다.

## 설계

### 설정

환경변수 `MNEME_MOUNTS`. 항목은 `;`로 구분한다.

```
이름:루트경로|include글롭[,include글롭...]
```

예:

```
MNEME_MOUNTS=proj-a:/path/to/proj-a|docs/**/*.md,README.md;proj-b:/path/to/proj-b|docs/**/*.md
```

**파싱 규칙**: 이름과 루트는 **첫 번째 `:`에서만 분리한다**(`split(":", 1)`). Windows 드라이브
문자가 `:`를 포함하므로(`m:E:/dev/proj`), 모든 `:`로 분리하면 경로가 잘린다. 이름에는 `:`를
허용하지 않는다.

`include`는 **필수**다. 생략 시 해당 항목을 건너뛴다. 기본값을 "전부"로 두면 의존성 디렉토리와
빌드 산출물이 섞이므로, 화이트리스트만 허용한다.

구체적인 마운트 값은 각자의 `.env`에 둔다(저장소에 커밋하지 않는다).

### 스키마

`memory.init_db()`에 추가한다. 기존 데이터 마이그레이션은 없다.

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS mount_fts USING fts5(
    source,
    path,
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

`summary` 컬럼이 없다. 컬럼이 없으므로 요약을 생성할 자리가 없고, LLM 비용이 구조적으로 0이다.

### 모듈

**`mneme/mounts.py`** — 마운트 해석과 파일 접근

- `parse_mounts() -> dict[str, Mount]` — 설정 파싱. 형식 오류·존재하지 않는 루트는 경고 후
  건너뛴다. 기동을 막지 않는다.
- `list_files(source) -> list[str]` — include 글롭 확장. 각 결과를 `resolve()`한 뒤
  `is_relative_to(root)`로 검사해 루트를 벗어나는 경로(심볼릭 링크 포함)를 제외한다.
- `abs_path(source, path) -> Path` — 상대경로를 절대경로로 해석. `sync()`가 `stat()`으로
  `mtime`·`size`를 얻는 데 쓴다.
- `read(source, path) -> str | None` — `encoding="utf-8"` 명시.

쓰기 함수는 정의하지 않는다.

**`mneme/recall.py`** — 동기화와 검색

- `sync(source=None) -> dict` — 아래 "갱신" 참조. 반환값은 `{added, updated, removed, skipped}`.
- `search(query, source=None, limit=10) -> list[dict]` — `mount_fts` 질의.
  `[{source, path, excerpt, abs_path}]` 반환. 발췌는 `snippet()`으로 만든다.
- `main()` — CLI 진입점.

**`mneme/episodes.py`** — 에피소드 기록 공용 헬퍼

- `record(agent, tool, query, result_summary, session_id=None, **extra) -> None`

현재 `server.py`의 세 곳에 생 SQL `INSERT INTO episodes`가 중복돼 있다. CLI와 MCP가 같은
형식으로 기록해야 하므로 이를 하나로 모으고, 기존 세 곳을 이 헬퍼로 교체한다.

**`mneme/console.py`** — 콘솔 인코딩

- `force_utf8_stdout() -> None`

`log.py`, `growth.py`, `lint.py`가 각각 다른 방식으로 UTF-8 stdout을 강제하고 있다. CLI가
하나 더 늘어나는 시점이므로 여기서 추출하고 네 곳이 공유한다.

### 인터페이스

| 표면 | 형태 | 서버 필요 |
|---|---|---|
| CLI | `python -m mneme.recall "질의" [-s 소스] [-n 개수] [--agent 이름] [--no-sync]` | 불필요 |
| MCP tool | `recall(query, source=None, limit=10, agent="unknown")` | 필요 |
| Claude Code 스킬 | `/recall` (CLI를 호출) | 불필요 |

CLI가 서버 없이 동작하는 것이 핵심이다. `state.db`는 SQLite 파일이므로 직접 읽는다. 이는
`mneme.log`·`mneme.growth`·`mneme.lint`가 이미 따르는 패턴이다.

세 표면 모두 `episodes.record(tool="recall", ...)`로 기록한다. `agent`는 CLI에서 `--agent`
플래그(기본 `"cli"`), MCP에서는 인자로 받는다.

### 갱신

`recall` 호출마다 `sync()`를 먼저 실행한다. `--no-sync`로 끌 수 있다.

```
각 마운트의 include 글롭을 확장해 현재 파일 목록을 만든다.
mount_index와 대조한다:
  mtime과 size가 모두 같으면            → 건너뛴다
  다르면 읽어서 sha256을 비교한다
      해시가 같으면                     → 메타데이터만 갱신
      다르면                            → FTS 갱신 + 인덱스 갱신
  목록에 없는데 인덱스에 있으면          → 인덱스와 FTS에서 제거
  목록에 있는데 인덱스에 없으면          → 추가
```

파일 수십 개 규모에서 `stat()` 비용은 무시할 수준이고, LLM이 경로에 없으므로 실제 갱신도
밀리초 단위다. 별도 재색인 명령을 기억할 필요가 없고 결과가 항상 최신이다.

watcher는 확장하지 않는다. 저장소 루트를 recursive로 감시하면 의존성·빌드·`.git` 디렉토리의
이벤트를 받게 되고, watcher는 서버가 기동 중일 때만 동작한다.

### 에러 처리

| 상황 | 동작 |
|---|---|
| 마운트 루트가 없음 | 경고 후 해당 소스만 건너뜀. 기동 실패로 취급하지 않음 |
| include 글롭 결과가 루트를 벗어남 | 해당 경로 제외 |
| 설정 항목 형식 오류 | 경고 후 해당 항목만 건너뜀 |
| FTS 질의 문법 오류 | 안내 메시지 반환. 트레이스백을 노출하지 않음 |
| 테이블 없음 | `init_db()`가 생성 |

## 테스트

`tests/`의 기존 패턴(격리된 `WIKI_DIR`·`DB_PATH`)을 따른다.

1. `parse_mounts` — 정상 파싱 / 형식 오류 항목 건너뜀 / 존재하지 않는 루트 건너뜀
2. 경로 이탈 거부 — include 글롭이 루트 밖을 가리키면 결과에서 제외된다
3. `sync` — 최초 색인 / 미변경 건너뜀 / 변경분만 갱신 / 삭제된 파일 제거 / 신규 파일 추가
4. `search` — 매치 반환 / `source` 필터 / 발췌 포함
5. **마운트 동기화 중 `llm.generate_summary`가 한 번도 호출되지 않는다**
6. `recall` 1회 실행 후 `episodes`에 `tool="recall"` 행이 정확히 1건 생긴다
7. **마운트 동기화 전후로 `wiki_index`와 `wiki_fts`의 행 수가 변하지 않는다**

5번과 7번은 이 설계의 두 전제("LLM 비용 0", "기존 검색 무영향")를 각각 잠근다. 회귀로
깨지면 설계 근거가 무너지므로 반드시 포함한다.

버그 수정에 대한 테스트는 수정 전 코드에서 실패함을 확인한 뒤 커밋한다.

## 나중에 판단할 것

- 마운트 문서가 실제로 검색되는지 관측한 뒤, 쓰이지 않으면 마운트 대상을 줄인다.
- `recall`이 자주 쓰이면 그때 세션 시작 자동 검색을 검토한다.
- 성장 계층(`episode_reflect` 유입)은 recall 사용 데이터를 본 뒤 별도로 결정한다.

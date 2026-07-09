# 아키텍처 (Architecture)

> 구성요소·데이터 흐름·저장 위치를 설명합니다.
> 개념(왜/어떻게)은 [PRINCIPLES.md](PRINCIPLES.md), 사용 방법은 [USAGE.md](USAGE.md)를 참고하세요.

---

## 1. 한 장 그림

```
        ┌─────────────────────────── Claude Code (또는 다른 AI 에이전트) ───────────────────────────┐
        │  계획         작업 수행          기록·반성                          관찰                     │
        │  skill_suggest               episode_reflect           mneme_status / *_status            │
        └───┬───────────────┬──────────────────┬───────────────────────────────┬────────────────────┘
            │               │                  │                               │
            ▼               ▼                  ▼                               ▼
   ┌─────────────────────────────────────── Mneme MCP 서버 (localhost:8080) ───────────────────────────┐
   │                                                                                                     │
   │  wiki_search/get/inject/list/lint ──►  L2 Wiki (마크다운, watcher가 자동 인덱싱) ◄── 사람이 직접 편집  │
   │                                                                                                     │
   │  episode_reflect ──► Inner Loop ──► CIB(헌법 게이트) ──► L3 skills(성향)                              │
   │                                                                                                     │
   │  [백그라운드 스케줄러: 10분마다]  Outer Loop(생애주기·CI/BC)  ─► self_model(보정오차·성장속도·경보)     │
   │                                                                                                     │
   │  LLM 호출(검색 선별·병합 판단·요약·반성·coherence·독립평가) ──► 로컬 모델(Ollama, OpenAI 호환)          │
   └─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 구성요소 (코드 모듈)

| 모듈 | 역할 |
|------|------|
| `server.py` | FastMCP 서버. 13개 도구 등록, watcher·scheduler 기동/정지. |
| `memory.py` | SQLite 스키마·연결. 모든 테이블의 단일 진입점. |
| `wiki.py` | Wiki 마크다운 파일 읽기/쓰기/목록. |
| `index.py` | FTS5 전문검색 인덱싱·재인덱싱·요약. |
| `llm.py` | 로컬 LLM 단일 이음매(OpenAI 호환). 미가용 시 보수적 fallback. |
| `constitution.py` / `constitution.yaml` | L4 헌법 로더 + 헌법 정의. |
| `cib.py` | CIB 헌법 게이트(성향 변화 검증). |
| `skills.py` | L3 스킬 시드·추천·성향 갱신. |
| `lint.py` | L2 Wiki 무결성 기계 검사. |
| `outer_loop.py` | 요소 4. 스킬 생애주기 전이 + CI/BC 정산. |
| `self_model.py` | 요소 5. 자기 인식 4장치(M14~M17). |
| `scheduler.py` | APScheduler. 10분마다 Outer Loop → self_model 틱. |
| `watcher.py` | watchdog. Wiki 파일 변경 시 자동 재인덱싱. |

---

## 3. 데이터 흐름

1. **지식 입출력**: 에이전트가 `wiki_*` 도구로 Wiki를 검색/주입/검증합니다. 사람이 Obsidian
   등으로 직접 편집해도 watcher가 변경을 감지해 FTS5 인덱스를 자동 갱신합니다.
2. **학습(Inner Loop)**: `episode_reflect` → LLM 반성 생성 → CIB 게이트 → L3 `skills` 성향 갱신.
3. **정산(백그라운드)**: 스케줄러가 10분마다 `outer_loop.run_cycle()`(L3 생애주기) →
   `self_model.assess()`(L5 자기평가)를 순서대로 실행합니다. 각 단계는 새 에피소드가
   임계(20)에 도달해야 실제 실행됩니다(하이브리드 게이트).
4. **LLM 호출**: 모든 LLM 작업은 `llm.py`를 거쳐 로컬 런타임(OpenAI 호환 `/chat/completions`)으로
   갑니다. base URL만 바꾸면 Ollama / Foundry Local(NPU) / LM Studio 등으로 교체됩니다.

---

## 4. 파일·데이터 위치

```
mneme-mcp/
├── mneme/              # 코드 (server·memory·wiki·index·llm·constitution·cib·skills·lint·outer_loop·scheduler·self_model)
│   └── constitution.yaml  # 헌법
├── docs/                # USAGE / PRINCIPLES / ARCHITECTURE
├── memory/              # 런타임 (git 제외): state.db(SQLite), server.log
├── .env                 # 환경변수 (git 제외)
└── README.md / PROGRESS.md

E:/development/wiki/      # ★ 지식 베이스 (mneme 레포 바깥, 별도 관리)
└── {career,personal,tech,ai-llm}/ 각 카테고리: CLAUDE.md, index.md, log.md (OKF 예약), sources/, pages/
```

> Wiki를 레포 바깥에 두는 이유: 지식(데이터)과 성장 로직(코드)을 분리해, 코드 변경과 무관하게
> 지식을 독립적으로 관리·백업하기 위함입니다 (헌법 "데이터 주권").

---

## 5. SQLite 테이블

| 테이블 | 층/용도 |
|--------|---------|
| `wiki_fts` | L2 전문검색(FTS5) |
| `wiki_index` | L2 요약·태그·갱신자 |
| `facts` | L2 보조(구조화 사실) |
| `episodes` | L1 에피소드(작업·반성) |
| `working` | 세션 캐시(TTL 30분) |
| `skills` | L3 스킬 성향·상태 |
| `loop_cycles` | Outer Loop 정산 기록(CI/BC/전이) |
| `self_model` | L5 자기 지표 시계열 |

`memory/` 디렉터리는 git 추적에서 제외되며, `state.db`는 서버 최초 기동 시 자동 생성됩니다.

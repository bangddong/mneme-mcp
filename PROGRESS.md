# Mneme 개발 진행 기록 (구 hermes-mcp)

> 시스템이 상시 사용 가능한 수준이 될 때까지 이 파일로 작업 체크 / TODO / 에러를 기록한다.
> 매 작업 시작·완료 시 갱신한다.
> ⚠️ 이 파일은 public repo에 포함된다 — 개인 상황·일정 맥락은 적지 않는다 (개인 위키로).

**최종 갱신**: 2026-07-12 (진척 점검 — §3 완료 항목 체크 정리)

---

## 0. 한 줄 목표

LLM 가중치를 건드리지 않고 외부 레이어(기억·스킬·가치)만 갱신해 **스스로 성장하는 게이트웨이 두뇌**(MNEME). wiki는 `E:/development/wiki/`에서 별도 관리, mneme는 코드/성장 로직만 담당.

핵심 원리: `θ_eff = clip(θ_base + δ)`, `δ_{t+1} = δ_t + η·∇L`, **CIB(헌법 게이트) 통과 시에만 적용**.

---

## 1. 전체 로드맵 (MNEME 5요소 + 메모리 5층)

### 5층 기억
| 층 | 내용 | 저장소 | 상태 |
|----|------|--------|------|
| L1 Episodic | 에피소드(반성 포함) | `episodes` 테이블 | ✅ |
| L2 Semantic | 일반 지식 | wiki/ + FTS5 (+ 기계 lint 무결성 게이트) | ✅ |
| L3 Procedural | 스킬 성향 θ_eff (+ Outer Loop 생명주기) | `skills` 테이블 | ✅ |
| L4 Value | 헌법 | `constitution.yaml` | ✅ |
| L5 Identity | 자기 모델(보정오차·성장속도·난이도) | `self_model` 테이블 | ✅ |

### 5요소
| # | 요소 | 상태 |
|---|------|------|
| 1 | Constitution (3층 + 시나리오 K) | ✅ |
| 2 | CIB (헌법 게이트) | ✅ |
| 3 | Inner Loop (반성 → CIB → θ_eff 갱신) | ✅ |
| 4 | Outer Loop (스킬 승급/강등·CI/BC·N주기) | ✅ |
| 5 | 자기 인식 4장치 (셀프모델/Assessor/GrowthRate/내재동기) | ✅ (M17 축소) |

---

## 2. 완료 (Done)

- [x] MCP 서버 스캐폴딩 (fastmcp HTTP, 5 tool) — `b4ace27`
- [x] `.gitignore` + git init (wiki/memory/.env 제외) — `b4ace27`
- [x] `.env`에 ANTHROPIC_API_KEY 설정 (이후 로컬 LLM 전환으로 불필요해짐)
- [x] **Constitution (L4)**: `constitution.yaml` 3층 + 시나리오 TS01~04 — `b1923f7`
- [x] **CIB**: 프록시(δ clip ±0.2) + 방향(coherence 채점), seed_protected 강등 차단 — `b1923f7`
- [x] **Inner Loop (L3)**: `skills.py` 성향 학습, CIB 게이트 — `b1923f7`
- [x] tool 3개: `skill_seed` / `skill_suggest` / `episode_reflect` — `b1923f7`
- [x] 통합 검증: 학습(0.5→0.6 상승), 헌법 차단, 영속성 모두 통과
- [x] **L2 기계 lint** (`lint.py`): 형식/깨진링크(stub 제외)/고아/index동기화/stale 결정론적 검사. knot에서 흡수, 우리 스키마에 맞춤. `wiki_lint` MCP tool 추가 (tool 9개) — `2026-06-18`
- [x] **인덱싱 스캐폴딩 제외**: `is_content_page`로 `_index/_log/CLAUDE.md/.claude/` FTS 제외 (19→2 콘텐츠 페이지)
- [x] **콘솔 UTF-8**: lint CLI `sys.stdout.reconfigure(utf-8)` (em-dash 깨짐 해결)
- [x] **로컬 LLM 전환**(`llm.py`): Anthropic API → OpenAI 호환 로컬(기본 Ollama). `LLM_BASE_URL`/`LLM_MODEL` env. `is_available()` 추가. 런타임 죽어도 fallback으로 무중단. `anthropic` 의존성 제거, `httpx` 추가 — `2026-06-22`
- [x] **Outer Loop (요소 4)**: `outer_loop.py` 생명주기 상태머신(seeding→developing→active↔degrading→archived, seed_protected 강등·아카이브 면제) + CI(로컬 채점·선택적)/BC(결정론 성향안정도) `loop_cycles` 적재. `scheduler.py`(APScheduler 하이브리드: 10분 간격 × N에피소드 게이트). tool 2개(`outer_loop_run`/`outer_loop_status`, 총 11개). 합성 검증 통과(승급/강등/회복/면제/게이트/BC/CI graceful) — `2026-06-22`
- [x] **자기 인식 4장치 (요소 5 / L5)**: `self_model.py` — M14 self_model 시계열 테이블, M15 Phoenix Assessor(`llm.assess_episode` 독립 재평가 → 보정오차), M16 GrowthRate Regulator(급락/정체/과속 감지 → 로그+status 경보), M17 내재적 동기(성장 타깃 추천 + 난이도 스칼라, 축소 해석). 스케줄러 `_tick`이 run_cycle 뒤 `assess()` 호출(메타 레이어). tool 2개(`self_model_status`/`curriculum_suggest`, 총 13개). 합성 검증 통과(게이트/crash·stagnant 조절/보정오차 graceful/난이도/커리큘럼) — `2026-06-22`

### 2026-07-02 세션 (OKF 정합화 · 개명 · 웹훅 알림 · 플레이북)
- [x] **위키 OKF v0.1 정합화**: Google Open Knowledge Format 채택 (계기: Cole Medin 영상 분석).
  `_index/_log`→`index.md`/`log.md`(예약 파일, 불릿/날짜헤딩 형식), 전 문서 `type` frontmatter,
  `updated`→`timestamp`, 링크는 위키 루트 절대경로. lint.py/wiki.py/wiki_agent.py 동기화.
  검증: 기계 lint 오류 0 + wiki_agent 조립함수 단위테스트 통과. wiki `d1431fd`, 상세 = DECISIONS 07-02 #1
- [x] **개명 hermes→mneme**: repo(GitHub 리다이렉트)·패키지·서버명·툴 프리픽스(`mcp__mneme__*`)·
  `.mcp.json`×4·`~/.claude.json`·시작프로그램·`.claude` 메모리 전부. Nous "Hermes Agent" 언급은 보존.
  서버 재기동 후 handshake `"name":"mneme"` 확인. `d181620`+`4e6fe06`, 상세 = DECISIONS 07-02 #2
- [x] **Discord 봇 동결 + 소비 프로젝트 연계 방향 확정**: 도메인 시드 빌드타임(본류·백로그) /
  웹훅 복습 알림(구현) / 모바일 캡처(보류·Claude 커넥터 방향). 상세 = DECISIONS 07-02 #3
- [x] **복습 알림 구현**: `notify.py` + `outer_loop.run_cycle` 훅. degrading 전이 → Discord 웹훅.
  `DISCORD_WEBHOOK_URL` 빈값=비활성, 실패해도 정산 안 막음. 경로별 테스트 통과 — `8db5678`
- [x] **PLAYBOOK.md**: 상황별 사용자 매뉴얼 (시스템 지도/학습/인제스트/검색/알림/점검/운영/확장/트러블슈팅/치트시트) — `2babd85`
- [x] lint 부수 수정: `.git` 디렉토리가 카테고리로 잡히던 버그 (닷 디렉토리 제외)

### 2026-07-03 세션 (웹훅 연결 · 백업 정리 · 연동 가이드)
- [x] Discord 웹훅 연결 + 테스트 발송 성공, `~/.claude.json.bak-mneme` 삭제 (★ 다음 세션 ①② 완료)
- [x] skills 소실 발견·재시드: `study-k8s-ingress` 복원 + `study-k8s-configmap-secret` 신규 (상세 = 에러 로그 07-03)
- [x] **`docs/INTEGRATION.md` 신설**: raw 데이터 쌓는 소비 프로젝트의 mneme/위키 활용 가이드 —
  4저장소 개념 지도(repo/위키/스킬/에피소드), 세션 수명주기, 위키 승격 기준 3문, 유스케이스 5개
  (기본 루프/빌드타임 시드/도메인 카테고리/역량 추적/크로스 프로젝트), 안티패턴, 점검 체크리스트.
  PLAYBOOK 헤더·§8에 크로스링크
- [x] **소비 프로젝트 담당자용 가이드**: 소비 프로젝트 측 `.claude/docs/mneme-guide.md` (클라이언트 관점 계약 문서, 해당 프로젝트 CLAUDE.md 표에 등재. ⚠️ 해당 repo 미커밋 상태 — 사용자 커밋 대기)
- [x] **위키 `guide/` 카테고리 신설**: 시스템 상황별 가이드 4편 (존댓말, type: doc, lint 오류 0) —
  system-architecture / usage-guide / writing-format / operations. 루트 index.md·CLAUDE.md 등재.
  발견: lint가 코드 블록 안 링크도 검사 → 예시 링크엔 stub 마커 필요 (writing-format §2에 명문화)

### 2026-07-07 세션 (wiki_inject 규약 강제 — 지시→코드)
- [x] **계기**: 소비 프로젝트發 inject 페이지가 규약 4중 위반(`type: knowledge`·pages/ 직속·index 형식·log 누락,
  wiki `048f879`로 수동 교정). 규약이 docstring 지시뿐이라 소비 프로젝트가 늘수록 재발 구조 → 코드 게이트화.
- [x] **`lint.py`**: 단일 페이지 형식 검사 `lint_page_text()` 분리(lint_wiki와 inject 게이트 공유) +
  **type 어휘 검사 신설**(`TYPE_VOCAB`, wiki CLAUDE.md와 동기) + frontmatter 보존용 `split_frontmatter()`.
- [x] **`wiki.py`**: `update_reserved_files()` — 생성/병합 시 index.md 불릿·log.md 날짜헤딩 항목을
  **코드가 결정적으로** 추가(호출자 LLM에게 안 맡김). 예약 파일 직접 inject는 거부.
- [x] **`wiki_inject` 게이트**: 경로 규칙(카테고리 존재·pages/ 직속 금지 — 기존 서브폴더 있을 때) +
  OKF 형식 위반 시 **쓰지 않고 `action: "rejected"` + errors 반환**(호출자가 고쳐 재호출).
  병합은 frontmatter 코드 보존(본문만 LLM) + 병합 후 lint 실패 시 원문 유지(`merge_rejected`)
  → **에러 로그 07-06 개선 ①② 완료**. integration 템플릿에 계약 반영.
- [x] 검증: 임시 위키 12케이스 통과(거부 5종/생성+예약파일 자동갱신/병합 게이트가 실제 LLM의
  Summary 누락 병합을 차단·원문 보존/같은 날 log 헤딩 누적). 실위키 전체 lint 오류 0.
- [x] 잔여 해소(07-12): 연동 프로젝트의 구버전 규약 블록을 v2로 갱신 — 구 블록 제거 후 `install.py` 재실행(연동 프로젝트 별도 브랜치 커밋). ※ install.py는 기존 `## mneme` 블록이 있으면 skip이므로 갱신 시 구 블록 먼저 제거 필요.

### 2026-07-09 세션 (public 전환 준비)
- [x] **개인 맥락 분리**: docs(DECISIONS/PROGRESS/INTEGRATION/PLAYBOOK)의 개인 상황 근거를
  개인 위키로 이관하고 예시를 일반화(`myapp`). 분리 기준 = *"fork한 타인에게도 유효한가"*
  (DECISIONS 헤더 07-09 노트). 이후 이 파일에 개인 상황·일정 맥락 금지(상단 규약).
- [x] **mneme-start.bat 경로 주입화**: `.venv` → `MNEME_PYTHON` env → PATH `python` 폴백 체인
  (파이썬 절대경로 하드코딩 제거). 해석 체인 검증 완료.
- [x] **Docker 지원**: `Dockerfile` + `compose.yaml`(ollama 사이드카 + 볼륨) + `.dockerignore`.
  이미지 빌드 + 컨테이너에서 MCP initialize 200 OK 검증 (Linux 동작 확인).
- [x] **히스토리 squash**: 개인 맥락이 남은 과거 커밋을 제거하고 단일 커밋으로 재구성
  (구 히스토리는 로컬 `private-history` 브랜치 + bundle 백업) → public 전환 준비 완료.
- [x] **PUBLIC 전환 완료**: mneme-mcp·wiki-agent public, 위키는 private 유지 (엔진/데이터 분리 — DECISIONS 07-09 #1)
- [x] **visibility 필드**: OKF 선택 필드 `visibility: public|shared|private`(생략=private) —
  lint 어휘 검사(`VISIBILITY_VOCAB`) + wiki CLAUDE.md·guide/writing-format 규약 동기. 마킹만, 파이프라인은 later (DECISIONS 07-09 #2)
- [x] **bearer 인증**: `MCP_AUTH_TOKEN` env → fastmcp `StaticTokenVerifier`(비우면 무인증=기존 동일,
  `이름:토큰` 복수 지원). 격리 인스턴스 검증: 무토큰 401 / 오토큰 401 / 유효 200.
  ※ 라이브 서버는 재시작해야 반영 (07-07 wiki_inject 게이트 변경도 동일)

---

## 3. TODO (우선순위 순)

### ★ 다음 세션 (2026-07-03 이어서)
- [x] **Discord 웹훅 연결 완료(07-03)**: `.env`에 URL 등록 + 서버 재시작 + `notify_degrading` 테스트 발송 성공(sent: True)
- [x] **`mcp__mneme__*` 툴 정상 확인(07-03)**: mneme_status/growth_log/outer_loop_status 응답 정상
- [x] 백업 `~/.claude.json.bak-mneme` 삭제 완료(07-03)
- [x] **skills 재시드 완료(07-03)**: `study-k8s-ingress` 복원 + `study-k8s-configmap-secret` 신규 (07-12 점검: seeding 2건 확인). 소실 원인은 여전히 미상 — 재발 시 추적
- [ ] **강사 모드 학습 재개** — 스킬·에피소드가 쌓여야 Outer Loop(20 에피소드 게이트)·복습 알림·curriculum이 실제로 작동 시작. k8s stage 이어가기 또는 `curriculum_suggest()` 추천
- [ ] (위키 쌓인 뒤) **소비 프로젝트 도메인 시드 반자동**: 위키 concept 페이지 훑어 빌드타임 시드 PR — PLAYBOOK §8 프롬프트
- [ ] (보류 — 수요 검증 시) mneme 인증(bearer) + Claude 모바일 커스텀 커넥터 (Tailscale Funnel)
- [ ] (여전히 백로그) wiki-agent `calendar_ingest` Phase 2 — Google Calendar 자동 인제스트

### 다음 마일스톤 — "상시 사용 가능" 최소선
- [x] **시작 자동화**: 부팅(로그인) 시 콘솔 없이 자동 기동. `mneme-start.bat` + 시작프로그램 폴더 `mneme-startup.vbs`(콘솔 숨김 런처). 로그는 `memory/server.log`. 검증: VBS→bat→서버→MCP handshake HTTP 200 OK
- [x] **Claude Code 연결**: `~/.claude.json`의 E:/development 프로젝트 mcpServers에 mneme(http) 등록 + 레포에 휴대용 `.mcp.json`. 서버 tool 8개 노출 확인. ※ MCP는 Claude Code **재시작 시 연결**됨 — 현재 실행 중 세션엔 즉시 반영 안 됨
- [x] **wiki 경로 실연동**: `WIKI_DIR=E:/development/wiki` 로 기존 wiki 인덱싱 검증 (lint으로 확인)
- [x] **L2 기계 lint**: knot 흡수 (위 Done 참조)
- [~] 콘솔 인코딩: lint CLI는 UTF-8 강제 완료. **server.py print/로그는 아직** (TODO 잔여)
- [ ] **wiki stub 규약 문서화**(사용자 승인 후): `wiki/CLAUDE.md`·`.claude/skills/lint.md`에 "미래 페이지는 같은 줄 `<!-- stub -->`" 추가 + `llm-wiki-pattern.md`의 `(구축 예정)` 링크 2건에 마커 부여 (현재 깨진 링크 오류로 잡힘)

### Outer Loop (요소 4) — ✅ 완료 (위 Done 참조)
- [x] 스킬 생명주기 상태머신 (seeding→developing→active↔degrading→archived)
- [x] N 에피소드 주기 스케줄러 (APScheduler 하이브리드)
- [x] Coherence Index(CI) / Behavioral Consistency(BC) 추적 (`loop_cycles`)
- [x] 스킬 승급/강등 기준 (성공률·사용빈도)
- [ ] 잔여: 미사용 사이클(stale) 정밀 추적은 간이 구현(윈도우 미등장=1). self_model 합류 시 정밀화.

### 자기 인식 4장치 (요소 5) — ✅ 완료 (위 Done 참조)
- [x] M14 셀프모델: `self_model` 테이블 (보정오차·성장속도·난이도, 시계열)
- [x] M15 Phoenix Assessor: 수행자와 분리된 독립 평가자 (`llm.assess_episode`)
- [x] M16 GrowthRate Regulator: 급락/정체/과속 감지 → 로그+status 경보
- [x] M17 내재적 동기(축소): 성장 타깃 추천 + 난이도 스칼라 (`curriculum_suggest`)
- [ ] 잔여: M17 본격화(난이도→실제 태스크 난이도 매핑·지식 갭 탐지)는 행동 데이터 축적 후

### 인프라 / 기타
- [ ] **기동 시간 단축**: `reindex_all()`이 매 기동마다 전 페이지 LLM 요약 재생성(~4분). mtime/해시 비교로 미변경 페이지 스킵
- [ ] **remote 연결**: GitHub private repo 생성 + push (사용자 요청 시)
- [ ] 오케스트레이터: `execute_task`로 서브에이전트 spawn/조율 (별도 결정 필요)
- [ ] 단위 테스트 작성 (pytest) — 현재 수동 검증만

---

## 4. 에러 / 이슈 로그

| 날짜 | 증상 | 원인 | 해결 |
|------|------|------|------|
| 06-17 | MCP tool 호출 시 "Missing session ID" | MCP는 initialize→session→call 순서 필요 | initialize로 mcp-session-id 획득 후 헤더에 포함 |
| 06-17 | `load_dotenv()` heredoc 실행 시 AssertionError | stdin 실행은 파일 프레임 없어 `find_dotenv` 실패 | `load_dotenv("절대경로/.env")` 명시 |
| 06-17 | `python -m mneme.server &` (Bash &) 백그라운드가 죽은 줄 알았으나 살아있음 | Bash 툴 `&`는 유지됨. run_in_background 서버가 포트 충돌(Errno 10048) | 포트 점유 확인 후 기존 프로세스 정리 |
| 06-17 | print에서 `—`(em-dash) UnicodeEncodeError (cp949) | Windows 콘솔 인코딩 | 출력 ASCII화 / 추후 UTF-8 강제 (TODO) |
| 06-18 | lint CLI 동일 em-dash cp949 오류 | 위와 동일 | `main()`에서 `sys.stdout.reconfigure(encoding="utf-8")` — server.py는 잔여 |
| 07-03 | 서버 재시작 후 8080 바인딩까지 ~4분 걸려 죽은 걸로 오인 | 기동 시 `reindex_all()`이 콘텐츠 페이지마다 `llm.generate_summary`(로컬 Ollama) 호출 — 변경 여부 무관하게 매번 재생성 | 대기하면 정상 기동. 개선 TODO: mtime/해시 비교로 미변경 페이지 스킵 |
| 07-03 | skills 테이블 빈 상태 발견 (`study-k8s-ingress` 소실) | 원인 미상 — episodes/loop_cycles 온전, 코드 삭제 경로 없음, 중복 state.db 없음 | 재시드 예정. 재발 시 원인 추적 |
| 07-06 | `wiki_inject` 병합이 문서 훼손: frontmatter `---` 구분자 삭제 + 한국어 마침표가 전각(。)으로 치환 → lint "frontmatter 없음" 에러 | 로컬 LLM(Ollama) 병합 프롬프트가 원문 형식을 보존하지 않음 (qwen 계열 중국어 토큰 누출 추정) | 파일 직접 수정으로 복구. ①② **07-07 구현 완료**(병합 후 lint→실패 시 원문 유지 `merge_rejected` / frontmatter 코드 보존). ③ diff 병합 옵션은 잔여 |

---

## 5. 비자명 결정 기록

- **성장 단위 = 스킬 성향(propensity)**. 선호도/실수기록 방식은 보류, 스킬 신뢰 점수 [0,1]로 단일화.
- **CIB 방향 검사**가 진짜 원칙, δ clip(±0.2)은 프록시(보조 안전 플로어). MNEME 자기교정 사례와 동일.
- **반성 로직은 `reflect.py` 대신 `llm.py`에 통합** (같은 haiku 호출 계열, 응집도).
- **mneme는 성장 두뇌만**. 에이전트 spawn/조율(오케스트레이터)은 이번 범위 제외.
- haiku 채점/반성 실패 시 fallback: coherence는 0.95 경계 반환(보수적), 반성은 빈 필드.
- **knot(netwaif/knot) 흡수 원칙**: frontmatter/폴더 규약은 우리 카테고리 격리형이 더 정교 → 채택 안 함. knot에서 *결정론적 기계 lint*만 흡수하고 규칙은 우리 스키마(title/updated/tags + Summary/Details/Sources/Related)에 맞춤. stub 마커(`<!-- stub -->`)는 미래 페이지 오탐 방지용으로 채택.
- **lint = L2 무결성 게이트**. CIB가 L3 스킬을 지키듯 lint가 L2 위키를 지킨다. 내용 모순(의미) 검사는 AI 영역이라 기계 lint에서 제외(`judge_conflict`/lint.md 담당).
- **lint은 순수 파이썬**(API 키 불필요). `sources/`는 검색 가치가 있어 콘텐츠로 인덱싱 유지, 검사는 `pages/**`만.
- **LLM 백엔드 = 로컬(기본 Ollama), API 종량제 폐기**. 근거: Pro/Max 구독으로 표준 SDK `messages.create`를 청구하는 공식 경로 없음(정적키·OAuth 모두 API 조직 과금). Outer Loop는 에이전트 미접속 백그라운드라 MCP sampling도 불가 → 로컬만이 무과금·상시가동. `llm.py`를 **OpenAI 호환 `/chat/completions`** 이음매로 작성해 base URL 교체만으로 Ollama(CPU/GPU)·Foundry Local(NPU)·LM Studio 갈아끼움. 사용자 하드웨어에 NPU 있으나 Ollama는 NPU 미사용(CPU/GPU) — NPU 쓰려면 `LLM_BASE_URL`만 Foundry Local로.
- **Outer Loop = 하이브리드 트리거 주기 정산**. 시간(APScheduler 10분) × 이벤트(누적 N=20 에피소드) 둘 다 만족 시 실행. 상태 전이는 propensity를 안 바꾸므로(state만) CIB 재호출 불요. **seed_protected는 강등·아카이브 모두 면제**(헌법 "정체성 보존" — cib의 강등 차단과 동일 원칙).
- **CI = 로컬 채점·선택적·graceful**(LLM 미가용 시 None). **BC = 결정론 성향안정도**(1 − 직전 사이클 대비 평균|Δpropensity|). 둘 다 `loop_cycles`에 적재 → L5 self_model·M16 GrowthRate 신호원.
- **자기 인식(L5)은 Outer Loop 위 메타 레이어**. 스케줄러 틱이 run_cycle(L3) 뒤 `self_model.assess()`(L5)를 이어 호출. 자기평가도 N에피소드 게이트.
- **M15 = 수행자와 분리된 독립 평가자**(헌법 원칙 "평가자 분리" 집행). `reflect_episode`(수행자 자기반성)와 다른 프롬프트/역할로 결과를 재채점. **보정오차 = 자기점수 ↔ 독립평가 괴리**. 독립평가 실패는 안전점수가 아니라 None을 반환해 보정오차 오염 방지.
- **M16 알림 = 로그 + status 플래그**(로컬 네이티브, 외부 알림 채널 없음). **M17 = 축소 해석**(성장 타깃 추천 + 난이도 스칼라). mneme는 스스로 태스크를 생성하지 않는 게이트웨이라 본격 auto-curriculum(난이도→태스크 매핑)은 보류.
- **자기평가도 LLM 미가용 시 graceful**: 보정오차만 None, 성장속도·조절·난이도·커리큘럼은 결정론으로 계속.
- **성장 조치 큐 = 자기평가의 출력 라우팅**. self_model.assess가 감지한 문제(M16 비정상 regulation / M15 보정오차>CAL_BAD / M17 at-risk 스킬)를 매 사이클 로그에 흘리지 않고 `growth_actions`에 박제. **dedup_key로 같은 문제 중복 누적 방지**(재감지 시 seen_count만↑), **정상화되면 emit이 자동 resolved**(관리 네임스페이스 regulation:/calibration/skill: 한정 — 사람이 만든 항목은 안 건드림). open 목록 = 지금 손볼 것. CLI `python -m mneme.growth`, tool `growth_log`/`growth_resolve`.
- **에피소드 agent 기록**: wiki_search·episode_reflect도 `agent` 인자(기본 "unknown") 수신 → 호출 에이전트 식별. 기존 하드코딩 "unknown" 빈틈 해소. (호출 측에서 agent를 넘겨야 실제 식별됨.)

### 참고 — 오케스트레이터 (이번 제외, 나중 레퍼런스)
netwaif/**multi-agent-starter** 패턴: `_shared/backends.json`(역할→모델/연결방식 매핑) + `call_worker.sh`(디스패처)로 벤더 독립. 토폴로지 4종(Pipeline / Fan-out·Fan-in / Expert Pool / Producer-Reviewer). 철학 "file-as-memory, runtime state 0". → mneme에 워커 spawn/조율을 붙일 때 이 어댑터 패턴을 레퍼런스로. (mneme는 상시 서버+SQLite라 철학은 반대이나 어댑터/토폴로지 설계는 참고 가치.)

---

## 6. 현재 코드 맵

```
mneme/
├── server.py        MCP 진입점 + tool 11개 + 스케줄러 기동/정지
├── memory.py        SQLite (wiki_fts/wiki_index/facts/episodes/working/skills/loop_cycles/self_model)
├── wiki.py          마크다운 R/W
├── index.py         FTS5 인덱싱/검색/summary
├── llm.py           로컬 LLM(OpenAI 호환, 기본 Ollama): 후보선별/충돌판단/요약/coherence채점/반성 + is_available
├── watcher.py       watchdog 파일 감시
├── constitution.py  헌법 로더
├── constitution.yaml 3층 헌법 + 시나리오 K
├── cib.py           헌법 게이트
├── skills.py        스킬 성향 θ_eff 관리 (Inner Loop)
├── lint.py          L2 무결성 게이트 (결정론적 기계 lint, 순수 파이썬)
├── outer_loop.py    Outer Loop(요소 4): 생명주기 상태머신 + CI/BC 정산
├── scheduler.py     APScheduler 하이브리드 트리거 (10분 × N에피소드 게이트) → run_cycle + assess
├── self_model.py    자기 인식 4장치(요소 5 / L5): M14~M17 (보정오차·성장조절·난이도·커리큘럼)
├── log.py           접근/이력 로그 조회 (옵저버빌리티). CLI `python -m mneme.log` + tool episode_log 공유
└── growth.py        성장 조치 큐(backlog). 자기평가가 감지한 문제를 박제 → 사용자 해소. CLI `python -m mneme.growth` + tool 2개

MCP tool (16): wiki_search, wiki_get, wiki_inject, wiki_list, wiki_lint,
               outer_loop_run, outer_loop_status,
               self_model_status, curriculum_suggest,
               mneme_status, skill_seed, skill_suggest, episode_reflect, episode_log,
               growth_log, growth_resolve
```
lint 단독 실행: `python -m mneme.lint [category]`  (errors 있으면 exit 1)

LLM 런타임: `LLM_BASE_URL`(기본 `http://localhost:11434/v1`), `LLM_MODEL`(기본 `qwen2.5:7b`). `ollama pull <model>` 후 기동. 미기동 시 fallback으로 무중단.
Outer Loop 주기: `OUTER_LOOP_INTERVAL_MIN`(기본 10). 수동 정산: MCP tool `outer_loop_run(force=true)`.

로그 조회: `python -m mneme.log [-n N] [-a agent] [-t tool]` (CLI) / `episode_log` (MCP tool). episodes 테이블 기반 — 어떤 에이전트가 어떤 tool로 무엇을 묻고 뭐라 답했나. 토큰/지연은 미기록(로컬 Ollama 무과금).
성장 조치 큐: `python -m mneme.growth [--all] [--resolve ID --note "..."]` (CLI) / `growth_log`·`growth_resolve` (MCP tool). 자기평가가 감지한 문제 백로그 — open 목록을 주기적으로 확인·해소.

새 프로젝트 연동: `integration/` 키트. `python install.py --project <p> --agent <name> --category <cat> --prefix <pre>` → 대상 `.mcp.json`에 mneme 머지(기존 서버 보존) + CLAUDE.md에 사용 규약 블록 추가(멱등). 크로스플랫폼 단일 스크립트(Python이 mneme 필수 의존이라 OS별 셸 스크립트 대신 install.py 하나로 Win/Linux/macOS 커버 — 두 스크립트 분기/인코딩 이슈 회피). mneme는 선택적 의존(서버 꺼지면 도구만 안 보임).

실행: `python -m mneme.server` → http://localhost:8080/mcp

---

## 7. 나중 고려 — 전사/다중 사용자 확장 (지금은 1인 집중, 기록만)

> 2026-06-25 논의 기록. **현재는 채택하지 않음.** 로컬 단일 사용자(SQLite + 로컬 Ollama)에 집중.
> "이미 성장한 두뇌를 여러 사람이 공유" 시점이 오면 아래를 검토한다.

**문제**: SQLite는 파일 기반·단일 머신·단일 라이터(쓰기 시 DB 전체 잠금). 다중 사용자/머신이 동시에 같은 두뇌에 쓰면 충돌·머신 경계 불가·트랜잭션 무결성 취약.

**핵심 결정은 "DB 종류"가 아니라 "두뇌를 어떻게 나눌 것인가"** (`θ_eff = clip(base + δ)`의 FedPer 뿌리가 이 질문용):

| 모델 | 구조 | 트레이드오프 |
|------|------|------|
| A. 단일 공유 두뇌 | 모두가 같은 δ에 R/W | 집단 성장 복리 최대 ↔ Tay 오염 전파·개인화 소실 |
| **B. base 공유 + δ 개인 ★** | base는 중앙 동기화, δ는 사용자/팀별 분리 | 공통지식 복리 + 개인맥락 보존 + 오염 격리. **프레임워크 철학(Tay 흉터=오염격리)에 부합** |

→ B 권장. `skills` 테이블의 `base`/`delta`/`propensity` 분리가 이미 포석.

**DB만이 아닌 동반 과제**:
1. `memory.py` 연결 계층 추상화(SQLAlchemy/Postgres 드라이버) — 스키마는 거의 그대로 이식.
2. 위키(L2): git remote 공유로 충분하거나, 규모 커지면 pgvector(`nomic-embed-text` 이미 설치됨).
3. CIB 게이트: 동시 δ 갱신 트랜잭션 보호.
4. LLM 백엔드: 전사 동시부하는 로컬 Ollama 1대 불가 → vLLM 등 공용 추론 서버.

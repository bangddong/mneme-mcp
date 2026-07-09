# 결정 기록 (Decisions Log)

> 시스템(mneme + 위키 + 게이트웨이)에 대한 굵직한 설계 결정을 시간순으로 남긴다.
> **이 파일이 진실의 원천(source of truth)이다.** Claude Code의 `.claude` 자동 메모리는
> 하네스 전용·경로 고정이라 이식 불가 → 거기엔 "이 파일을 봐라"는 포인터만 둔다.
> 어떤 AI·머신이든 이 repo를 clone하면 결정 맥락을 그대로 복원할 수 있어야 한다.
>
> **2026-07-09**: repo public 전환에 따라 과거 항목에서 개인 상황에 기반한 판단 근거
> (착수 트리거·수요 평가 상세)를 개인 위키로 분리했다. 분리 기준 — *"fork한 타인에게도
> 유효한 근거인가"*. 여기 남은 것은 설계 결정과 그 기술적 근거다.

---

## 2026-07-02

### 1. 위키를 OKF(Open Knowledge Format) v0.1에 정합 (확정)
Google이 Karpathy LLM Wiki 패턴을 표준화한 [OKF 스펙](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(Apache 2.0, 2026-06 발표)에 위키 구조를 맞췄다. 계기: Cole Medin 분석 영상(2026-07-02).

**Why:** "읽기=직접" 원칙(아래 2026-06-29 #2)의 연장 — 위키를 읽는 쪽이 우리 에이전트만이 아니라
**어떤 OKF consumer라도** 되게 한다. 표준 정착 여부와 무관하게 (a) type 기반 분류,
(b) 예약 인덱스/로그 파일, (c) 절대경로 링크는 어떤 표준이 이겨도 공통분모다.
기존 구조(_index/_log, frontmatter)가 이미 OKF와 90% 일치해 전환 비용이 낮았다.

**적용 범위:**
- 예약 파일: `_index.md`→`index.md`(불릿 `* [제목](경로) - 설명`), `_log.md`→`log.md`(`## 날짜` 헤딩, 최신 위)
- frontmatter: 전 문서 필수 `type` + `title`/`description`/`timestamp`(구 `updated`)/`tags`
- type 어휘: concept(pages)·source(sources)·lesson·roadmap·schema(CLAUDE.md)·doc — 위키 루트 CLAUDE.md에 표
- 링크: 위키 루트 절대경로 마크다운 링크(`/tech/pages/...md`). `[[...]]` 위키링크는 레거시(lint는 계속 허용), 미존재 페이지는 `<!-- stub -->` 규약 유지
- 코드 동기화: mneme `lint.py`(type/timestamp/마크다운 링크 검사)·`wiki.py`(index/log 스캐폴딩 제외),
  wiki-agent `wiki_agent.py`(OKF frontmatter 조립·불릿 인덱스·날짜헤딩 로그·Related 절대경로+stub)

**파급:** 위키 번들을 그대로 남에게 공유하거나 남의 OKF 번들을 이식받을 수 있는 기반.
wiki_inject를 쓰는 프로젝트는 `integration/CLAUDE-mneme.md.template`의 갱신된 페이지 형식을 따를 것.

### 2. 개명: hermes → mneme (확정)
**Why:** "hermes"는 유명 제품과 이중 충돌 — Nous Research **Hermes**(LLM 시리즈, 아래 06-29 #4에서
비교한 그 제품)와 Meta **Hermes**(React Native JS 엔진). 검색성·정체성 모두 손해.
새 이름 **mneme**는 그리스 '기억의 무사' — 원래 이 시스템의 성장 두뇌 내부 코드네임(MNEME)이라
개념 연속성이 있고, 유명 툴 충돌이 없다.

**적용 범위:** GitHub repo `bangddong/hermes-mcp`→`mneme-mcp`(리다이렉트 유지),
로컬 `E:/development/mneme-mcp`, 파이썬 패키지 `hermes/`→`mneme/`, MCP 서버명
`FastMCP("mneme")` — **툴 프리픽스가 `mcp__hermes__*`→`mcp__mneme__*`로 바뀌므로**
각 프로젝트 `.mcp.json`의 서버 키와 CLAUDE.md 연동 블록도 함께 갱신(위키·연동 프로젝트).
시작 스크립트 `mneme-start.bat` + `shell:startup`의 `mneme-startup.vbs`.
과거 결정 기록 속 Nous "Hermes Agent" 언급은 타사 제품명이므로 보존.

### 3. Discord 봇 동결 + 소비 프로젝트 연계 방향 (확정)
**봇 평가:** wiki-agent Discord 봇은 배포하지 않고 동결한다(코드는 보존).
근거: ①조회 경로가 "읽기=직접" 원칙(06-29 #2) 위반 — 약한 로컬 LLM이 합성한 답은
Claude+MCP 직접 조회의 하위 호환. ②인제스트 주 경로는 이미 Track 1. ③실수요 미검증
(수요가 검증되지 않은 기능은 만들지 않는다). 살아있는 가치는 봇이 아니라
`calendar_ingest.py`(자동 누적) 쪽.

**소비 프로젝트 연계 3방향 (우선순위순):**
1. **위키 → 도메인 데이터 빌드타임 시드** (본류, 백로그): OKF frontmatter(type/tags)
   기반으로 concept 페이지 → 소비 프로젝트의 시드 SQL/JSON 생성.
   "런타임 앱→mneme 금지, 빌드타임 시드만" 원칙 준수. 지금은 위키가
   얇아 보류 — 반자동(Claude가 위키 훑어 시드 PR)으로 시작 예정.
2. **degrading → Discord 웹훅 복습 알림** (구현됨, 이 커밋): `mneme/notify.py` +
   `outer_loop.run_cycle` 훅. `DISCORD_WEBHOOK_URL` 비우면 자동 비활성(Loki 패턴).
   봇 없이 웹훅 URL 하나로 "폰으로 오는 접점"을 회수. 실패해도 정산을 막지 않음.
3. **모바일 캡처** (보류 — 수요 검증 시 착수): 구현은 봇 부활이 아니라
   **mneme 인증(bearer) + Claude 모바일 커스텀 커넥터(Tailscale Funnel HTTPS)**
   방향 — 폰에서도 "읽기=직접" 유지.

착수 시점·수요 평가의 개인 맥락은 개인 위키(2026-07-09 분리)에 있다.

---

## 2026-06-29

### 1. 투트랙 아키텍처 (확정)
사용자는 위키를 **직접 채우지 않는다.** 두 경로로 나뉜다:

- **Track 1 — 성장/쓰기 (메인):** 각 소비 프로젝트가 작업 중 mneme에 MCP로 붙어
  `wiki_search` / `wiki_inject` / `episode_reflect(agent=...)`를 호출 → 위키(L2) 누적 + 스킬 학습.
  규약은 `integration/install.py`가 프로젝트 CLAUDE.md에 설치(현재 1개 프로젝트 연동).
  **규약=지시 기반(강제 아님)** — 각 프로젝트의 Claude가 따르는 만큼 쌓인다.
- **Track 2 — 조회/읽기 (보조):** Discord 봇(`E:/development/wiki-agent`)은 사람이 폰에서
  "X 뭐였지?" 하고 **들여다보는 창**. 같은 위키를 보므로 Track 1 누적분을 자연히 조회.
  봇은 성장 루프와 무관. (원래 ingest 중심이라 조회 경로 강화는 추후 과제)

### 2. 설계 원칙 — 읽기=직접, 쓰기=통제
다른 AI가 위키를 **요청하면 마크다운을 직접 읽게** 한다(약한 로컬 LLM 경유 X — 품질 천장·손실·환각 방지).
로컬 LLM은 **쓰기(인제스트) 전담 "사서"** 역할만. 검색도 답 생성이 아니라 원문 청크 반환(FTS5/grep).

### 3. 로컬 LLM 인제스트 모델 = qwen2.5:14b
정확도 우선(인제스트는 비동기라 느려도 OK). 벤치(전파속성 7개): 14b만 7개 정확(113초),
7b·9b는 7번째 오류(30초). HW: Ryzen5 5600X / RAM 32GB / Radeon RX Vega 8GB(GPU 정상).
모델 저장소는 `D:\ollama\models`로 이전(`OLLAMA_MODELS`).

### 4. Nous "Hermes Agent" 비교 — 갈아타지 않고 패턴만 차용
동명의 별개 제품(Nous Research). 핵심(스킬·에피소드·로컬Ollama·자기모델)은 수렴하나 **위상이 다름**:
Nous=독립형 1인 비서 + 멀티플랫폼 게이트웨이(20+), 우리=프로젝트들이 꽂는 **MCP 공유 두뇌**.
우리만의 강점 = **Constitution + CIB 거버넌스**(δ clip·정합성 게이트로 *진화 자체*를 통제) — Nous엔 없음
(걘 실행안전만: 명령승인·샌드박스). 사용자 목표(크로스-프로젝트 자동 누적)는 MCP 허브 위상이라야 가능 →
**갈아타기 비추(목표·강점 상실). 좋은 패턴만 차용.**

- **차용 ★★★ — `SKILL.md`(agentskills.io) 표준:** Anthropic Claude 스킬과 **동일 표준**.
  mneme 자체 스킬 스키마 대신 채택하면 Claude Code 호환 + 공유 가능.
  ⚠️ "스킬" 의미가 다름: SKILL.md=**절차 콘텐츠**, mneme `θ_eff`=**숙련도 스칼라** → 충돌 아닌 **상보**.
  `name`으로 연결(절차 정의 + 숙련도 학습). 마이그레이션은 본체 변경이라 PROGRESS.md 먼저, 신중히.
- **차용 ★ (보류) — 멀티플랫폼 게이트웨이:** 수동입력용이라 자동누적 목표와 무관. 패턴만 참고
  (어댑터→세션스토어→코어 / allowlist·DM페어링 / interrupt·queue·steer / systemd 서비스).

### 5. 진실의 원천은 이식 가능한 레이어에 (메타 결정)
Claude Code `.claude/projects/.../memory/`는 **하네스 전용 + 경로 고정 = 이식 불가**.
"성장 두뇌"의 **자기 메타 결정은 자기 이식 레이어(git 마크다운: 이 파일 / 위키)에** 둔다.
`.claude` 메모리는 세션마다 자동 로드되는 **얇은 포인터/캐시**로만 쓰고, 원본을 두지 않는다.
(= "읽기=직접 + MCP는 편의" 패턴의 메타 버전)

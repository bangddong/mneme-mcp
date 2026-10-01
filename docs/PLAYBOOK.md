# Madi / Mneme 사용자 플레이북

> **"지금 이 상황에서 뭘 하면 되지?"에 답하는 문서.**
> 설치는 [USAGE.md](USAGE.md), 새 PC 이식은 [SETUP-NEW-PC.md](SETUP-NEW-PC.md),
> 동작 원리는 [PRINCIPLES.md](PRINCIPLES.md), 왜 이렇게 만들었는지는 [DECISIONS.md](DECISIONS.md),
> 새 프로젝트 연동·활용법(유스케이스 포함)은 [INTEGRATION.md](INTEGRATION.md).

---

## A. Madi 일상 운영

### 작업을 시작하거나 다시 이어갈 때

1. `doctor`로 Vault 구조와 policy 상태를 확인합니다.
2. `context --workstream <id> --mode portable`로 선택된 portable head를 읽습니다.
3. 이 머신의 confidential overlay까지 허용할 때만 `--mode effective-local`을 사용합니다.
4. 필요한 세부 정보는 `recall`과 authoritative project docs에서 추가로 찾습니다.

전체 Vault나 전체 session history를 매번 prompt에 넣지 않습니다.

### 작업 중 중요한 경계가 생겼을 때

다음 시점에는 host agent가 semantic checkpoint를 작성합니다.

- compact/PreCompact 직전
- 중요한 결정 또는 검증된 milestone
- agent 전환
- 다른 PC로 이동하기 전
- 장시간 작업 중 명시적 중간 checkpoint

Checkpoint에는 대화 원문이 아니라 다음 작업자가 실제로 이어가는 데 필요한 내용만
넣습니다: objective, current state, verified facts, completed work, blockers, next actions.

### Claude에서 Codex로 넘길 때

1. Claude가 마지막 sanitized Session revision을 기록합니다.
2. Handoff는 별도 파일이 아니라 revision 안의 relation/event로 남깁니다.
3. Codex는 별도 Session을 만들고 `continues_from`으로 연결합니다.
4. Registry의 active head/preferred head는 명시적 Core command로 선택합니다.
5. Agent native memory는 실시간 세부 맥락에 사용하고 Madi는 durable flow를 제공합니다.

### 다른 PC에서 이어갈 때

1. Private Vault를 clone합니다.
2. 새 machine-local state-home을 만듭니다.
3. `doctor` → `reindex` → `context`를 실행합니다.
4. 필요한 source mount의 local path를 이 PC에서 다시 binding합니다.

Generated DB, CURRENT, PROFILE, cache는 복사하지 않습니다. 없어도 canonical state는
Session/Memory/Registry에서 복원되어야 합니다.

### Madi가 실패했을 때

일반 agent 작업은 계속합니다. Adapter는 fail-open입니다. 실패한 checkpoint는 나중에
수동으로 다시 제출하며, Madi 장애 때문에 coding/build/test 작업을 중단하지 않습니다.

---

## Legacy Mneme 운영

이하의 상시 HTTP MCP, 외부 Wiki, `state.db`, Ollama, Growth 운영 절차는 기존 사용자를
위해 보존합니다. Madi Core의 필수 운영 절차가 아닙니다.

---

## 0. 시스템 지도 — 뭐가 어디서 돌고 있나

```
[Windows 부팅] ──자동실행──▶ mneme MCP 서버 (localhost:8080/mcp, 상시 가동)
                                │
        ┌───────────────────────┼──────────────────────────┐
        ▼                       ▼                          ▼
  위키 (지식 자산)        스킬/에피소드 (성장)         알림 (폰 접점)
  E:/development/wiki    memory/state.db (SQLite)    Discord 웹훅
  OKF v0.1 마크다운       10분마다 Outer Loop 정산     degrading → 복습 알림
```

| 구성요소 | 위치 | 역할 |
|---------|------|------|
| **mneme 서버** | `E:/development/mneme-mcp` | MCP 허브 (tool 16개). 부팅 시 `shell:startup`의 `mneme-startup.vbs`가 자동 기동 |
| **위키** | `E:/development/wiki` | 진짜 자산. OKF 마크다운, git으로 이력·이식 |
| **wiki-agent** | `E:/development/wiki-agent` | Discord 봇은 **동결**(DECISIONS 07-02 #3). calendar_ingest만 미래 가치 |
| **상태 DB** | `mneme-mcp/memory/state.db` | FTS 인덱스·스킬·에피소드·성장 큐 (새 PC 이식 시 복사 대상) |
| **서버 로그** | `mneme-mcp/memory/server.log`, `server.err.log` | 문제 생기면 여기부터 |

**자동으로 도는 것들 (내가 안 해도 됨):**
- 부팅 → 서버 자동 시작 (기동 직후 위키 전체 재인덱싱 — 로컬 LLM이라 몇 분 걸릴 수 있음)
- 10분마다 → Outer Loop 정산(단, 새 에피소드 20개 쌓여야 실제 실행) + 자기평가
- 위키 파일 변경 → watcher가 FTS 자동 재인덱싱
- 스킬이 degrading으로 강등 → Discord 웹훅 복습 알림 (URL 설정 시)
- wiki-agent 인제스트 시 → 위키 repo git 자동 커밋 (`WIKI_GIT_AUTOCOMMIT=1`)

---

## 1. 공부할 때 (강사 모드)

`E:/development/wiki/tech`에서 Claude Code를 연다 (tech/CLAUDE.md의 강사 페르소나 + mneme 툴 자동 적용).

**세션 흐름:**

| 단계 | 하는 것 | 도구 |
|------|--------|------|
| ① 시작 | "복습 필요한 것부터" — 식어가는 토픽 확인 | `growth_log()` |
| ② 계획 | 오늘 주제 말하면 과거 교훈·진행 중 토픽을 받음 | `skill_suggest("<주제>", session_id)` |
| ③ 새 토픽이면 | 학습 토픽 등록 (토픽 1개 = 스킬 1개) | `skill_seed(name="study-<영역>-<토픽>", wiki_path="tech/pages/...")` |
| ④ 학습 | 실습 중심 진행. 배운 것은 즉시 `pages/`에 통합됨 | (강사 모드가 알아서) |
| ⑤ 종료 | 이해도 자가 반성 제출 → 성향 학습 | `episode_reflect(task=..., success=스스로 설명 가능?, score=0~1, skills_used=["study-..."])` |
| ⑥ 다음엔 뭐? | 현재 난이도 기반 다음 추천 | `curriculum_suggest()` |

> 이 사이클이 돌면: 이해=성향↑(seeding→developing→active), 방치=degrading→**폰 알림**.
> 지식은 위키에, 숙련도는 스킬에 — 이원 추적이 설계 의도.

---

## 2. 지식을 넣을 때 (인제스트 3경로)

| 경로 | 언제 | 방법 |
|------|------|------|
| **① 프로젝트 자동 (메인)** | 연동 프로젝트에서 개발 중 | 아무것도 안 함 — 프로젝트 Claude가 `wiki_inject`로 알아서 적재 (Track 1) |
| **② 위키에서 직접** | 아티클/문서를 각 잡고 넣을 때 | 해당 카테고리 디렉토리에서 Claude 열고 `/ingest <URL 또는 텍스트>` |
| **③ 아무 세션에서** | 이 대화처럼 즉석에서 | `wiki_inject(path="카테고리/pages/.../slug.md", content=<OKF 형식>, source_agent, session_id)` |

**어느 경로든 페이지는 OKF 형식이어야 한다** (→ §6 요약표). 형식이 맞는지 의심되면 `wiki_lint()`.

---

## 3. 지식을 찾을 때 (읽기=직접 원칙)

- **목록 훑기**: `wiki_list(prefix="tech/")` — 경로+요약+태그
- **검색**: `wiki_search(query, session_id)` — FTS5 원문 청크 반환 (로컬 LLM이 답을 "생성"하지 않음, 원문이 옴)
- **전문 읽기**: `wiki_get(path)` — 파일 통째로
- **위키 안에서**: 카테고리 디렉토리에서 `/query <질문>`
- 성능이 급하면 그냥 `grep`/에디터로 열어도 됨 — 마크다운이 원본이라는 게 이 시스템의 존재 이유

---

## 4. 복습 알림이 왔을 때 (📉 Discord 푸시)

1. `growth_log()` (또는 `python -m mneme.growth`) — 뭐가 얼마나 식었는지 확인
2. 해당 토픽을 강사 모드로 복습 (§1의 ②~⑤ 그대로 — `episode_reflect`에서 이해되면 성향 회복 → active 복귀)
3. 스킬 외 조치 항목(자기평가가 감지한 문제)이면 처리 후 `growth_resolve(id, note)`

**알림 켜는 법 (최초 1회):** Discord 채널 설정 → 연동 → 웹후크 → URL 복사 →
`mneme-mcp/.env`의 `DISCORD_WEBHOOK_URL=`에 붙여넣기 → 서버 재시작 (§7).

---

## 5. 주간 점검 루틴 (5분)

```bash
cd E:/development/mneme-mcp
python -m mneme.lint          # 위키 무결성 (오류 0 유지)
python -m mneme.growth        # 미해소 조치 큐
python -m mneme.log -n 10     # 최근 누가 뭘 했나 (옵저버빌리티)
```
MCP 쪽에서는 `mneme_status()` / `outer_loop_status()` / `self_model_status()`가 같은 역할.
위키 repo는 `cd E:/development/wiki && git log --oneline -5`로 자동 커밋이 잘 쌓이는지 확인.

---

## 6. OKF 페이지 규격 (요약)

> 전체 규격: 위키 루트 `CLAUDE.md` + [OKF SPEC](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)

```markdown
---
type: concept                  # 필수! concept|source|lesson|roadmap|schema|doc
title: 페이지 제목
description: 한줄 설명 (인덱스용)
timestamp: YYYY-MM-DD
tags: [tag1, tag2]
---

## Summary / ## Details / ## Sources / ## Related   ← 4섹션 필수
```

- 링크는 **위키 루트 절대경로**: `[제목](/tech/pages/java-spring/foo.md)` (`[[...]]`는 레거시, 신규 금지)
- 아직 없는 페이지 링크는 같은 줄에 `<!-- stub -->`
- `index.md`/`log.md`는 예약 파일 (frontmatter 없음) — 인덱스는 `* [제목](경로) - 설명` 불릿, 로그는 `## 날짜` 헤딩

---

## 7. 서버 운영

| 작업 | 방법 |
|------|------|
| 상태 확인 | `netstat -ano \| findstr :8080` 또는 MCP로 `mneme_status()` |
| 수동 시작 | `mneme-startup.vbs` 더블클릭, 또는 PowerShell: `Start-Process python -ArgumentList '-u','-m','mneme.server' -WorkingDirectory 'E:\development\mneme-mcp' -WindowStyle Hidden` |
| 중지 | `netstat -ano \| findstr :8080`으로 PID 확인 → `taskkill /PID <PID> /F` |
| 재시작 | 중지 → 시작 (`.env` 변경 후 필수) |
| 로그 | `memory/server.log` (일반), `memory/server.err.log` (오류) |

**`.env` 항목:** `LLM_BASE_URL`/`LLM_MODEL`(로컬 LLM) · `WIKI_DIR` · `DB_PATH` · `MCP_HOST`/`MCP_PORT` · `OUTER_LOOP_INTERVAL_MIN` · `DISCORD_WEBHOOK_URL` · `MCP_AUTH_TOKEN`(bearer 인증 — 비우면 무인증 로컬 전용, 외부 노출 시 필수)

---

## 8. 확장할 때

| 하고 싶은 것 | 방법 |
|------|------|
| **새 프로젝트를 mneme에 연결** | `python integration/install.py --project <경로> --agent <이름> --category <위키카테고리> --prefix <스킬접두어>` — CLAUDE.md 연동 블록 + .mcp.json 자동 설치. **무엇을 어디에 쌓고 언제 위키로 승격하나·유스케이스는 [INTEGRATION.md](INTEGRATION.md)** |
| **새 위키 카테고리** | 위키 루트 `CLAUDE.md`의 "새 카테고리 추가 방법" 5단계 |
| **위키를 남과 공유** | OKF 번들이라 그대로 전달 가능 — 상대는 OKF 스펙만 알면 어떤 에이전트로든 소비 |
| **새 PC로 이식** | [SETUP-NEW-PC.md](SETUP-NEW-PC.md) — 3 repo clone + state.db 복사 + .env |
| **질문 뱅크 채우기** | (백로그, 위키 쌓이면) Claude에게: "위키 tech/·ai-llm/의 concept 페이지를 훑어 질문 뱅크 시드 PR 만들어줘" — 런타임 연동 금지, 빌드타임 시드만 (DECISIONS 07-02 #3) |
| **폰에서 캡처/조회** | (보류 — 수요 검증 시) mneme 인증 추가 + Claude 모바일 커스텀 커넥터 (Tailscale Funnel) — Discord 봇 부활 아님 |

---

## 9. 트러블슈팅

| 증상 | 원인/조치 |
|------|----------|
| 서버 켰는데 한참 8080이 안 열림 | **정상** — 기동 시 위키 전체 재인덱싱(로컬 LLM). 수 분 대기. `server.log`에 `Mneme MCP server running` 뜨면 완료 |
| 브라우저로 `localhost:8080/mcp` 열면 406 | **정상** — MCP streamable-http는 전용 헤더 필요. 서버는 살아 있음 |
| Claude에서 mneme 툴이 안 보임 | 서버 다운 (선택적 의존이라 조용히 사라짐) → §7로 기동. 세션 재시작해야 다시 잡힘 |
| 복습 알림이 안 옴 | ①`DISCORD_WEBHOOK_URL` 비었나 ②재시작 했나 ③애초에 degrading 전이가 없었나(`outer_loop_status()`로 확인 — 20 에피소드 게이트) |
| lint 오류 | `python -m mneme.lint <카테고리>`로 좁혀서 메시지대로 수정. 미래 페이지 링크는 `<!-- stub -->` 추가 |
| 검색 결과가 낡음 | 위키를 밖에서 수정했는데 watcher가 놓친 경우 → 서버 재시작 (기동 시 전체 재인덱싱) |

---

## 10. 치트시트

```
# ── MCP 툴 (Claude 세션 안에서) ─────────────────────
wiki_search / wiki_get / wiki_list / wiki_inject / wiki_lint     # 지식
skill_seed / skill_suggest / episode_reflect                     # 성장
growth_log / growth_resolve / curriculum_suggest                 # 조치·추천
mneme_status / outer_loop_run / outer_loop_status                # 운영
self_model_status / episode_log                                  # 자기인식·이력

# ── CLI (터미널에서, mneme-mcp 디렉토리) ─────────────
python -m mneme.lint [카테고리]                  # 위키 무결성
python -m mneme.growth [--resolve ID --note ""]  # 성장 조치 큐
python -m mneme.log [-n N] [-a agent] [-t tool]  # 접근 이력

# ── 위키 스킬 (위키 카테고리 디렉토리에서) ────────────
/ingest <소스>   /query <질문>   /lint
```

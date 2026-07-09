# 사용 방법 (Usage)

> 설치·기동부터 일상 사용, 관찰·운영, 트러블슈팅까지 다룹니다.
> 개념(왜/어떻게)은 [PRINCIPLES.md](PRINCIPLES.md), 구조는 [ARCHITECTURE.md](ARCHITECTURE.md)를 참고하세요.

---

## 1. 설치 & 기동

### 1-1. 의존성

```bash
pip install fastmcp watchdog httpx apscheduler python-dotenv pyyaml
```

### 1-2. 로컬 LLM (Ollama) — 무과금 두뇌

Mneme의 모든 LLM 작업은 로컬 모델로 처리됩니다(API 토큰 없음).

> 설치가 끝난 뒤 **일상적으로 쓸 때는 [PLAYBOOK.md](PLAYBOOK.md)** — 상황별(공부할 때,
> 지식 넣을 때, 알림 왔을 때 등) 가이드가 따로 있습니다.

```bash
# Ollama 설치 후
ollama pull qwen2.5:7b      # 하드웨어에 맞춰 3B~8B 중 선택 (아래 표 참고)
ollama serve                # 보통 자동 실행, http://localhost:11434
```

| VRAM/RAM | 권장 모델 |
|----------|-----------|
| 8GB GPU | `qwen2.5:7b` 또는 `llama3.1:8b` (Q4 ~5GB) |
| 4GB 이하 / GPU 없음 | `llama3.2:3b`, `qwen2.5:3b` (CPU로도 동작, 느릴 뿐) |

> **모델이 꺼져 있어도 됩니다**: Mneme는 LLM 미가용 시 보수적 fallback(검색 빈 결과,
> 병합 기본값, coherence 0.95, 보정오차 None)으로 **죽지 않고** 동작합니다. 단 검색 품질·
> 자기평가 정밀도는 떨어집니다.
>
> **NPU를 쓰고 싶다면**: Ollama는 NPU를 쓰지 않습니다(CPU/GPU). `LLM_BASE_URL`만
> Foundry Local(OpenAI 호환) 등으로 바꾸면 코드 변경 없이 NPU를 활용할 수 있습니다.

### 1-3. 환경변수 (`.env`)

```ini
LLM_BASE_URL=http://localhost:11434/v1   # OpenAI 호환 엔드포인트 (Ollama 기본)
LLM_MODEL=qwen2.5:7b                       # 사용할 로컬 모델
WIKI_DIR=E:/development/wiki                # 지식 베이스 위치 (mneme 레포 바깥)
DB_PATH=./memory/state.db                  # SQLite 상태 DB
MCP_HOST=0.0.0.0
MCP_PORT=8080
OUTER_LOOP_INTERVAL_MIN=10                 # 백그라운드 정산 주기(분)
DISCORD_WEBHOOK_URL=                       # 복습 알림 웹훅 (비우면 비활성화)
```

`DISCORD_WEBHOOK_URL`을 채우면 Outer Loop가 스킬을 `degrading`으로 강등할 때
"복습 필요" 알림이 해당 Discord 채널(→폰 푸시)로 갑니다. 봇 계정 불필요 —
채널 설정 → 연동 → 웹후크에서 URL만 발급하면 됩니다.

`.env`는 git 추적에서 제외됩니다(민감정보). `.env.example`을 복사해서 만드세요.

### 1-4. 실행

```bash
python -m mneme.server
# → Mneme MCP server running at http://0.0.0.0:8080/mcp
```

기동 시 자동으로: ① DB 초기화 → ② Wiki 전체 재인덱싱 → ③ 파일 감시(watcher) 시작 →
④ 백그라운드 스케줄러 시작을 수행합니다.

### 1-5. 부팅 시 자동 기동 (선택)

- `mneme-start.bat`: 서버 런처.
- 시작프로그램 폴더의 `mneme-startup.vbs`: 콘솔을 숨긴 채 bat을 실행합니다. 로그는 `memory/server.log`.

### 1-6. Claude Code 연결

프로젝트 `.mcp.json`(이미 레포에 포함) 또는 `~/.claude.json`의 mcpServers에 추가합니다.

```json
{ "mcpServers": { "mneme": { "type": "http", "url": "http://localhost:8080/mcp" } } }
```

> ⚠️ MCP 서버는 **Claude Code 재시작 시점에 연결**됩니다. 서버에 새 도구를 추가했다면
> Claude Code를 껐다 켜야 13개가 모두 보입니다.

---

## 2. 일상 사용 시나리오 (가장 중요)

Mneme는 두 가지 방식으로 씁니다: **(A) 지식 관리**, **(B) 자가 성장 루프**.

### 2-A. 지식 관리 — Wiki 읽고 쓰기

**검색해서 답 찾기**

```
wiki_search(query="스프링 트랜잭션 전파 옵션", session_id="sess-1")
→ { results: [{path, excerpt}...], summary: "..." }
```

- FTS5 전문검색 + 로컬 LLM이 관련 문서를 추려 요약까지 제공합니다.
- 같은 query는 30분간 세션 캐시됩니다(중복 호출 절약).

**문서 전문 보기**

```
wiki_get(path="tech/pages/java-spring/transaction.md")
→ { content, ... , updated_by }
```

**지식 적어 넣기 (생성 또는 병합)**

```
wiki_inject(path="...", content="...", source_agent="claude", session_id="sess-1")
→ { action: "created" | "merge" | "conflict" | "skip", path }
```

- 새 파일이면 생성합니다. 기존 파일이면 LLM이 관계를 판단합니다.
  - `merge`: 보완 관계 → 자동 병합
  - `conflict`: 모순 → 원문 보존 + `<!-- CONFLICT -->` 마커로 첨부 (사람이 나중에 해결)
  - `skip`: 동일 내용

**목록 보기 / 건강검진**

```
wiki_list(prefix="tech/")           → 인덱싱된 문서 목록(경로·요약·태그·갱신자)
wiki_lint(category="ai-llm")        → 형식·깨진링크·고아·index동기화·stale 검사 (category="" 면 전체)
```

> Wiki는 mneme 바깥(`E:/development/wiki/`)에서 **Obsidian 등으로 직접 편집해도** watcher가
> 자동으로 인덱스를 갱신합니다. mneme는 그 위의 검색·병합·검증 계층입니다.

### 2-B. 자가 성장 루프 — Inner Loop (에이전트가 직접 돌립니다)

성장의 단위는 **스킬(skill)** = "어떤 종류의 일에 대한 신뢰도(성향, 0~1)"입니다. 흐름은 다음과 같습니다.

```
① (1회)   skill_seed       스킬 등록
② (작업 전) skill_suggest    관련 스킬 + 과거 교훈을 받아 계획에 반영
③ 작업 수행 (에이전트가 함)
④ (작업 후) episode_reflect  결과를 반성으로 제출 → CIB 게이트 → 성향 갱신
```

**① 스킬 시드**

```
skill_seed(name="api-design", description="REST API 설계", seed_protected=False)
→ { name, state: "seeding", propensity: 0.5 }
```

- `seed_protected=True`로 등록한 스킬(예: 안전·거부 관련)은 **CIB가 강등을 차단**하고,
  Outer Loop도 강등·아카이브에서 면제합니다. 정체성의 닻입니다.

**② 작업 전 — 계획 지원**

```
skill_suggest(task="결제 API 만들기", session_id="sess-1")
→ { skills: [{name, propensity, state}...],  past_hints: ["지난번 X에서 Y 주의"...] }
```

- 관련 스킬을 성향 높은 순으로, 최근 반성에서 뽑은 교훈과 함께 줍니다. 에이전트는 이걸 보고
  계획을 세웁니다.

**④ 작업 후 — 반성·학습**

```
episode_reflect(
  task="결제 API 만들기",
  outcome="3개 엔드포인트 구현, 테스트 통과",
  success=True,
  score=0.8,                       # 에이전트의 자기 평가(0~1)
  skills_used=["api-design"],
  session_id="sess-1"
)
→ { reflection: {what_worked, what_failed, next_hint},
    skill_updates: [{name, applied: true/false, propensity, reason}] }
```

여기서 일어나는 일:

1. LLM이 반성을 생성합니다(무엇이 통했나/실패했나/다음 힌트) → `episodes`에 저장됩니다.
2. `skills_used`의 각 스킬에 reward(성공 +1 / 실패 −1)를 적용해 후보 δ를 계산합니다.
3. **CIB 게이트** 검사: 헌법 시나리오 coherence ≥ 0.95 이고 δ 크기 ≤ 0.2 이며 seed_protected
   강등이 아니면 반영(`applied: true`)됩니다. 아니면 폐기(`applied: false`, reason 명시)됩니다.

> 자세한 성장 원리는 [PRINCIPLES.md](PRINCIPLES.md)를 참고하세요.

---

## 3. 관찰 & 운영 — 지금 무엇이 일어나는지 보기

| 도구 | 보는 것 |
|------|---------|
| `mneme_status()` | wiki 문서 수, 인덱스 수, 오늘 에피소드 수, 마지막 인덱싱 시각, 활성 세션 수 |
| `outer_loop_status()` | 최근 정산 사이클(CI/BC/전이 내역) + 현재 스킬 state 분포 |
| `self_model_status()` | 보정오차·성장속도·난이도 시계열 + 최신 조절 상태 + `alert` 플래그 |
| `curriculum_suggest()` | 현재 난이도 + 성장 타깃(어떤 스킬을 더 연습/개선/주의해야 하나) |
| `outer_loop_run(force=True)` | 20개가 안 쌓여도 **즉시** 정산 (점검·디버깅용) |

운영 팁:

- 처음에는 데이터가 없어 `self_model_status`가 비어 있습니다. 에이전트가 `episode_reflect`로
  작업을 쌓아야 의미가 생깁니다.
- 빨리 결과를 보고 싶으면 에피소드 몇 개를 만든 뒤 `outer_loop_run(force=True)` →
  `self_model_status()` 순으로 확인하세요.
- 로그: `memory/server.log` (자동 기동 시). WARNING 라인이 성장 경보입니다.

---

## 4. 트러블슈팅 / FAQ

**Q. 도구가 5개(또는 옛날 개수)만 보입니다.**
→ MCP는 Claude Code 재시작 때 연결됩니다. Claude Code를 껐다 켜세요. 현재 13개입니다.

**Q. 검색이 빈약하거나 보정오차가 계속 None입니다.**
→ 로컬 LLM(Ollama)이 떠 있지 않을 때입니다. `ollama serve` 여부, `LLM_MODEL` 모델 pull
여부를 확인하세요. (서버 자체는 fallback으로 정상 동작 중입니다)

**Q. `self_model_status` / `outer_loop_status`가 비어 있습니다.**
→ 정산이 아직 안 돌았습니다. 에피소드 20개 이상이 쌓이길 기다리거나 `outer_loop_run(force=True)`
를 호출하세요. self_model은 다음 스케줄러 틱(또는 서버 재기동 후 force)에서 채워집니다.

**Q. 한글 로그가 깨집니다(cp949).**
→ 알려진 잔여 이슈(server.py print)입니다. lint CLI는 UTF-8 강제를 완료했고, 서버 로그는
TODO입니다. 기능에는 영향이 없습니다.

**Q. 성향이 오르지 않습니다.**
→ ① 그 스킬을 `skills_used`에 넣어 `episode_reflect` 했는지, ② `success=True`였는지,
③ `applied:false`면 CIB가 막은 것(reason 확인)인지, ④ η=0.1씩이라 여러 번 성공해야 눈에
띈다는 점을 확인하세요.

**Q. API 비용이 드나요?**
→ 아닙니다. 모든 LLM 호출은 로컬(Ollama)입니다. `.env`에 Anthropic 키가 필요 없습니다(제거됨).

---

## 5. 빠른 시작 체크리스트

1. `ollama serve` + `ollama pull qwen2.5:7b`
2. `cp .env.example .env` → 값 확인
3. `python -m mneme.server` → `http://localhost:8080/mcp`가 뜨는지 확인
4. Claude Code 재시작 → 도구 13개가 보이는지 확인 (`mneme_status` 호출)
5. `wiki_search`로 검색 → `skill_seed` → 작업 → `episode_reflect`로 첫 학습 루프 한 바퀴
6. `outer_loop_run(force=True)` → `self_model_status()`로 성장 지표 확인

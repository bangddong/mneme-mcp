# 동작 원리 (Principles)

> Madi가 **누구의 연속성을, 어떤 경계로 보존하는지**와 legacy Mneme Growth가
> 어떤 역할로 남는지를 설명합니다.
> 설치·사용 방법은 [USAGE.md](USAGE.md), 구성요소·데이터 흐름은 [ARCHITECTURE.md](ARCHITECTURE.md)를 참고하세요.

승인된 상세 설계의 authority는
[Madi D3 specification](superpowers/specs/2026-08-26-madi-d3-vault-design.md)입니다.

## A. Madi Core 원칙

### A-1. 사람 소유

Madi의 ownership unit은 project, agent, model이 아니라 **person**입니다. 한 사람이
private portable Vault를 소유하고 Claude, Codex, 미래 agent 및 여러 project가 그
연속성을 읽고 씁니다.

### A-2. Project truth와 personal continuity 분리

- Project repository는 코드, ADR, 공식 규칙과 팀 합의 같은 **project truth**를 소유합니다.
- Madi는 개인의 checkpoint, 교훈, preference, cross-project knowledge와 project source
  reference 같은 **personal continuity**를 소유합니다.
- 같은 project에 관해 충돌하면 project repository가 우선입니다. Madi는 공식 문서를
  복제해 competing truth를 만들지 않고 reference나 개인적 consequence를 저장합니다.

### A-3. Git + Markdown이 durable source of truth

Portable Session revision, accepted Memory, Registry와 policy revision은 현재 파일 트리에
완전해야 합니다. Shallow clone, squash, history rewrite 또는 생성 DB 삭제 후에도 durable
state를 복원할 수 있어야 합니다. Git history는 추가 이력이지 checkpoint의 유일한 저장소가
아닙니다.

### A-4. Deterministic Core, semantic host

Madi Core는 schema, validation, CAS, policy, storage, indexing과 deterministic recall을
담당합니다. 무엇이 중요한지, 어떤 내용을 checkpoint로 정제할지는 현재 작업 문맥을 아는
host agent가 판단합니다. Generation LLM, embedding, daemon은 Core의 필수 조건이 아닙니다.

### A-5. Candidate-first, immutable accepted semantics

Candidate는 검토 중 수정할 수 있습니다. Accepted Memory의 semantic body는 overwrite하지
않고 새 Memory ID와 `supersedes` relation으로 교체합니다. Session checkpoint도
`sessions/<session-id>/<revision>.md`의 immutable revision으로 누적합니다.

### A-6. Privacy ceiling 상속

Raw transcript, raw tool output, credential, 고객 데이터와 local evidence는 자동으로 portable
Git에 들어가지 않습니다. Source/project policy는 파생 artifact의 portability 상한으로
상속됩니다. Agent는 더 보수적으로 낮출 수 있지만 상한보다 높일 수 없습니다.

Policy tightening은 이후 read/export/sync에 즉시 적용되지만 이미 Git commit, clone, backup,
mirror에 전파된 정보의 retroactive deletion은 보장하지 않습니다.

### A-7. 명시적 동시성

Registry는 active heads와 preferred head를 portable하게 기록합니다. Intentional parallel
work는 `divergent`, source 부재 등 제한된 상태는 `degraded`, 구조·policy 손상은 `invalid`로
구분합니다. 같은 lineage나 preferred-head 충돌을 last-write-wins 또는 AI semantic merge로
덮지 않습니다.

### A-8. 실패 격리

Madi와 adapter는 ordinary agent work를 막지 않아야 합니다. Adapter 장애는 닫힌 경고를
남기되 host를 block하지 않습니다. Growth Lab의 실패나 부재도 Core를 깨뜨리지 않습니다.

---

## Legacy Mneme / Growth Lab 원칙

아래 5층 기억, CIB, Inner/Outer Loop, self-model은 기존 Mneme의 연구적 성장 시스템입니다.
가치를 보존하되 Madi Core의 필수 기능으로 해석하지 않습니다.

**MNEME**(므네메)는 그리스 신화 '기억의 무사(Muse)'에서 온 이름입니다. 원래 시스템의
성장 두뇌를 부르던 내부 코드네임이었고 2026-07-02 시스템 전체 이름으로 승격했습니다.

---

## Legacy 1. 왜 이렇게 만들었나

보통의 RAG는 매 질문마다 원본을 다시 뒤져 답을 "재유도"합니다 — 지식이 **쌓이지 않습니다**.
MNEME는 처리된 지식을 구조화된 Wiki에 **컴파일해 저장**하고(L2), 작업 경험에서 **스킬 성향을
학습**하며(L3), 그 성장이 **가치(헌법)** 에 어긋나지 않도록 게이트(CIB)로 지킵니다.
즉 *복리로 자라는 기억*을 목표로 합니다.

핵심 수식(개념):

```
θ_eff = clip(θ_base + δ)          # 스킬의 실효 신뢰도 = 공유 기반 + 개인화 적응
δ_{t+1} = δ_t + η · reward         # 성공(+1)/실패(-1)로 적응값을 한 스텝(η=0.1) 이동
→ 단, CIB(헌법 게이트)를 통과할 때만 적용. 막히면 폐기.
```

LLM의 본체(θ_base)는 그대로 둔 채, 작은 적응값 δ만 외부에서 조정합니다. 그래서 모델을
재학습하지 않고도 시스템이 점점 능숙해집니다.

---

## Legacy 2. 5층 기억 — 무엇을 기억하나

| 층 | 내용 | 저장소 |
|----|------|--------|
| **L1 Episodic** | 에피소드(작업 이력·반성) | `episodes` 테이블 |
| **L2 Semantic** | 일반 지식 = Wiki | `E:/development/wiki/` + FTS5 인덱스 |
| **L3 Procedural** | 스킬 성향 θ_eff (어떤 일에 얼마나 능숙/신뢰?) | `skills` 테이블 |
| **L4 Value** | 헌법(절대·원칙·전략층) | `constitution.yaml` |
| **L5 Identity** | 자기 모델(보정오차·성장속도·난이도) | `self_model` 테이블 |

---

## Legacy 3. 5요소 — 어떻게 성장하나

| 요소 | 역할 |
|------|------|
| **1. Constitution** | 가치 헌법 + 테스트 시나리오 K. 성장의 기준입니다. |
| **2. CIB (헌법 게이트)** | 모든 성향 변화가 헌법 공간 안인지 검사합니다. 통과해야만 반영됩니다. |
| **3. Inner Loop** | 매 작업마다: 반성 → CIB → 스킬 성향 즉시 갱신. (건별 미세 조정) |
| **4. Outer Loop** | N주기마다: 누적 성과로 스킬 **생애주기** 전이 + CI/BC 기록. (주기 정산) |
| **5. 자기 인식** | Outer Loop 위 메타: 보정오차·성장속도 추적, 이상 감지, 성장 타깃 추천. |

### 3-1. Inner Loop — 매 작업의 미세 조정

성장의 단위는 **스킬(skill)** = "어떤 종류의 일에 대한 신뢰도(성향, 0~1)"입니다.
에이전트가 작업을 마치고 `episode_reflect`로 결과를 제출하면 다음이 일어납니다.

1. LLM이 반성을 생성합니다(무엇이 통했나 / 실패했나 / 다음 힌트) → `episodes`에 저장됩니다.
2. `skills_used`의 각 스킬에 reward(성공 +1 / 실패 −1)를 적용해 후보 δ를 계산합니다.
3. **CIB 게이트**를 통과하면 반영(`applied: true`), 아니면 폐기(`applied: false`)합니다.

> 잘한 일을 반복하면 그 스킬의 성향이 천천히(η=0.1씩) 오르고, 헌법에 어긋나는 방향이면
> 보상이 있어도 막힙니다.

### 3-2. Outer Loop — 스킬 생애주기 정산 (L3)

`OUTER_LOOP_INTERVAL_MIN`(기본 10분)마다 스케줄러가 깨어, "직전 정산 이후 새 에피소드
20개 이상"일 때 실행됩니다(하이브리드 게이트). 스킬은 5개 상태를 오갑니다.

```
seeding ──(use≥3)──► developing ──(use≥10 & 성공률≥0.7 & 성향≥0.6)──► active
                                                                         │ ▲
                                  (최근성공률<0.4 또는 성향<0.35)──────────┘ │(최근성공률≥0.7 회복)
                                              ▼                            │
                                         degrading ──────────────────────┘
                                              │
                          (성향<0.2 또는 3사이클 미사용)──► archived
```

- **seed_protected 스킬은 강등·아카이브 모두 면제**됩니다 (헌법 "정체성 보존").
- 매 사이클마다 두 지표를 `loop_cycles`에 기록합니다.
  - **CI (Coherence Index)**: active/developing 스킬이 지금도 헌법에 정렬돼 있는가 (로컬 LLM 채점, 미가용 시 None).
  - **BC (Behavioral Consistency)**: 성향이 얼마나 안정적인가 = 1 − 직전 대비 평균 |Δ성향|.

### 3-3. 자기 인식 — self_model (L5, Outer Loop 위 메타)

- **M15 독립 평가자(Phoenix Assessor)**: 수행자의 자기 점수(`score`)를, 분리된 LLM
  감사관이 다시 채점합니다 → 둘의 괴리가 **보정오차**입니다. ("내가 내 실력을 정확히 보고 있나?")
- **M16 성장 조절기**: 성공률 변화(성장속도)를 보고 상태를 판정합니다 →
  `healthy` / `crash`(급락, ≤ −0.25) / `overspeed`(과속, ≥ +0.4) / `stagnant`(정체, 평탄 3사이클).
  이상 시 **server.log에 WARNING** + `self_model_status`의 `alert` 플래그가 켜집니다.
- **M17 내재적 동기**: **난이도 스칼라**(잘 굴러가면 +0.1, 무너지면 −0.1)와
  **성장 타깃**(연습 부족·품질 미달·위험 스킬)을 산출합니다.

---

## Legacy 4. 안전 — 헌법과 CIB

`mneme/constitution.yaml`은 3층 구조입니다.

- **절대층**(불변): 정직성·안전·투명성·정체성 보존. `coherence_threshold: 0.95`.
- **원칙층**: 데이터 주권, 선택적 주입, 평가자 분리, "망각은 기능".
- **전략층**: 도메인·기술 선호(정확도 > 안전 > 속도)·팀 규모. (단기, Outer Loop에서 갱신)
- **테스트 시나리오 K**(TS01~04): CIB가 매 성향 변화마다 이 시나리오들로 정렬도를 채점합니다.

**CIB가 막는 것**:

1. seed_protected 스킬의 강등(reward < 0).
2. δ 크기가 ±0.2 초과.
3. 헌법 시나리오 coherence 최저점이 0.95 미만 (예: "추측을 사실로 단정하는 스킬"을 성공으로
   강화하려는 시도 → 투명성 위반 → 차단).

> 성장이 막히면 `episode_reflect`의 `skill_updates[].applied`가 `false`로 오고 `reason`에
> 이유가 적힙니다. 이것은 버그가 아니라 **설계된 안전 동작**입니다.

---

*Legacy MNEME 5요소·5층은 호환 경로에서 유지됩니다. 현재 Madi 구현 진행은
[PROGRESS.md](../PROGRESS.md)를 참고하세요.*

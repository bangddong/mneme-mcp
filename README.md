# mneme-mcp — Madi 전환 작업

이 저장소는 기존 **Mneme**를 사람 소유의 휴대 가능한 AI 연속성 계층인
**Madi**로 점진적으로 진화시키고 있습니다. 저장소와 Python 패키지 이름은 아직
`mneme-mcp` / `mneme`이며, 기존 기능도 삭제하지 않았습니다.

> **Madi의 핵심 명제:** AI 공급자가 아니라 사람이 작업의 연속성을 소유한다.

현재 코드에는 두 경로가 공존합니다.

| 경로 | 상태 | 용도 |
|---|---|---|
| **Madi Vault Core** | opt-in, 구현됨 | Git + Markdown 기반 개인 연속성, 세션 revision, Memory, 정책, FTS recall, CLI/stdio/adapters |
| **Legacy Mneme** | 유지됨 | HTTP MCP, 외부 Wiki, `state.db`, watcher, scheduler, Growth Lab |

Madi Core는 generation LLM, Ollama, 항상 켜진 daemon 없이 동작합니다. Legacy
Mneme 경로에서는 기존 local LLM 및 HTTP MCP 구성이 계속 사용됩니다.

## 무엇을 어디에 저장하는가

| 저장소 | authoritative ownership | 예시 |
|---|---|---|
| Project repository | 프로젝트의 공식 진실 | 소스 코드, ADR, 공식 규칙, 팀 합의 문서 |
| Portable Madi Vault | 개인이 소유하는 휴대 가능한 연속성 | checkpoint, 개인 결정·lesson·preference·knowledge, project reference |
| Machine-local Madi state | 이 머신에서만 필요한 민감·생성 상태 | index, CURRENT/PROFILE view, source binding, evidence, cache, log |
| Legacy Mneme data | 마이그레이션 전 기존 데이터 | 외부 Wiki와 `state.db`의 episode/Growth 데이터 |

Portable Vault에는 raw transcript, raw tool output, credential, 고객 데이터, 운영
로그 원문을 자동 저장하지 않습니다. Host agent가 의미 있는 내용을 선택·정제하고,
현재 policy ceiling을 통과한 artifact만 Git에 들어갈 수 있습니다.

## Madi Vault 빠른 시작

Python 3.11 이상이 필요합니다.

```powershell
python -m pip install -e ".[dev]"
```

새 Vault는 현재 Core API로 초기화합니다. Vault와 machine-local state는 서로 다른
경로여야 합니다.

```powershell
@'
from pathlib import Path
from mneme.core.vault import Vault

Vault.initialize(
    Path(r"D:\data\my-madi"),
    Path(r"D:\local\madi-state"),
    "my-owner-id",
)
'@ | python -
```

그다음 CLI를 사용할 수 있습니다. 전역 경로 옵션은 subcommand 앞에 둡니다.

```powershell
$Vault = "D:\data\my-madi"
$State = "D:\local\madi-state"

python -m mneme.cli --vault-root $Vault --state-home $State status
python -m mneme.cli --vault-root $Vault --state-home $State doctor
python -m mneme.cli --vault-root $Vault --state-home $State reindex
python -m mneme.cli --vault-root $Vault --state-home $State context --mode portable
python -m mneme.cli --vault-root $Vault --state-home $State recall "검색어" --limit 10
```

editable/package install은 같은 명령을 `mneme-vault` entry point로 제공합니다.
CLI가 반환하는 결과는 자동화 가능한 JSON envelope입니다.

Memory candidate 작성 예:

```powershell
python -m mneme.cli --vault-root $Vault --state-home $State remember `
  --kind lesson `
  --scope personal-global `
  --authority personal `
  --portability personal-vault `
  --body "검증된 재사용 가능 교훈"
```

`remember`는 candidate를 제출합니다. Accepted Memory 승격과 policy lifecycle은
agent-neutral Core command API를 사용합니다. Workstream bootstrap은 아직 CLI/Core
command가 아니라 typed `RegistryStore` API를 사용합니다. 실제 예시는
[사용 방법](docs/USAGE.md)과
[통합 문서](docs/INTEGRATION.md)를 참고하세요.

## Claude와 Codex 연결

Madi adapter는 opt-in이며 실패해도 일반 agent 작업을 막지 않습니다.

- Claude: `integration/madi/install_claude.py`
- Codex: `integration/madi/install_codex.py`
- Codex `PreCompact` 계약: [integration/madi/codex/README.md](integration/madi/codex/README.md)

Lifecycle event는 checkpoint 필요성을 알릴 뿐입니다. Raw transcript를 읽거나 자동으로
Vault에 복사하지 않습니다. 실제 checkpoint는 host agent가 작성한 정제된 payload를
명시적으로 Core에 제출해야 합니다.

## 다른 PC에서 이어가기

Portable Vault만 private Git으로 clone한 뒤 새 machine-local state를 만들고 다음을
실행합니다.

```powershell
python -m mneme.cli --vault-root <cloned-vault> --state-home <new-local-state> doctor
python -m mneme.cli --vault-root <cloned-vault> --state-home <new-local-state> reindex
python -m mneme.cli --vault-root <cloned-vault> --state-home <new-local-state> context --mode portable
```

생성 DB나 view를 복사할 필요가 없습니다. Source mount는 machine-local binding이므로
새 PC에서 다시 연결합니다. 자세한 절차는
[새 PC 설정 가이드](docs/SETUP-NEW-PC.md#8-opt-in-vault-bootstrap-on-another-pc)를
참고하세요.

## Legacy Mneme 실행

기존 HTTP MCP/Wiki/Growth 경로는 호환성을 위해 유지됩니다.

```powershell
python -m pip install -e ".[dev]"
copy .env.example .env
python -m mneme.server
# http://localhost:8080/mcp
```

Legacy semantic 기능을 사용하려면 `.env`의 `LLM_BASE_URL`, `LLM_MODEL`, `WIKI_DIR`,
`DB_PATH`를 설정하고 필요에 따라 Ollama 같은 OpenAI-compatible local provider를
실행합니다. 이것은 Madi Core의 필수 조건이 아닙니다.

Legacy data를 Madi로 옮길 때는 원본 DB/Wiki를 보존하고 local-only review bundle로
먼저 staging해야 합니다. 자세한 절차는
[Mneme → Madi migration guide](docs/MIGRATION-MNEME-TO-MADI.md)를 참고하세요.

## 문서 지도

| 문서 | 역할 |
|---|---|
| [docs/PRINCIPLES.md](docs/PRINCIPLES.md) | 현재 제품 원칙과 Mneme/Madi 경계 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | legacy 및 Madi 구성요소와 데이터 흐름 |
| [docs/USAGE.md](docs/USAGE.md) | Madi CLI/Core 사용법과 legacy 사용법 |
| [docs/PLAYBOOK.md](docs/PLAYBOOK.md) | 실제 운영·checkpoint·복원 절차 |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Claude/Codex adapter 및 policy lifecycle |
| [docs/SETUP-NEW-PC.md](docs/SETUP-NEW-PC.md) | 새 머신 bootstrap |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 제품·아키텍처 결정 기록 |
| [D3 specification](docs/superpowers/specs/2026-08-26-madi-d3-vault-design.md) | 승인된 authoritative Vault 설계 |
| [Migration implementation plan](docs/superpowers/plans/2026-08-26-mneme-to-madi-migration.md) | 점진적 구현 계획과 dependency gate |
| [PROGRESS.md](PROGRESS.md) | 구현 진행 기록 |

## 현재 전환 상태

- Madi Vault Core, CLI, deterministic recall, policy enforcement, session revision,
  Memory lifecycle, Git sync primitives, Claude/Codex adapter가 구현되어 있습니다.
- Legacy Mneme HTTP MCP/Wiki/Growth 기능은 그대로 공존합니다.
- 저장소 및 Python package rename은 아직 하지 않았습니다.
- Remote push, PR, merge, publish는 별도 단계입니다.

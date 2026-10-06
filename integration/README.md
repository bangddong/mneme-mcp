# Agent integration

이 디렉터리에는 두 세대의 integration이 공존합니다.

| 경로 | 대상 | 상태 |
|---|---|---|
| `integration/madi/` | Madi Vault Core와 Claude/Codex 연결 | 현재 opt-in 경로 |
| `integration/install.py` 및 legacy templates | Mneme HTTP MCP/Wiki/Growth 연결 | 호환 경로 |

두 integration 모두 host project의 기존 설정을 보존하며, Madi 또는 Mneme 장애가 일반
agent 작업을 막지 않는 선택적 의존성을 지향합니다.

## Madi integration

### Claude

Claude adapter는 agent-neutral stdio transport와 project instruction block을 opt-in으로
설치합니다.

```powershell
$env:MADI_VAULT_ROOT = "D:\data\my-madi"
$env:MADI_STATE_HOME = "D:\local\madi-state"
$Project = (Resolve-Path -LiteralPath "D:\development\my-project").Path

# Current installer limitation: prepare these as regular files in the trusted
# project with your editor. Do not let this recipe create/follow link targets.
if (-not (Test-Path -LiteralPath "$Project\.mcp.json" -PathType Leaf)) {
  throw "Create a regular .mcp.json containing {} in the trusted project first."
}
if (-not (Test-Path -LiteralPath "$Project\CLAUDE.md" -PathType Leaf)) {
  throw "Create a regular CLAUDE.md in the trusted project first."
}

python integration/madi/install_claude.py --project $Project
```

설치기는 기존 `.mcp.json` server와 `CLAUDE.md` 내용을 보존하고 Madi 항목을 한 번만
추가합니다. Vault absolute path나 credential을 project repository에 기록하지 않습니다.
현재 installer의 conservative path preflight는 두 target file이 없는 것을 unsafe로
처리합니다. Trusted project에서 `.mcp.json`은 `{}`를 담은 regular file로,
`CLAUDE.md`는 빈 regular file로 먼저 준비해야 합니다. 위 preflight는 누락 시 쓰기 전에
중단하며, installer가 전체 path chain의 symlink/reparse 여부를 다시 검증합니다.
`MADI_VAULT_ROOT`와 `MADI_STATE_HOME`은 Claude를 시작하는 process가 상속해야 합니다.
위 예시는 현재 PowerShell process에만 적용됩니다. 지속 설정이 필요하면 machine-local
launcher나 OS user environment를 사용하고 project repository에는 경로를 commit하지 않습니다.

Claude installer는 native lifecycle hook을 설치하지 않습니다. Claude는 `CLAUDE.md`의
지침과 stdio MCP tools를 이용해 context/checkpoint를 명시적으로 요청합니다.

관련 파일:

- `madi/claude/mcp-stdio.json`
- `madi/claude/CLAUDE.md.template`
- `madi/install_claude.py`

### Codex

Codex adapter는 pinned native `PreCompact` payload를 agent-neutral lifecycle event로
변환합니다.

```powershell
$env:MADI_CODEX_WORKSTREAM_ID = "ws-1"
python integration/madi/install_codex.py --project D:\development\my-project
```

`PreCompact` event는 checkpoint 필요성을 알릴 뿐 Session revision을 자동 작성하지
않습니다. Transcript path, cwd, model, turn ID와 agent field를 열거나 portable artifact에
복사하지 않습니다. Workstream binding은 machine-local configuration에서만 가져옵니다.
`MADI_CODEX_WORKSTREAM_ID`는 Codex를 시작하는 process가 상속해야 하며 project file에
기록하지 않습니다. Hook output은 `checkpoint_required` diagnostic일 뿐입니다. 실제
checkpoint는 host가 sanitize한 payload를 CLI/stdio/Core command로 별도 제출합니다.

관련 파일:

- `madi/codex/hooks.json`
- `madi/codex/pre_compact_hook.py`
- `madi/codex/AGENTS.md.template`
- [Pinned schema and provenance](madi/codex/README.md)
- `madi/install_codex.py`

### 실제 checkpoint 흐름

```text
Claude: project instruction + stdio MCP tool ───────────────┐
Codex: native PreCompact → validation → checkpoint requested ├─→ host agent가 semantic content 선택·sanitize
                                                            │
                                                            └─→ explicit Core command
                                                                → policy/CAS 검증
                                                                → immutable Session revision
```

Raw transcript나 raw tool output은 이 흐름에서 portable Vault로 자동 유입되지 않습니다.
Adapter/Core 실패 시 host 작업은 계속되고 수동 checkpoint를 나중에 재시도할 수 있습니다.

## Legacy Mneme integration

기존 Claude 프로젝트를 HTTP MCP/Wiki/Growth 경로에 연결하려면 legacy installer를
사용합니다.

```powershell
python integration/install.py `
  --project D:\development\my-project `
  --agent my-project-builder `
  --category my-project `
  --prefix my-project-
```

설치기는 다음을 멱등적으로 추가합니다.

1. Project `.mcp.json`에 `http://localhost:8080/mcp` server 항목
2. Project `CLAUDE.md`에 Wiki/Growth tool 호출 규약

Legacy 수동 설치 파일:

- `mcp-snippet.json`
- `CLAUDE-mneme.md.template`

Legacy integration은 `python -m mneme.server`, 외부 Wiki, `.env`, 필요시 local LLM을
사용합니다. 이 조건들은 Madi Core의 필수 조건이 아닙니다.

## 선택 기준

- Cross-agent/cross-machine personal continuity가 목적이면 `integration/madi/`를 사용합니다.
- 기존 Wiki 인제스트 및 Growth workflow를 유지하려면 legacy integration을 사용합니다.
- Migration 중에는 둘을 동시에 사용할 수 있지만 같은 사실을 자동으로 양쪽에 복제하지
  않습니다.

구조와 policy 경계는 [docs/INTEGRATION.md](../docs/INTEGRATION.md), 기존 데이터 staging은
[docs/MIGRATION-MNEME-TO-MADI.md](../docs/MIGRATION-MNEME-TO-MADI.md)를 참고하세요.

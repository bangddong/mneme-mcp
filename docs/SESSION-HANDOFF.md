# 새 세션 인계 — Mac 개인 에이전트 + Madi

마지막 갱신: 2026-10-13

이 문서는 이전 대화의 작업 인계다. 제품 specification이나 설치 성공 보고가 아니다.
새 세션은 이 파일을 먼저 읽고, 실제 환경을 확인한 뒤 아래 목표를 이어간다.
설계 판단은 아래 Approved D3 specification을 따른다.

## 1. 현재 목표

사용자의 개인 Mac에서 실행되는 **개인 메인 에이전트에 Madi를 연결**한다.
개발·조사·학습을 함께 수행하고, 중요한 목표, 결정과 이유, 진행 상태,
blocker, next action을 남겨 다른 agent/session에서도 작업을 이어가는 것이 목적이다.

- Hermes Agent가 첫 검토 후보다. Octo Agent도 후보지만 최종 선정은 완료되지 않았다.
- Mac에서의 설치나 Madi 연동 성공은 아직 확인되지 않았다.
- 관심사 정리나 교육보다 **에이전트 연결과 검증을 먼저** 진행한다.
- 기존 Mneme→Madi migration을 다시 시작하거나 아키텍처를 재설계하지 않는다.

## 2. Repository 상태와 설계 authority

- Repository: https://github.com/bangddong/mneme-mcp
- Migration PR: https://github.com/bangddong/mneme-mcp/pull/7
- PR #7은 main에 squash merge됐다.
- Merge commit: `1856cf01bc9b2d0e7e2d4ebf827af6cd691151f0`
- 머지 직전 Ubuntu/Windows × Python 3.11/3.13 CI와 `ci-ok`가 통과했다.
  이것은 당시 결과이며 현재 checkout의 검증 결과로 간주하지 않는다.
- repository/package 이름은 여전히 `mneme-mcp` / `mneme`다.
- Binding authority:
  - [Approved D3 specification](superpowers/specs/2026-08-26-madi-d3-vault-design.md)
  - [Migration implementation plan](superpowers/plans/2026-08-26-mneme-to-madi-migration.md)

인계 작성 당시 Windows checkout의 `main`은 `origin/main` 대비 ahead 4 / behind 1이고,
기존 untracked 진단 파일도 있었다. 새 환경에서는 `git status`, branch, HEAD부터 확인한다.
기존 checkout을 무조건 pull/reset/cleanup하지 않는다. 새 clone에서 이 문서가 보이지 않는다면
문서 PR의 merge 여부와 clone한 branch를 확인한다.

## 3. 유지해야 할 Madi 핵심

- 사람이 private portable Git Vault를 소유한다.
- Markdown/YAML이 durable source of truth이고 SQLite/index는 재생성 가능하다.
- Project repository는 공식 project truth, Madi는 personal continuity를 소유한다.
- Core는 generation LLM 없이 동작한다.
- Checkpoint는 immutable Session revision이다. Git history만에 의존하지 않는다.
- Workstream Registry가 active heads / preferred head를 명시한다.
- CURRENT/PROFILE은 generated view다.
- Checkpoint persistence와 Git commit/push는 별개다.
- Machine-local 설정과 generated state는 portable Vault와 분리한다.
- Raw transcript/tool output/evidence/secrets를 portable Git에 자동 저장하지 않는다.
- MCP 연결만으로 자동 checkpoint가 보장되지 않는다. Host의 semantic selection,
  정제, 제출 동작까지 별도로 확인해야 한다.

## 4. Mac / iPhone 원격 접속 현황

확인된 사용자 상황:

- iPhone과 Mac은 Tailscale로 연결돼 있다.
- 사용자는 iPhone으로 Mac에 원격접속 중이라고 했다. 사용 앱은 미확인이다.
- 원하는 것은 Mac에서 Codex를 실행하고 다른 기기에서 작업을 이어가는 것이다.
- iPhone ChatGPT 앱을 사용하는 편한 방식을 선호한다.
- Mac의 QR을 같은 iPhone에서 보고 있어 카메라 스캔이 어려웠다.
- iPhone 스크린샷 → 사진 앱 → QR 길게 누르기로 링크를 여는 방법을 안내했다.
- 실제 페어링 성공 여부는 미확인이다.

이전 설명에서 웹사이트 / 모바일 Remote / CLI remote를 섞어 혼란이 있었다.
아래를 구분하고, 최신 공식 문서와 설치된 제품에서 실제 지원을 확인한다.

| 방식 | 설명과 주의점 |
| --- | --- |
| 공식 모바일 Remote | Mac 데스크톱 앱의 Connections에서 설정하고 iPhone과 페어링하는 경로를 확인했다. 메뉴·제공 여부는 계정과 설치 버전에서 재확인한다. 임의의 standalone CLI 세션이 자동으로 보인다고 단정하지 않는다. |
| CLI remote | `app-server` + `codex --remote`는 별도 experimental 경로다. 기존 CLI 세션에 자동 attach되는 것으로 설명하지 않는다. |
| SSH + tmux | 같은 터미널 화면에 재접속하는 별도 방법이다. 사용자는 현재 ChatGPT 앱을 더 선호한다. |
| ChatGPT 웹사이트 | 기존 Mac CLI 세션에 직접 attach하는 공식 경로는 확인하지 못했다. Work Cloud의 local computer access와 기존 CLI 세션 attach를 혼동하지 않는다. |

이전 조사에 사용한 공식 문서:

- https://learn.chatgpt.com/docs/remote-connections
- https://learn.chatgpt.com/docs/app-server

QR, pairing code, 인증 토큰은 대화나 이 문서에 기록하지 않는다.

## 5. 개인 에이전트 후보와 연결점

### Hermes

- Repository: https://github.com/NousResearch/hermes-agent
- 설치 문서: https://hermes-agent.nousresearch.com/docs/getting-started/installation/
- MCP 문서: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/
- 초기 문서 조사에서 stdio MCP 연결 구조가 기존 Madi와 맞는 것으로 확인했다.
- 실제 설치·설정·호환성 검증은 하지 않았다. Mac CPU/OS와 지원 범위부터 확인한다.
- Hermes의 native memory는 Madi와 함께 사용할 수 있다.

### Octo

- 후보 repository: https://github.com/open-octo/octo-agent
- 사용자가 말한 Octo가 이 프로젝트인지, 실제 MCP 호환성이 어떤지는 추가 확인이 필요하다.
- 상세 비교나 설치 검증은 아직 하지 않았다.

### 기존 Madi transport

원격 main의 코드를 확인한 stdio entry point:

```sh
python -m mneme.transports.mcp_stdio
```

필요한 machine-local 환경변수:

```text
MADI_VAULT_ROOT=<portable Vault 경로>
MADI_STATE_HOME=<별도 machine-local state 경로>
```

제공 tool: `madi_context`, `madi_recall`, `madi_remember`,
`madi_checkpoint`, `madi_decision`.

Madi 전용 Python 환경의 interpreter를 agent MCP 설정에 명시한다.
기존 Vault가 있으면 덮어쓰거나 재초기화하지 않는다. Bootstrap 방법은
[USAGE](USAGE.md), [새 PC 설정](SETUP-NEW-PC.md), 실제 Core API를 확인한다.
외부 agent 설정 파일의 수정 대상은 먼저 사용자와 경로를 확인한다.

## 6. 다음 세션의 실행 순서와 완료 기준

첫 응답은 목표와 미확인 상태를 짧게 요약하고 다음부터 진행한다.

1. 현재 OS, Mac 접근 가능 여부, repository branch/HEAD/status를 확인한다.
2. Mac의 CPU/OS, Codex/데스크톱 앱/개인 에이전트 설치 상태와 페어링 결과를 확인한다.
3. 현재 도구로 Mac에 접근할 수 없다면 그 사실을 밝히고, 사용자가 실행할 최소 확인 명령을 제공한다.
   이전 Windows 세션에는 Mac 접근 endpoint가 제공되지 않았다.
4. 개인 에이전트 선택을 확인하고 기존 설치를 재사용하거나 필요한 설치를 진행한다.
5. Madi stdio MCP를 연결하고 아래 완료 기준을 실제로 검증한다.

연결 완료 기준:

- Mac에서 개인 에이전트와 대화 가능.
- Agent가 Madi `context` / `recall`을 성공적으로 호출.
- Host가 작성·정제한 테스트 checkpoint 저장 성공.
- 재시작 후 저장된 상태를 읽어 작업을 이어갈 수 있음.

이미 확인된 질문을 반복하지 말고, 성공하지 않은 단계를 성공했다고 말하지 않는다.

## 7. 작업 범위와 소통

- 한국어로 짧고 직접적으로 설명한다. 긴 일방 강의를 하지 않는다.
- 기존 파일, 설정, durable data와 unrelated 변경을 보존한다.
- 외부 경로는 자동으로 writable workspace로 간주하지 않는다.
- 큰 refactor, repository/package rename, 무단 push/merge는 하지 않는다.
- API key, credentials, pairing 정보를 portable Vault나 repository에 넣지 않는다.
- 설치 스크립트, 외부 서비스 설정, 네트워크 노출은 실제 대상과 영향을 확인한다.

연결 검증 이후 기술 중심 교육을 재개할 수 있다. 사용자는 짧은 설명과 잦은 상호작용,
명확하고 엄격한 0~10점 채점, 피드백 마지막의 10점 모범 답안을 원한다.
목표는 면접관급의 시니어 타인에게 시스템을 설명하고 도그푸딩 문제를 구체적으로
보고할 수 있는 수준이다. 이직 준비를 목적으로 표현하지 않는다.

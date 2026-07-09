# mneme-mcp

로컬 상시 가동 MCP 서버 — 여러 AI 에이전트(Claude Code 등)가 개인 지식 베이스(Wiki)를 읽고
쓰며, **LLM 가중치를 건드리지 않고 외부 기억·스킬·가치만 갱신해 스스로 성장**하는 게이트웨이
두뇌입니다. 이 성장 프레임워크를 **MNEME**(므네메)라고 부릅니다.

## 30초 요약

- mneme는 `http://localhost:8080/mcp`에서 도는 **MCP 서버**입니다. Claude Code가 여기에 붙어 **13개 도구**를 씁니다.
- 진짜 지식은 `E:/development/wiki/`의 **마크다운 파일**(= L2 기억)에 있습니다. mneme는 그것을 검색·병합·검증합니다.
- 에이전트가 일을 하고 결과를 **반성(`episode_reflect`)** 으로 돌려주면, mneme가 **스킬 신뢰도(성향)** 를 조금씩 학습합니다.
- 단, 성장은 **헌법(CIB 게이트)** 을 통과할 때만 반영됩니다. 정직성·안전 같은 핵심을 깨는 성장은 차단됩니다.
- 백그라운드에서 주기적으로 **스킬 생애주기 정산(Outer Loop)** 과 **자기 점검(self_model)** 이 돌며, 비정상 성장을 감지하면 경보합니다.
- LLM 호출은 전부 **로컬 모델(Ollama)** 로 처리되어 API 토큰 과금이 없습니다. 모델이 꺼져 있어도 서버는 보수적으로 동작합니다.

## 문서

처음 쓰신다면 위에서 아래 순서로 읽으시면 됩니다.

| 문서 | 내용 |
|------|------|
| [docs/PLAYBOOK.md](docs/PLAYBOOK.md) | **사용자 플레이북** — "지금 뭘 하면 되지?" 상황별 가이드 (공부·인제스트·검색·알림·점검·확장) |
| [docs/USAGE.md](docs/USAGE.md) | **사용 방법** — 설치·기동, 일상 사용 시나리오, 관찰·운영, 트러블슈팅, 빠른 시작 |
| [docs/PRINCIPLES.md](docs/PRINCIPLES.md) | **동작 원리** — 5층 기억, 5요소 성장, 성장 수식, 헌법/CIB 안전 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | **아키텍처** — 구성요소, 데이터 흐름, 파일·DB 위치, 테이블 |
| [PROGRESS.md](PROGRESS.md) | 개발 진행 기록·TODO |

## 빠른 시작

```bash
pip install fastmcp watchdog httpx apscheduler python-dotenv pyyaml

# 로컬 LLM 런타임 (무과금). Ollama 권장:
ollama pull qwen2.5:7b      # 또는 하드웨어에 맞는 모델 (3B~8B)

cp .env.example .env
# .env에서 LLM_BASE_URL / LLM_MODEL 확인 (기본 Ollama)

python -m mneme.server
# → http://localhost:8080/mcp
```

> LLM 호출(검색 후보 선별·충돌 판단·요약·coherence 채점·반성·Outer Loop CI)은 모두
> 로컬 런타임(OpenAI 호환 `/chat/completions`)을 거칩니다. 런타임이 꺼져 있어도 서버는
> 보수적 fallback으로 무중단 동작합니다. NPU를 쓰려면 `LLM_BASE_URL`만 Foundry Local 등으로 교체하세요.

자세한 설치·연결 방법은 [docs/USAGE.md](docs/USAGE.md)를 참고하세요.

### Docker로 기동

Ollama까지 한 번에 띄우려면:

```bash
WIKI_HOST_DIR=/path/to/your/wiki docker compose up -d
docker compose exec ollama ollama pull qwen2.5:7b
# → http://localhost:8080/mcp
```

위키 경로를 생략하면 repo 안의 빈 `./wiki` placeholder로 시작합니다.
상태 DB는 `mneme-data` 볼륨에 보존됩니다.

## Claude Code 연결

프로젝트 `.mcp.json` 또는 `~/.claude.json`의 mcpServers에 추가합니다.

```json
{ "mcpServers": { "mneme": { "type": "http", "url": "http://localhost:8080/mcp" } } }
```

> MCP 서버는 Claude Code 재시작 시점에 연결됩니다. 새 도구를 추가했다면 Claude Code를 껐다 켜세요.

## MCP Tools

| Tool | 설명 |
|------|------|
| `wiki_search` | FTS5 + 로컬 LLM으로 관련 문서 검색 |
| `wiki_get` | 특정 파일 전문 조회 |
| `wiki_inject` | 파일 생성 또는 병합 (충돌 자동 판단) |
| `wiki_list` | 인덱스 목록 조회 (prefix 필터 가능) |
| `wiki_lint` | L2 무결성 검사 (형식·깨진링크·고아·index·stale) |
| `skill_seed` / `skill_suggest` | L3 스킬 시드 / 작업 관련 스킬 추천 |
| `episode_reflect` | Inner Loop: 결과 반성 → CIB 게이트로 성향 갱신 |
| `outer_loop_run` / `outer_loop_status` | Outer Loop: 스킬 생명주기 정산 / 사이클·상태 조회 |
| `self_model_status` | L5 자기 인식: 보정오차·성장속도·난이도 시계열 + 성장 경보 |
| `curriculum_suggest` | 내재적 동기: 난이도 스칼라 + 성장 타깃 추천 |
| `mneme_status` | 서버 상태 및 통계 |

## wiki/ 디렉터리

Obsidian 등으로 직접 편집해도 watchdog이 자동으로 FTS5 인덱스를 갱신합니다.
지식 베이스는 mneme 레포 바깥(`E:/development/wiki/`)에서 별도로 관리됩니다.

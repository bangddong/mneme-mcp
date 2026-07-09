# mneme 연동 키트 (Integration Kit)

새 Claude Code 프로젝트에서 **외부 성장 두뇌 mneme**를 쓰기 위한 최소 배선.
mneme는 MCP 서버라 도구 자체는 떠 있지만, 에이전트가 *제대로* 쓰려면
(어떤 agent 이름으로 / 작업 전·후 어떤 도구를 / 어느 위키 카테고리에) 두 가지가 필요하다:

1. **`.mcp.json`** — 프로젝트가 mneme 서버를 보게 하는 연결 1줄
2. **CLAUDE.md 규약 블록** — 에이전트가 따라야 할 호출 프로토콜

이 키트가 둘 다 자동으로 넣어준다.

> mneme는 **선택적 의존**이다. 서버가 꺼져 있으면 도구가 안 보일 뿐, 프로젝트는 평소대로 동작한다.

---

## 빠른 설치 (1단계, 크로스플랫폼)

Python은 mneme의 필수 의존이라 어느 OS든 보장된다 → OS별 셸 스크립트 대신 단일 `install.py`로
Windows/Linux/macOS를 모두 커버한다.

```bash
python E:/development/mneme-mcp/integration/install.py \
  --project  E:/development/my-new-proj \
  --agent    my-proj-builder \   # episodes.agent 에 기록될 이름
  --category my-new-proj \       # 위키 카테고리
  --prefix   mnp-                # 이 프로젝트 스킬 접두어
```

스크립트가 하는 일 (멱등 — 다시 돌려도 안전):
- `<project>/.mcp.json` 에 mneme 서버 항목 머지 (기존 서버 보존)
- `<project>` CLAUDE.md 에 mneme 규약 블록 추가 (`{{AGENT_NAME}}` 등 치환)

---

## 수동 설치 (스크립트 없이)

1. `mcp-snippet.json` 의 `mneme` 항목을 프로젝트 `.mcp.json` 의 `mcpServers` 에 붙인다.
2. `CLAUDE-mneme.md.template` 을 복사해 `{{AGENT_NAME}}`/`{{WIKI_CATEGORY}}`/`{{SKILL_PREFIX}}` 를
   채운 뒤, 프로젝트 CLAUDE.md 맨 아래에 붙인다.

---

## 설치 후 남는 수동 단계

| 단계 | 내용 |
|------|------|
| 위키 카테고리 | `E:/development/wiki/<Category>/` 생성 (기존 카테고리 구조 복사: `CLAUDE.md`/`index.md`/`log.md`/`pages/`/`sources/`) + 루트 `wiki/index.md`·`CLAUDE.md` 표에 행 추가 |
| 서버 가동 | `python -m mneme.server` (자동시작 설정 시 부팅 시 자동) |
| 확인 | Claude Code 재시작 → 도구 16개 노출 (`mneme_status` 호출) |

---

## 키트 파일

| 파일 | 역할 |
|------|------|
| `install.py` | 1단계 설치 스크립트 (크로스플랫폼 — Win/Linux/macOS) |
| `mcp-snippet.json` | 수동 머지용 `.mcp.json` 조각 |
| `CLAUDE-mneme.md.template` | 프로젝트 CLAUDE.md 에 넣을 규약 블록 (치환 변수 포함) |

규약 자세히는 템플릿 본문 참고. 동작 원리·아키텍처는 `../docs/` 참고.

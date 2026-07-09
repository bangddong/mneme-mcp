# 새 PC 설치 가이드 (Migration)

> 다른 PC에서 mneme를 처음부터 띄우는 절차입니다. 기존 PC의 경험·지식을 그대로
> 이어가려면 **0단계(원격 백업)** 가 반드시 선행돼야 합니다.

---

## 0단계 — (기존 PC에서) 원격 백업 ★필수★

시스템은 **3개 repo**로 구성되며, 가치는 누적된 **위키(지식)** 와 **state.db(스킬·성장)** 에 있습니다.
2026-06-29 기준 **세 repo 모두 GitHub private 원격에 올라가 있습니다** — 즉 0단계는 (state.db 제외) 완료 상태.

| 대상 | 역할 | 원격 | 비고 |
|------|------|------|------|
| `mneme-mcp/` | 두뇌·허브 (코드) | ✅ `bangddong/mneme-mcp` | |
| `llm-wiki/` | 지식 자산 (마크다운) | ✅ `bangddong/llm-wiki` | **진짜 자산** |
| `wiki-agent/` | 조회 봇 (Track 2, 코드만) | ✅ `bangddong/wiki-agent` | 상태 0, 선택적 |
| `state.db` | 성장 데이터 (스킬·에피소드) | ❌ git 제외(`memory/`) | **수동 복사 필요** |

> `state.db`만 git 밖에 있습니다(용량·민감도). 이사 시 기존 PC의 `mneme-mcp/memory/state.db`를
> 새 PC 같은 위치로 **수동 복사**하세요. 위키(L2)·코드는 git clone으로 충분합니다.

---

## 1단계 — 새 PC에 필수 소프트웨어 설치

1. **Python 3.13** (또는 3.11+) — 설치 시 "Add to PATH" 체크
2. **Ollama** — https://ollama.com (설치하면 보통 자동 시작 등록됨)
3. **git**

설치 확인:
```bash
python --version
ollama --version
git --version
```

---

## 2단계 — 코드·위키 내려받기

```bash
# 원하는 위치에 (예: D:\dev) — 경로는 새 PC에 맞게
git clone https://github.com/bangddong/mneme-mcp.git
git clone https://github.com/bangddong/llm-wiki.git
git clone https://github.com/bangddong/wiki-agent.git   # 선택: Discord 조회 봇(Track 2)
```
> ⚠️ 새 PC의 경로는 기존 `E:/development/...` 과 다를 수 있습니다. 아래 .env·런처에서
> 경로를 새 위치로 바꿔주세요.

---

## 3단계 — 의존성 + 모델

```bash
cd mneme-mcp
pip install fastmcp watchdog httpx apscheduler python-dotenv pyyaml

ollama pull qwen2.5:7b      # 하드웨어에 맞는 모델 (3B~8B)
```

---

## 4단계 — `.env` 작성 (경로를 새 PC에 맞게)

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b
WIKI_DIR=D:/dev/llm-wiki      # ★ clone한 llm-wiki 경로로 변경
DB_PATH=./memory/state.db     # 상대경로라 그대로 OK
MCP_HOST=0.0.0.0
MCP_PORT=8080
OUTER_LOOP_INTERVAL_MIN=10
```
`cp .env.example .env` 후 위 값(특히 `WIKI_DIR`)을 수정합니다.
(기존 PC의 `state.db`를 새 PC `mneme-mcp/memory/`에 복사하면 성장 데이터까지 이어집니다.)

---

## 5단계 — 자동 시작 설정 (Windows)

새 PC에서도 부팅 시 자동으로 뜨게 합니다.

**(a) `mneme-start.bat` 의 python 경로 확인**
```bat
@echo off
cd /d "%~dp0"
python -m mneme.server >> "%~dp0memory\server.log" 2>&1
```
> python이 PATH에 있으면 위처럼 `python`으로 충분합니다. 안 잡히면 `where python`으로
> 전체 경로를 찾아 그 경로로 바꿔주세요.

**(b) `mneme-startup.vbs` 의 bat 경로를 새 위치로 수정**
```vbs
CreateObject("WScript.Shell").Run """D:\dev\mneme-mcp\mneme-start.bat""", 0, False
```

**(c) 시작프로그램 폴더에 VBS 복사**
- `Win + R` → `shell:startup` → 열린 폴더에 `mneme-startup.vbs` 복사
- Ollama는 설치 시 보통 자동 등록됨. 없으면 Ollama 바로가기도 이 폴더에 추가.

---

## 6단계 — Claude Code 연결

각 프로젝트의 `.mcp.json` (또는 `~/.claude.json`):
```json
{ "mcpServers": { "mneme": { "type": "http", "url": "http://localhost:8080/mcp" } } }
```
> mneme는 HTTP 서버라 한 번 띄우면 그 PC의 모든 프로젝트가 같은 URL로 공유합니다.

---

## 7단계 — 검증 체크리스트

1. `ollama list` → 모델 보이는지
2. `python -m mneme.server` 수동 실행 → `http://localhost:8080/mcp` 뜨는지
3. 한 번 재부팅 → 로그인 후 자동으로 mneme·Ollama 떴는지 (`memory/server.log` 확인)
4. Claude Code 재시작 → 도구 13개 (`mneme_status` 호출)
5. `wiki_list` → 옮겨온 위키 카테고리·페이지가 인덱싱됐는지

---

## 핵심 요약

> **코드·위키를 원격에 올려두는 것(0단계)이 이사의 90%입니다.** 새 PC에선 clone →
> deps·모델 설치 → `.env`·런처 **경로만 새 위치로** 고치면 그대로 이어집니다.

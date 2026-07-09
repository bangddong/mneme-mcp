#!/usr/bin/env python3
"""mneme(외부 성장 두뇌)를 새 Claude Code 프로젝트에 연동한다 (크로스플랫폼).

Python은 mneme의 필수 의존(서버가 Python)이라 어느 OS든 보장되므로,
OS별 셸 스크립트 대신 이 단일 스크립트로 Windows/Linux/macOS를 모두 커버한다.

하는 일 (멱등 — 다시 돌려도 안전):
  1) <project>/.mcp.json 에 mneme 서버 항목 머지 (기존 서버 보존)
  2) <project> CLAUDE.md 에 mneme 사용 규약 블록 추가 (agent명/카테고리/접두어 치환)

예:
  python install.py --project ../my-new-proj --agent my-proj-builder \\
                    --category my-new-proj --prefix mnp-
"""
import argparse
import json
from pathlib import Path

KIT = Path(__file__).resolve().parent


def merge_mcp(project: Path, mneme_url: str):
    mcp_path = project / ".mcp.json"
    if mcp_path.exists():
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if "mneme" in servers:
        print("  .mcp.json : mneme 이미 존재 → 건너뜀")
        return
    servers["mneme"] = {"type": "http", "url": mneme_url}
    mcp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("  .mcp.json : mneme 서버 추가 완료")


def append_rules(project: Path, agent: str, category: str, prefix: str):
    # 대상 CLAUDE.md: 루트에 있으면 그것, 없으면 .claude/CLAUDE.md, 둘 다 없으면 루트 신규
    root_md = project / "CLAUDE.md"
    claude_md = project / ".claude" / "CLAUDE.md"
    if root_md.exists():
        target = root_md
    elif claude_md.exists():
        target = claude_md
    else:
        target = root_md

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if "## mneme" in existing:
        print(f"  CLAUDE.md : mneme 규약 이미 존재 → 건너뜀 ({target})")
        return

    tpl = (KIT / "CLAUDE-mneme.md.template").read_text(encoding="utf-8")
    tpl = "\n".join(l for l in tpl.splitlines() if not l.startswith("<!--"))
    block = (tpl.replace("{{AGENT_NAME}}", agent)
                .replace("{{WIKI_CATEGORY}}", category)
                .replace("{{SKILL_PREFIX}}", prefix))
    sep = "\n\n" if existing.strip() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(existing.rstrip() + sep + block.rstrip() + "\n", encoding="utf-8")
    print(f"  CLAUDE.md : mneme 규약 추가 완료 ({target})")


def main():
    p = argparse.ArgumentParser(description="mneme 연동 설치 (크로스플랫폼)")
    p.add_argument("--project", required=True, help="대상 프로젝트 루트 경로")
    p.add_argument("--agent", required=True, help="에이전트 이름 (episodes.agent 기록)")
    p.add_argument("--category", required=True, help="위키 카테고리")
    p.add_argument("--prefix", required=True, help="스킬 접두어 (예: mnp-)")
    p.add_argument("--mneme-url", default="http://localhost:8080/mcp")
    args = p.parse_args()

    project = Path(args.project).resolve()
    if not project.exists():
        raise SystemExit(f"프로젝트 경로 없음: {project}")

    merge_mcp(project, args.mneme_url)
    append_rules(project, args.agent, args.category, args.prefix)

    print("\n연동 완료. 남은 수동 단계:")
    print(f"  1) 위키 카테고리 생성 (wiki/{args.category}/): "
          "기존 카테고리 구조 복사 + 루트 index.md·CLAUDE.md 표에 행 추가")
    print("  2) mneme 서버 가동:  python -m mneme.server  (자동시작 설정 시 부팅 시 자동)")
    print("  3) Claude Code 재시작 → 도구 16개 노출 확인 (mneme_status 호출)")


if __name__ == "__main__":
    main()

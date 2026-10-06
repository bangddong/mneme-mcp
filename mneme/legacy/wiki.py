import os
from pathlib import Path
from datetime import date, datetime, timezone


def get_wiki_dir() -> Path:
    return Path(os.getenv("WIKI_DIR", "./wiki"))


def read_file(path: str) -> dict | None:
    full_path = get_wiki_dir() / path
    if not full_path.exists():
        return None
    content = full_path.read_text(encoding="utf-8")
    stat = full_path.stat()
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {"path": path, "content": content, "updated_at": updated_at}


def write_file(path: str, content: str):
    full_path = get_wiki_dir() / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")


def delete_file(path: str):
    full_path = get_wiki_dir() / path
    if full_path.exists():
        full_path.unlink()


def update_reserved_files(path: str, title: str, description: str,
                          source_agent: str, action: str = "Creation"):
    """페이지 생성/병합 시 카테고리 index.md·log.md를 코드가 결정적으로 갱신한다.

    OKF 예약 파일 형식(호출자 LLM에게 맡기지 않음 — 규약 위반 재발 방지):
    - index.md: `* [제목](카테고리 상대경로) - 설명` 불릿 (Creation 시, 중복 경로면 생략)
    - log.md: `## YYYY-MM-DD` 헤딩(최신이 위) 아래 `* **Creation|Update**: [경로](경로) — 소스`
    """
    parts = path.replace("\\", "/").split("/")
    category, rel = parts[0], "/".join(parts[1:])
    cat_dir = get_wiki_dir() / category
    today = date.today().isoformat()

    if action == "Creation":
        index_path = cat_dir / "index.md"
        bullet = f"* [{title}]({rel}) - {description}".rstrip(" -")
        if index_path.exists():
            text = index_path.read_text(encoding="utf-8")
            if f"({rel})" not in text:
                index_path.write_text(text.rstrip("\n") + f"\n{bullet}\n", encoding="utf-8")
        else:
            index_path.write_text(
                f"# {category} — 페이지 인덱스\n\n## Pages\n{bullet}\n", encoding="utf-8"
            )

    log_path = cat_dir / "log.md"
    entry = f"* **{action}**: [{rel}]({rel}) — {source_agent}"
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
    else:
        text = f"# {category} — 인제스트 로그\n"
    heading = f"## {today}"
    lines = text.splitlines()
    if heading in lines:
        # 오늘 헤딩 아래(빈 줄 건너뛰고)에 삽입
        j = lines.index(heading) + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        lines.insert(j, entry)
    else:
        # 최신이 위 — 제목 줄 다음, 기존 날짜 헤딩들 앞에 삽입
        insert_at = 1 if lines and lines[0].startswith("#") else 0
        lines[insert_at:insert_at] = ["", heading, "", entry]
    log_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def is_content_page(path: str) -> bool:
    """콘텐츠 페이지인지 판정 (스캐폴딩 제외).

    제외 대상: `.claude/` 하위, OKF 예약 파일(`index.md`/`log.md`),
    basename이 `_`로 시작(`_roadmap.md` 등), `CLAUDE.md`.
    → FTS/인덱스에는 실제 지식 페이지만 등록한다.
    """
    parts = path.replace("\\", "/").split("/")
    if ".claude" in parts:
        return False
    name = parts[-1]
    if name in ("CLAUDE.md", "index.md", "log.md") or name.startswith("_"):
        return False
    return True


def list_md_files(content_only: bool = False) -> list[str]:
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return []
    files = [
        str(p.relative_to(wiki_dir)).replace("\\", "/")
        for p in wiki_dir.rglob("*.md")
    ]
    if content_only:
        files = [f for f in files if is_content_page(f)]
    return files

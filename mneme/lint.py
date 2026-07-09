"""L2 무결성 게이트 — 결정론적 기계 lint.

knot(netwaif/knot)의 scripts/lint.py 아이디어를 흡수하되, 규칙은 우리 wiki
스키마(OKF v0.1 정합 + 카테고리 격리형: type/title/timestamp/tags frontmatter +
Summary/Details/Sources/Related 섹션 + 번들루트 절대경로 마크다운 링크,
[[..]] 위키링크는 레거시 허용)에 맞춘다.

순수 파이썬 — ANTHROPIC_API_KEY 없이도 동작한다. (내용 모순 같은 의미 검사는
AI 영역이므로 여기서 제외하고 llm.judge_conflict / lint.md 프롬프트가 담당.)

사용:
    from mneme.lint import lint_wiki
    result = lint_wiki(category=None)   # 전체 / 또는 "ai-llm"
    # result = {"errors": [...], "warnings": [...], "info": [...], "stats": {...}}

CLI:
    python -m mneme.lint [category]    # errors 있으면 exit 1
"""

import re
import sys
from datetime import date, datetime
from pathlib import Path

from mneme.wiki import get_wiki_dir

REQUIRED_FRONTMATTER = ("type", "title", "timestamp", "tags")
REQUIRED_SECTIONS = ("Summary", "Details", "Sources", "Related")
# OKF type 어휘 (wiki/CLAUDE.md "문서 type 어휘" 표와 동기)
TYPE_VOCAB = ("concept", "source", "lesson", "roadmap", "schema", "doc")
# visibility 어휘 (선택 필드, 없으면 private 취급 — wiki/CLAUDE.md와 동기)
# 지금은 마킹만 강제(어휘 검사). 공개 파이프라인/공유 게이트가 붙을 때 이 값을 읽는다.
VISIBILITY_VOCAB = ("public", "shared", "private")
STALE_DAYS = 180
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# [[target]] 또는 [[target|표시명]] — 레거시 위키링크 (stub 줄은 호출부에서 제외)
LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# [표시명](target.md) — OKF 표준 마크다운 링크 (외부 URL 제외)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+\.md)\)")
# index.md 항목: OKF 불릿 `* [제목](경로.md) - 설명` (레거시 표 행도 허용)
INDEX_BULLET_RE = re.compile(r"^\*\s*\[[^\]]*\]\(([^)\s]+\.md)\)")
INDEX_ROW_RE = re.compile(r"^\|\s*([^\s|]+\.md)\s*\|")


def _list_categories(wiki_dir: Path) -> list[str]:
    """wiki 루트 1단계 디렉토리(.git/.claude 등 닷 디렉토리 제외)를 카테고리로 본다."""
    return sorted(
        p.name
        for p in wiki_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _category_pages(wiki_dir: Path, category: str) -> list[Path]:
    """카테고리의 콘텐츠 페이지(pages/**/*.md)."""
    pages_dir = wiki_dir / category / "pages"
    if not pages_dir.exists():
        return []
    return sorted(pages_dir.rglob("*.md"))


def _parse_frontmatter(text: str) -> dict | None:
    """첫 --- 블록을 얕게 파싱. 없으면 None."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end].strip("\n")
    fm: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """(frontmatter 블록 원문 — 구분자 `---` 포함, 본문) 튜플로 분리.

    frontmatter가 없으면 (None, 원문). LLM 병합 시 frontmatter를 코드로
    보존하기 위한 헬퍼 (에러 로그 07-06 개선 ②).
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    close = end + len("\n---")
    return text[:close], text[close:].lstrip("\n")


def lint_page_text(text: str, label: str = "content") -> list[str]:
    """콘텐츠 페이지 하나의 형식을 검사해 오류 목록 반환 (파일 불필요).

    frontmatter 존재/필수 필드/type 어휘/timestamp 형식 + 필수 섹션.
    lint_wiki(디스크 전수 검사)와 wiki_inject(쓰기 전 게이트)가 공유한다.
    링크 해석은 디스크 문맥이 필요하므로 여기서 제외.
    """
    errors: list[str] = []
    fm = _parse_frontmatter(text)
    if fm is None:
        errors.append(f"[{label}] frontmatter 없음")
    else:
        for key in REQUIRED_FRONTMATTER:
            if key not in fm or not fm[key]:
                errors.append(f"[{label}] frontmatter 누락: {key}")
        doc_type = fm.get("type", "")
        if doc_type and doc_type not in TYPE_VOCAB:
            errors.append(
                f"[{label}] type 어휘 밖: {doc_type} (허용: {', '.join(TYPE_VOCAB)})"
            )
        upd = fm.get("timestamp", "")
        if upd and not DATE_RE.match(upd):
            errors.append(f"[{label}] timestamp 형식 오류(YYYY-MM-DD): {upd}")
        vis = fm.get("visibility", "")
        if vis and vis not in VISIBILITY_VOCAB:
            errors.append(
                f"[{label}] visibility 어휘 밖: {vis} (허용: {', '.join(VISIBILITY_VOCAB)})"
            )
    for sec in REQUIRED_SECTIONS:
        if not re.search(rf"^##\s+{sec}\b", text, re.MULTILINE):
            errors.append(f"[{label}] 필수 섹션 누락: ## {sec}")
    return errors


def _parse_index_entries(index_path: Path) -> list[str]:
    """카테고리 index.md에서 페이지 경로(카테고리 상대) 목록 추출.

    OKF 불릿(`* [제목](경로) - 설명`)이 표준, 레거시 표 행도 허용.
    """
    if not index_path.exists():
        return []
    entries = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        m = INDEX_BULLET_RE.match(line.strip()) or INDEX_ROW_RE.match(line.strip())
        if m:
            entries.append(m.group(1).replace("\\", "/"))
    return entries


def _extract_links(text: str) -> list[str]:
    """<!-- stub --> 마커가 없는 줄에서 링크 타깃 수집.

    OKF 마크다운 링크(`[x](path.md)`)와 레거시 위키링크(`[[..]]`) 모두.
    외부 URL(`://`)은 제외.
    """
    targets = []
    for line in text.splitlines():
        if "<!-- stub -->" in line:
            continue
        for m in LINK_RE.finditer(line):
            targets.append(m.group(1).strip())
        for m in MD_LINK_RE.finditer(line):
            t = m.group(1).strip()
            if "://" not in t:
                targets.append(t)
    return targets


def _resolve_link(target: str, source_file: Path, wiki_dir: Path,
                  category_pages: list[Path]) -> Path | None:
    """링크 타깃을 실제 파일로 해석. 없으면 None.

    - `/`로 시작: 번들(위키) 루트 절대경로 — OKF 권장
    - 경로형(`/` 또는 `..` 포함): 링크가 있는 파일 기준 상대 경로 해석
    - 단순 이름(`[[page-name]]`): 같은 카테고리 pages/** 에서 basename 매칭
    """
    if target.startswith("/"):
        resolved = (wiki_dir / target.lstrip("/")).resolve()
        return resolved if resolved.is_file() else None
    if "/" in target or target.startswith(".."):
        base = source_file.parent
        for cand in (target, target + ".md"):
            resolved = (base / cand).resolve()
            if resolved.exists() and resolved.is_file():
                return resolved
        return None
    # 단순 이름
    stem = target[:-3] if target.endswith(".md") else target
    for p in category_pages:
        if p.stem == stem:
            return p
    return None


def lint_wiki(category: str | None = None) -> dict:
    """wiki 무결성 검사. category=None이면 전체 카테고리."""
    wiki_dir = get_wiki_dir().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    if not wiki_dir.exists():
        return {"errors": [f"wiki dir not found: {wiki_dir}"],
                "warnings": [], "info": [], "stats": {}}

    categories = [category] if category else _list_categories(wiki_dir)
    today = date.today()
    total_pages = 0

    for cat in categories:
        cat_dir = wiki_dir / cat
        if not cat_dir.is_dir():
            errors.append(f"[{cat}] 카테고리 디렉토리 없음")
            continue

        pages = _category_pages(wiki_dir, cat)
        total_pages += len(pages)

        index_path = cat_dir / "index.md"
        index_entries = _parse_index_entries(index_path)
        if not index_path.exists():
            warnings.append(f"[{cat}] index.md 없음")

        # 백링크 집계용: 해석된 타깃 → 들어오는 링크 수
        backlinks: dict[Path, int] = {p: 0 for p in pages}
        # 페이지별 (frontmatter, timestamp 날짜) 캐시 — stale 정렬용
        page_updated: dict[Path, date | None] = {}

        listed = set()  # index.md에 등재된 (카테고리 상대) 경로

        for page in pages:
            rel = page.relative_to(cat_dir).as_posix()
            text = page.read_text(encoding="utf-8")

            # --- 형식 검사 (wiki_inject 게이트와 동일 규칙) ---
            errors.extend(lint_page_text(text, label=f"{cat}/{rel}"))
            fm = _parse_frontmatter(text)
            if fm is not None:
                upd = fm.get("timestamp", "")
                try:
                    page_updated[page] = (
                        datetime.strptime(upd, "%Y-%m-%d").date() if DATE_RE.match(upd) else None
                    )
                except ValueError:
                    page_updated[page] = None

            # --- 깨진 링크 검사 (stub 제외) ---
            for target in _extract_links(text):
                resolved = _resolve_link(target, page, wiki_dir, pages)
                if resolved is None:
                    errors.append(f"[{cat}/{rel}] 깨진 링크: [[{target}]] "
                                  f"(미래 페이지면 같은 줄에 <!-- stub --> 표기)")
                elif resolved in backlinks:
                    backlinks[resolved] += 1

            # --- index 동기화: 페이지가 index.md에 있나 ---
            if index_entries and rel not in index_entries:
                warnings.append(f"[{cat}/{rel}] index.md 미등재 (고아 — 인덱스)")
            if rel in index_entries:
                listed.add(rel)

        # --- index 동기화: 유령/중복 ---
        seen = set()
        for entry in index_entries:
            if entry in seen:
                warnings.append(f"[{cat}] index.md 중복 항목: {entry}")
            seen.add(entry)
            if not (cat_dir / entry).exists():
                errors.append(f"[{cat}] index.md 유령 항목(파일 없음): {entry}")

        # --- 고아(백링크 0) + stale ---
        stale_candidates = []
        for page in pages:
            rel = page.relative_to(cat_dir).as_posix()
            if backlinks.get(page, 0) == 0:
                warnings.append(f"[{cat}/{rel}] 고아 페이지 (백링크 0)")
            upd = page_updated.get(page)
            if upd and (today - upd).days > STALE_DAYS:
                stale_candidates.append((backlinks.get(page, 0), rel, upd))

        # stale은 백링크 많은 순(중요도순)으로 경고
        for _, rel, upd in sorted(stale_candidates, key=lambda x: -x[0]):
            warnings.append(f"[{cat}/{rel}] 오래된 페이지 (timestamp {upd}, "
                            f">{STALE_DAYS}일)")

    stats = {
        "categories": len(categories),
        "pages": total_pages,
        "errors": len(errors),
        "warnings": len(warnings),
    }
    return {"errors": errors, "warnings": warnings, "info": info, "stats": stats}


def _format_report(result: dict) -> str:
    lines = []
    s = result["stats"]
    lines.append(f"## Lint 결과 — 카테고리 {s.get('categories', 0)}개 / "
                 f"페이지 {s.get('pages', 0)}개")
    lines.append("")
    if not result["errors"] and not result["warnings"]:
        lines.append("OK: 문제 없음")
        return "\n".join(lines)
    if result["warnings"]:
        lines.append(f"[경고] {len(result['warnings'])}건 (수정 권장)")
        lines.extend(f"  - {w}" for w in result["warnings"])
        lines.append("")
    if result["errors"]:
        lines.append(f"[오류] {len(result['errors'])}건 (수정 필수)")
        lines.extend(f"  - {e}" for e in result["errors"])
    return "\n".join(lines)


def main():
    # Windows 콘솔 cp949에서 em-dash 등 깨짐 방지
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    category = sys.argv[1] if len(sys.argv) > 1 else None
    result = lint_wiki(category)
    print(_format_report(result))
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()

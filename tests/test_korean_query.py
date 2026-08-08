"""한국어 질의가 FTS5에서 조용히 0건이 되는 두 간극을 검증한다.

배경 — 2026-08-08 실측 (위키 33건 색인 상태). `tokenize='unicode61'`은 한글 어절을
통째로 한 토큰으로 본다. 형태소 분석도, 부분 문자열 매칭도 없다. 그래서:

  ① 조사·어미     비용 → 12건인데 **비용은 → 0건**, 대시보드 → 2건인데 대시보드를 → 0건
  ② 음차 기술명사  grafana → 3건인데 **그라파나 → 0건**, kubernetes → 10건인데 쿠버네티스 → 0건

①이 특히 고약하다. `비용`이 되는 걸 보고 "한국어 검색이 된다"고 판단하게 만드는데,
그건 그 문서에 정확히 `비용`이라는 어절이 있었던 우연이다. 조사 하나면 무너진다.

🔴 **토크나이저 교체는 답이 아니다** — 같은 코퍼스로 `trigram`을 시험했더니
   `비용`이 12건 → **0건**이 됐다(trigram은 3자 미만 질의를 못 쓴다). 그리고 음차는
   trigram으로도 0건이다 — **본문에 `그라파나`라는 문자열이 아예 없기 때문**이다.
   없는 문자열은 어떤 토크나이저로도 못 찾는다. 그래서 인덱스가 아니라 **질의를 고친다.**

설계 원칙 — **확장은 순증(純增)이어야 한다.** 원본 토큰을 항상 OR에 포함시켜서,
어간 추출이 틀려도 결과가 줄지 않게 한다. 잘못된 어간의 최대 피해는 무관한 결과 몇 개다.
"""
import tempfile
from pathlib import Path

import pytest


# ── 순수 함수 ────────────────────────────────────────────────────────────

def test_어간추출은_조사를_뗀다():
    from mneme import korean

    assert korean.stem("비용은") == "비용"
    assert korean.stem("대시보드를") == "대시보드"
    assert korean.stem("클러스터에서") == "클러스터"
    assert korean.stem("마이그레이션은") == "마이그레이션"


def test_어간추출은_짧은_토큰을_건드리지_않는다():
    """어간이 1글자로 남으면 떼지 않는다.

    `작은` → `작`, `가는` → `가` 같은 파괴를 막는다. 조사처럼 생긴 끝글자를 가진
    보통명사가 한국어에 흔하다(`노드`의 `드`는 조사가 아니지만, 2글자 토큰을
    전부 보호하면 이 판단 자체가 불필요해진다).
    """
    assert __import__("mneme.korean", fromlist=["korean"]).stem("작은") == "작은"
    from mneme import korean

    assert korean.stem("가는") == "가는"
    assert korean.stem("노드") == "노드"
    assert korean.stem("비용") == "비용"


def test_음차사전은_영문어를_붙인다():
    from mneme import korean

    assert "grafana" in korean.expand_token("그라파나")
    assert "kubernetes" in korean.expand_token("쿠버네티스")
    assert "secret" in korean.expand_token("시크릿")


def test_확장은_항상_원본을_포함한다():
    """순증 보장 — 확장이 결과를 줄이는 일이 없어야 한다."""
    from mneme import korean

    for tok in ["그라파나", "비용은", "노드", "grafana", "xyzzy"]:
        assert tok in korean.expand_token(tok), tok


def test_조사를_뗀_뒤에도_음차사전이_걸린다():
    """`시크릿을` → 어간 `시크릿` → 영문 `secret`. 두 처방이 이어져야 한다."""
    from mneme import korean

    expanded = korean.expand_token("시크릿을")
    assert "시크릿을" in expanded  # 원본
    assert "시크릿" in expanded  # 어간
    assert "secret" in expanded  # 음차


def test_FTS5_문법이_섞인_질의는_건드리지_않는다():
    """확장이 사용자의 FTS5 식을 망가뜨리면 안 된다.

    `"정확한 구"`, `a OR b`, `pod*`, `NEAR(...)`는 의도된 문법이다.
    이걸 어절 단위로 쪼개 괄호를 씌우면 의미가 바뀌거나 구문 오류가 난다.
    """
    from mneme import korean

    for q in ['"정확한 구"', "pod OR node", "kube*", "NEAR(a b)", "(a AND b)"]:
        assert korean.expand_query(q) == q, q


def test_영문_전용_질의는_원본_그대로다():
    """확장할 게 없으면 괄호를 씌우지 않는다 — 불필요한 재작성은 순위를 흔든다."""
    from mneme import korean

    assert korean.expand_query("grafana") == "grafana"


# ── 통합: 실제 FTS 인덱스 ────────────────────────────────────────────────

@pytest.fixture()
def indexed(monkeypatch):
    """임시 DB에 한국어 요약 + 영문 본문을 가진 문서를 넣는다.

    실제 코퍼스의 모양을 그대로 흉내낸다 — 본문은 영문 기술명사, 요약은 한국어인데
    기술명사만은 영문으로 남는 상태.
    """
    tmp = tempfile.mkdtemp(prefix="mneme-korean-")
    wiki = Path(tmp) / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_DIR", str(wiki))
    monkeypatch.setenv("DB_PATH", str(Path(tmp) / "state.db"))

    from mneme import memory, index

    memory.init_db()
    conn = index.get_connection()
    conn.execute(
        "INSERT INTO wiki_fts(path, content) VALUES (?, ?)",
        (
            "ops/monitoring.md",
            # 본문은 영문 기술명사, 요약은 한국어인데 기술명사만 영문으로 남는다.
            # `dashboard`가 영문으로만 있는 것이 의도적이다 — 한국어 복합 질의
            # `그라파나 대시보드`가 **두 낱말 모두 음차 변환을 거쳐야** 걸린다.
            "Grafana dashboard and Prometheus setup.\n\n"
            "클러스터 모니터링 구성과 비용 최적화를 다룬다. Secret은 SealedSecret으로 관리한다.",
        ),
    )
    conn.commit()
    conn.close()
    return index


def test_음차_질의가_영문_본문을_찾는다(indexed):
    """🔴 이 테스트가 이번 변경의 본체다. 지금은 0건이다."""
    assert len(indexed.search_fts("그라파나", limit=10)) == 1
    assert len(indexed.search_fts("프로메테우스", limit=10)) == 1


def test_조사가_붙어도_찾는다(indexed):
    assert len(indexed.search_fts("비용은", limit=10)) == 1
    assert len(indexed.search_fts("클러스터에서", limit=10)) == 1


def test_확장이_기존_검색을_깨지_않는다(indexed):
    """회귀 가드 — 영문·한국어 정확 질의는 전과 같이 동작해야 한다."""
    assert len(indexed.search_fts("grafana", limit=10)) == 1
    assert len(indexed.search_fts("모니터링", limit=10)) == 1
    assert len(indexed.search_fts("존재하지않는단어", limit=10)) == 0


def test_복합_질의도_확장된다(indexed):
    """🔴 회귀 재발 방지 — 첫 구현이 여기서 조용히 0건을 냈다.

    확장식을 `(a OR b) (c OR d)`로 만들었는데 **FTS5는 괄호 그룹 사이의 암묵적 AND를
    지원하지 않는다**: `fts5: syntax error near "("`. 낱말 단위로는 전부 동작해서
    단어 테스트만으로는 안 잡혔다.

    더 나쁜 건 **구문 오류가 조용히 0건이 됐다는 것**이다 — 폴백이 원본 질의를
    구(phrase)로 재시도하니 예외도 로그도 없이 빈 결과가 나갔다.
    안전망이 버그를 가린 셈이라, 이 테스트는 안전망 **바깥**에서 형태를 직접 확인한다.
    """
    from mneme import korean

    assert " AND " in korean.expand_query("그라파나 대시보드")
    assert len(indexed.search_fts("그라파나 대시보드", limit=10)) == 1
    assert len(indexed.search_fts("클러스터 비용은", limit=10)) == 1


def test_확장식이_FTS5에서_구문오류를_내지_않는다(indexed):
    """폴백에 기대지 않고 확장식 자체의 유효성을 본다.

    폴백이 있으면 잘못된 식도 '0건'으로 보이므로, 결과 개수만 보는 테스트는
    구문 오류를 통과시킨다. 여기서는 MATCH를 직접 태워 예외 여부를 확인한다.
    """
    import sqlite3

    from mneme import korean

    conn = indexed.get_connection()
    try:
        for q in ["그라파나", "그라파나 대시보드", "쿠버네티스 시크릿 비용은", "비용"]:
            expanded = korean.expand_query(q)
            try:
                conn.execute(
                    "SELECT path FROM wiki_fts WHERE wiki_fts MATCH ? LIMIT 1", (expanded,)
                ).fetchall()
            except sqlite3.OperationalError as e:  # pragma: no cover - 실패 시 진단용
                pytest.fail(f"확장식이 FTS5 구문 오류: {q!r} → {expanded!r} ({e})")
    finally:
        conn.close()


def test_깨진_FTS5_구문은_여전히_예외를_안_낸다(indexed):
    """기존 폴백(구문 오류 → 구 escape 재시도)이 확장 뒤에도 살아있어야 한다."""
    assert indexed.search_fts('unterminated "', limit=10) == []
    assert indexed.search_fts("NEAR(", limit=10) == []

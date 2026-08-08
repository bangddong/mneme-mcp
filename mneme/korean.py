"""한국어 질의를 FTS5가 찾을 수 있는 형태로 확장한다.

## 왜 필요한가 (2026-08-08 실측, 위키 33건)

`wiki_fts`는 `tokenize='unicode61'`이다. 한글 어절을 통째로 한 토큰으로 보고,
형태소 분석도 부분 문자열 매칭도 하지 않는다. 그 결과 두 곳에서 조용히 0건이 난다:

| 간극 | 실측 |
|---|---|
| ① 조사·어미 | `비용` 12건 · **`비용은` 0건** / `대시보드` 2건 · **`대시보드를` 0건** |
| ② 음차 기술명사 | `grafana` 3건 · **`그라파나` 0건** / `kubernetes` 10건 · **`쿠버네티스` 0건** |

①이 더 위험하다. `비용`이 되는 걸 보고 "한국어 검색이 된다"고 판단하게 만드는데,
그건 그 문서에 정확히 `비용`이라는 어절이 있었던 **우연**이다.

## 왜 토크나이저를 안 바꿨나

같은 코퍼스로 `tokenize='trigram'`을 시험했다:

    비용        unicode61 12건 → trigram **0건**   (trigram은 3자 미만 질의를 못 쓴다)
    그라파나     unicode61  0건 → trigram   0건

`비용`·`배포`·`인증` 같은 2글자 기술어가 한국어에 아주 흔해서 trigram은 손해가 크다.
그리고 **음차는 trigram으로도 0건**이다 — 본문에 `그라파나`라는 문자열이 아예 없기 때문.
없는 문자열은 어떤 토크나이저로도 못 찾는다.

→ 그래서 **인덱스가 아니라 질의를 고친다.** 재색인이 필요 없고(40~50분·LLM 필요),
  되돌리기도 쉽다.

## 설계 원칙 — 확장은 순증(純增)이어야 한다

`expand_token`은 **항상 원본 토큰을 포함**한다. 어간 추출이 틀려도 결과가 줄지 않는다.
잘못된 어간의 최대 피해는 무관한 결과 몇 개이고, 이건 순위(bm25)가 대부분 흡수한다.
반대로 원본을 빼면 **되던 검색이 안 되게** 만들 수 있다 — 그쪽이 훨씬 비싸다.
"""
from __future__ import annotations

import re

# 조사·어미. 긴 것부터 매칭해야 `으로서`가 `서`로 잘리지 않는다.
# 완전한 목록이 아니라 **검색 질의에 실제로 자주 붙는 것들**이다. 형태소 분석기를
# 넣지 않는 이유: 사전·모델 의존이 생기고(설치 부담), 순증 설계라 정확도가 덜 중요하다.
JOSA = (
    "으로써", "으로서", "이라는", "에서의", "에게서", "이라고", "라고",
    "라는", "으로", "에서", "에게", "까지", "부터", "보다", "처럼", "마다",
    "이나", "이란", "한테", "와의", "과의", "에는", "에도", "만을", "만이",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "와", "과", "로", "랑",
)
_JOSA_SORTED = tuple(sorted(JOSA, key=len, reverse=True))

# 음차 → 영문. 코퍼스에 **영문어가 실제로 등장하는 항목만** 넣는다.
# (안 쓰이는 항목은 사전을 키우기만 하고 아무것도 구제하지 못한다.)
# 2026-08-08 측정: 아래 중 20개가 "음차로는 0건인데 영문으로는 히트"였다.
TRANSLIT = {
    # 쿠버네티스 생태계
    "쿠버네티스": "kubernetes", "쿠버": "kubernetes", "케이８에스": "k8s",
    "파드": "pod", "노드": "node", "클러스터": "cluster", "네임스페이스": "namespace",
    "디플로이먼트": "deployment", "인그레스": "ingress", "서비스": "service",
    "시크릿": "secret", "컨피그맵": "configmap", "볼륨": "volume",
    "스테이트풀셋": "statefulset", "헬름": "helm", "아르고": "argocd",
    # 관측
    "그라파나": "grafana", "프로메테우스": "prometheus", "로키": "loki",
    "대시보드": "dashboard", "메트릭": "metric", "트레이싱": "tracing",
    # 인프라·도구
    "도커": "docker", "테라폼": "terraform", "젠킨스": "jenkins",
    "엔진엑스": "nginx", "레디스": "redis", "포스트그레스": "postgres",
    "카프카": "kafka", "래빗엠큐": "rabbitmq",
    # 개발
    "깃": "git", "커밋": "commit", "브랜치": "branch", "머지": "merge",
    "레포": "repo", "리포지토리": "repository", "풀리퀘스트": "pull",
    "스프링": "spring", "리액트": "react", "코틀린": "kotlin", "파이썬": "python",
    # 일반
    "인덱스": "index", "쿼리": "query", "캐시": "cache", "토큰": "token",
    "엔드포인트": "endpoint", "마이그레이션": "migration", "롤백": "rollback",
}

_HANGUL = re.compile(r"[가-힣]")

# FTS5 고유 문법. 하나라도 있으면 **사용자가 의도한 식**으로 보고 손대지 않는다.
# 어절로 쪼개 괄호를 씌우면 의미가 바뀌거나 구문 오류가 난다.
_FTS5_SYNTAX = re.compile(r'["*():^]|\b(?:OR|AND|NOT|NEAR)\b')


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text))


def stem(token: str) -> str:
    """조사·어미를 한 겹 뗀다. 뗄 수 없으면 원본 그대로.

    🔴 어간이 2글자 미만으로 남으면 떼지 않는다. `작은`→`작`, `가는`→`가` 같은
       파괴를 막는다. 조사처럼 생긴 끝글자를 가진 보통명사가 흔한데(`노드`의 `드`),
       2글자 토큰을 통째로 보호하면 그 판단 자체가 불필요해진다.
    """
    if not has_hangul(token):
        return token
    for j in _JOSA_SORTED:
        if token.endswith(j) and len(token) - len(j) >= 2:
            return token[: -len(j)]
    return token


def expand_token(token: str) -> list[str]:
    """토큰 하나를 검색 가능한 변형들로 넓힌다. **원본이 항상 첫 원소다.**"""
    out = [token]

    def add(t: str) -> None:
        if t and t not in out:
            out.append(t)

    root = stem(token)
    add(root)
    # 음차 사전은 원본과 어간 둘 다에 걸어본다 — `시크릿을` → `시크릿` → `secret`.
    for form in (token, root):
        en = TRANSLIT.get(form)
        if en:
            add(en)
    return out


def expand_query(query: str) -> str:
    """FTS5 MATCH 식으로 확장한다. 확장할 게 없으면 원본을 그대로 돌려준다.

    - FTS5 문법이 섞여 있으면 **손대지 않는다**(사용자 의도 존중).
    - 어절마다 `(원본 OR 어간 OR 영문)`을 만들고 **명시적 `AND`로 잇는다.**
    - 확장이 하나도 안 일어나면 재작성하지 않는다 — 불필요한 괄호는 순위를 흔든다.

    🔴 **`AND`를 생략하면 안 된다.** 낱말 사이의 암묵적 AND는 평범한 토큰에만 통하고
       **괄호 그룹 사이에는 안 통한다**:

           (그라파나 OR grafana) (대시보드 OR dashboard)   → fts5: syntax error near "("
           (그라파나 OR grafana) AND (대시보드 OR dashboard) → 2건

       첫 구현이 공백으로 이었다가 여기서 **조용히 0건**을 냈다. 낱말 질의는 전부
       동작해서 단어 테스트로는 안 잡혔고, `search_fts`의 폴백이 예외를 삼켜
       "결과 없음"으로 보이게 만들었다 — **안전망이 버그를 가렸다.**
    """
    if not query or _FTS5_SYNTAX.search(query):
        return query

    tokens = query.split()
    if not tokens:
        return query

    parts: list[str] = []
    changed = False
    for tok in tokens:
        variants = expand_token(tok)
        if len(variants) == 1:
            parts.append(tok)
        else:
            changed = True
            parts.append("(" + " OR ".join(variants) + ")")

    return " AND ".join(parts) if changed else query

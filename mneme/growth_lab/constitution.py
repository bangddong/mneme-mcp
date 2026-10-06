"""Constitution (L4 Value Memory) 로더.

MNEME 3층 헌법 + 테스트 시나리오 집합 K를 YAML에서 읽어온다.
"""
from pathlib import Path
import yaml

_CONSTITUTION_PATH = Path(__file__).parent / "constitution.yaml"
_cache: dict | None = None


def load_constitution() -> dict:
    """헌법 전체(절대/원칙/전략층 + 시나리오)를 반환한다. 1회 로드 후 캐시."""
    global _cache
    if _cache is None:
        with open(_CONSTITUTION_PATH, encoding="utf-8") as f:
            _cache = yaml.safe_load(f)
    return _cache


def get_coherence_threshold() -> float:
    """절대층의 최소 정렬 점수 (기본 0.95)."""
    return load_constitution().get("absolute", {}).get("coherence_threshold", 0.95)


def get_test_scenarios() -> list[dict]:
    """CIB 검증용 테스트 시나리오 집합 K."""
    return load_constitution().get("test_scenarios", [])


def get_absolute_values() -> list:
    """절대층 가치 목록 (헌법 채점 프롬프트에 주입)."""
    return load_constitution().get("absolute", {}).get("values", [])

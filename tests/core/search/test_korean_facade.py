"""Compatibility coverage for the Korean search normalization module."""


def test_legacy_and_core_korean_apis_match():
    from mneme import korean as legacy
    from mneme.core.search import korean as core

    for query in ("\uadf8\ub77c\ud30c\ub098 \ub300\uc2dc\ubcf4\ub4dc", 'Grafana "unclosed'):
        assert legacy.expand_query(query) == core.expand_query(query)

    assert legacy is core

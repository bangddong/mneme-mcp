def test_llm_facade_is_legacy_module_so_call_monkeypatch_reaches_helpers(monkeypatch):
    import mneme.llm as facade
    import mneme.legacy.llm as legacy

    monkeypatch.setattr(facade, "_call", lambda *args, **kwargs: "summary")

    assert facade is legacy
    assert legacy.generate_summary("note.md", "body") == "summary"


def test_llm_facade_preserves_current_function_inventory():
    import mneme.llm as llm

    names = {
        "_base_url",
        "_model",
        "is_available",
        "_call",
        "select_candidate_paths",
        "judge_conflict",
        "score_coherence",
        "reflect_episode",
        "assess_episode",
        "generate_summary",
    }

    assert {name for name in names if callable(getattr(llm, name, None))} == names

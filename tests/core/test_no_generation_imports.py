def test_core_source_does_not_import_generation_provider():
    from pathlib import Path

    offenders = []
    for path in Path("mneme/core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "mneme.llm" in text or "mneme.providers" in text or "httpx" in text:
            offenders.append(str(path))
    assert offenders == []

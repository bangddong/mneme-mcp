def test_wiki_facade_aliases_legacy_module_and_preserves_writes(tmp_path, monkeypatch):
    from mneme import wiki
    from mneme.legacy import wiki as legacy_wiki

    monkeypatch.setattr(wiki, "get_wiki_dir", lambda: tmp_path)

    wiki.write_file("docs/a.md", "# A")

    assert wiki is legacy_wiki
    assert legacy_wiki.read_file("docs/a.md")["content"] == "# A"


def test_lint_facade_aliases_core_module_and_preserves_private_monkeypatching(monkeypatch):
    from mneme import lint
    from mneme.core.validation import wiki as core_wiki

    monkeypatch.setattr(
        lint,
        "_parse_frontmatter",
        lambda _text: {"type": "concept", "title": "T", "timestamp": "2026-01-01", "tags": "x"},
    )

    assert lint is core_wiki
    assert lint.lint_page_text("## Summary\n## Details\n## Sources\n## Related") == []

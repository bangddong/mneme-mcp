from pathlib import Path


def test_filesystem_source_lists_markdown_deterministically_and_reads_utf8(tmp_path):
    from mneme.core.sources.filesystem import FileSystemSource

    (tmp_path / "docs").mkdir()
    (tmp_path / "z.md").write_text("# Z", encoding="utf-8")
    (tmp_path / "docs" / "a.md").write_text("# 안녕", encoding="utf-8")
    (tmp_path / "docs" / "note.txt").write_text("ignored", encoding="utf-8")

    source = FileSystemSource(tmp_path)

    assert source.list_markdown() == ["docs/a.md", "z.md"]
    assert source.read("docs/a.md") == "# 안녕"


def test_filesystem_source_is_read_only_and_fails_closed_for_unsafe_paths(tmp_path):
    from mneme.core.sources.filesystem import FileSystemSource

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A", encoding="utf-8")
    outside = tmp_path.parent / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "docs" / "escape.md").symlink_to(outside)
    source = FileSystemSource(tmp_path)

    assert source.read("../secret.md") is None
    assert source.read("docs\\..\\a.md") is None
    assert source.read("docs\\a.md") is None
    assert source.read(str((tmp_path / "docs" / "a.md").resolve())) is None
    assert source.read("docs/escape.md") is None
    assert {"write", "write_file", "delete", "delete_file", "save"}.isdisjoint(dir(source))

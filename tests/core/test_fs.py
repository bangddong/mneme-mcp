from pathlib import Path

import pytest


def _directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_write_new_uses_utf8_normalized_newlines_and_refuses_overwrite(tmp_path):
    from mneme.core.errors import ArtifactExists
    from mneme.core.fs import write_new

    path = tmp_path / "artifact.md"
    write_new(path, "첫 줄\r\nsecond")

    assert path.read_bytes() == "첫 줄\nsecond\n".encode("utf-8")
    with pytest.raises(ArtifactExists):
        write_new(path, "replacement")
    assert path.read_text(encoding="utf-8") == "첫 줄\nsecond\n"


def test_yaml_cas_is_deterministic_and_stale_write_preserves_current_value(tmp_path):
    from mneme.core.errors import ConcurrentWrite
    from mneme.core.fs import dump_yaml, read_yaml, write_new, write_yaml_cas

    path = tmp_path / "registry.yaml"
    write_new(path, dump_yaml({"zeta": "한글", "generation": 0, "items": {}}))
    assert path.read_text(encoding="utf-8") == (
        "generation: 0\nitems: {}\nzeta: 한글\n"
    )

    write_yaml_cas(
        path,
        {"zeta": "한글", "generation": 1, "items": {"b": 2, "a": 1}},
        expected_generation=0,
    )
    current = path.read_text(encoding="utf-8")
    assert current == "generation: 1\nitems:\n  a: 1\n  b: 2\nzeta: 한글\n"
    assert read_yaml(path)["generation"] == 1

    with pytest.raises(ConcurrentWrite):
        write_yaml_cas(path, {"generation": 1, "items": {}}, expected_generation=0)
    assert path.read_text(encoding="utf-8") == current


def test_yaml_cas_requires_exact_next_generation(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.fs import dump_yaml, write_new, write_yaml_cas

    path = tmp_path / "registry.yaml"
    write_new(path, dump_yaml({"generation": 4}))

    with pytest.raises(InvalidArtifact):
        write_yaml_cas(path, {"generation": 6}, expected_generation=4)
    assert path.read_text(encoding="utf-8") == "generation: 4\n"


def test_frontmatter_round_trips_utf8_deterministically(tmp_path):
    from mneme.core.fs import dump_frontmatter, read_frontmatter, write_new

    path = tmp_path / "session.md"
    encoded = dump_frontmatter(
        {"workstream": "작업-1", "revision": "000001"},
        "## 현재\r\n진행 중",
    )
    write_new(path, encoded)

    metadata, body = read_frontmatter(path)
    assert metadata == {"revision": "000001", "workstream": "작업-1"}
    assert body == "## 현재\n진행 중\n"
    assert path.read_text(encoding="utf-8") == (
        "---\nrevision: '000001'\nworkstream: 작업-1\n---\n"
        "## 현재\n진행 중\n"
    )


def test_frontmatter_rejects_missing_or_non_mapping_metadata(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.fs import read_frontmatter

    missing = tmp_path / "missing.md"
    missing.write_text("body only\n", encoding="utf-8")
    with pytest.raises(InvalidArtifact):
        read_frontmatter(missing)

    sequence = tmp_path / "sequence.md"
    sequence.write_text("---\n- not\n- a mapping\n---\nbody\n", encoding="utf-8")
    with pytest.raises(InvalidArtifact):
        read_frontmatter(sequence)


def test_path_chain_validation_allows_a_clean_missing_tail_without_creating_it(
    tmp_path,
):
    """Catches initialization validation requiring every future directory to exist."""
    from mneme.core.fs import validate_path_chain

    candidate = tmp_path / "not-yet-created" / "vault"

    assert validate_path_chain(candidate, allow_missing=True) == candidate
    assert not candidate.exists()


def test_path_chain_validation_rejects_a_directory_symlink_ancestor(tmp_path):
    """Catches leaf-only lstat checks following an earlier symbolic link."""
    from mneme.core.errors import UnsafePath
    from mneme.core.fs import validate_path_chain

    target = tmp_path / "target"
    target.mkdir()
    (target / "child").mkdir()
    alias = tmp_path / "alias"
    _directory_symlink(alias, target)
    try:
        with pytest.raises(UnsafePath):
            validate_path_chain(alias / "child", allow_missing=False)
    finally:
        alias.unlink()


def test_file_lock_refuses_a_symlinked_ancestor_without_writing_through_it(
    tmp_path,
):
    """Catches lock creation following a link into another directory tree."""
    from mneme.core.errors import UnsafePath
    from mneme.core.fs import exclusive_file_lock

    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    _directory_symlink(alias, target)
    try:
        with pytest.raises(UnsafePath):
            with exclusive_file_lock(alias / "operation.lock"):
                pass
    finally:
        alias.unlink()

    assert not (target / "operation.lock").exists()

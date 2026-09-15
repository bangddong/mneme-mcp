"""Opt-in Madi Claude installer behavior in disposable projects."""

from __future__ import annotations

import json

import pytest


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_claude_preserves_existing_config_and_rules_and_is_idempotent(tmp_path):
    """Catches the opt-in installer clobbering a project or adding Madi twice."""
    from integration.madi.install_claude import install

    project = tmp_path / "project"
    project.mkdir()
    existing_server = {
        "type": "stdio",
        "command": "existing-tool",
        "args": ["--keep", "all"],
    }
    mcp_path = project / ".mcp.json"
    mcp_path.write_text(
        json.dumps({"custom": {"keep": True}, "mcpServers": {"other": existing_server}}),
        encoding="utf-8",
    )
    original_rules = "# Existing Claude guidance\nKeep this byte sequence."
    rules_path = project / "CLAUDE.md"
    rules_path.write_text(original_rules, encoding="utf-8")

    first = install(project)
    first_mcp = mcp_path.read_text(encoding="utf-8")
    first_rules = rules_path.read_text(encoding="utf-8")
    second = install(project)

    installed = _read_json(mcp_path)
    assert first.changed is True
    assert second.changed is False
    assert installed["custom"] == {"keep": True}
    assert installed["mcpServers"]["other"] == existing_server
    assert installed["mcpServers"]["madi"] == {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "mneme.transports.mcp_stdio"],
    }
    assert first_rules.startswith(original_rules)
    assert first_rules.count("<!-- MADI-CLAUDE-ADAPTER: BEGIN -->") == 1
    assert first_rules.count("<!-- MADI-CLAUDE-ADAPTER: END -->") == 1
    assert mcp_path.read_text(encoding="utf-8") == first_mcp
    assert rules_path.read_text(encoding="utf-8") == first_rules
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in project.rglob("*") if path.is_file())
    assert str(tmp_path / "private-vault") not in rendered
    assert "http://localhost" not in rendered


def test_install_claude_preserves_existing_rule_bytes_when_appending_madi_block(tmp_path):
    """Catches text newline translation rewriting pre-existing Claude guidance."""
    from integration.madi.install_claude import install

    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text("{}\n", encoding="utf-8")
    rules_path = project / "CLAUDE.md"
    original_rules = b"# Existing Claude guidance\r\nKeep these exact bytes.\r\n"
    rules_path.write_bytes(original_rules)

    install(project)

    installed_rules = rules_path.read_bytes()
    assert installed_rules.startswith(original_rules)
    assert installed_rules.count(b"<!-- MADI-CLAUDE-ADAPTER: BEGIN -->") == 1


def test_install_claude_refuses_unrecognized_complete_madi_marker_block(tmp_path):
    """Catches marker text alone being mistaken for an already-installed Madi block."""
    from integration.madi.install_claude import InstallError, install

    project = tmp_path / "project"
    project.mkdir()
    mcp_path = project / ".mcp.json"
    original_mcp = '{"mcpServers":{"other":{"type":"stdio"}}}'
    mcp_path.write_text(original_mcp, encoding="utf-8")
    rules_path = project / "CLAUDE.md"
    original_rules = (
        "# Existing guidance\n"
        "<!-- MADI-CLAUDE-ADAPTER: BEGIN -->\n"
        "User-authored text, not the Madi integration.\n"
        "<!-- MADI-CLAUDE-ADAPTER: END -->\n"
    )
    rules_path.write_text(original_rules, encoding="utf-8")

    with pytest.raises(InstallError):
        install(project)

    assert mcp_path.read_text(encoding="utf-8") == original_mcp
    assert rules_path.read_text(encoding="utf-8") == original_rules


@pytest.mark.parametrize(
    "mcp_text",
    [
        '{"mcpServers":{"other":{},"other":{}}}',
        '{"mcpServers": []}',
        '{"mcpServers": null}',
        '{"mcpServers": {"madi": null}}',
        '{"mcpServers": {"madi": {"type": "http", "url": "https://elsewhere"}}}',
    ],
)
def test_install_claude_rejects_unsafe_config_without_touching_project(tmp_path, mcp_text):
    """Catches malformed/ambiguous JSON or a conflicting Madi server being overwritten."""
    from integration.madi.install_claude import InstallError, install

    project = tmp_path / "project"
    project.mkdir()
    mcp_path = project / ".mcp.json"
    mcp_path.write_text(mcp_text, encoding="utf-8")
    rules_path = project / "CLAUDE.md"
    original_rules = "# Keep existing guidance exactly"
    rules_path.write_text(original_rules, encoding="utf-8")

    with pytest.raises(InstallError):
        install(project)

    assert mcp_path.read_text(encoding="utf-8") == mcp_text
    assert rules_path.read_text(encoding="utf-8") == original_rules


@pytest.mark.parametrize(
    "rules",
    [
        "# Existing\n<!-- MADI-CLAUDE-ADAPTER: BEGIN -->",
        "# Existing\n<!-- MADI-CLAUDE-ADAPTER: END -->",
        "<!-- MADI-CLAUDE-ADAPTER: END -->\n<!-- MADI-CLAUDE-ADAPTER: BEGIN -->",
    ],
)
def test_install_claude_refuses_incomplete_or_reordered_rule_markers(tmp_path, rules):
    """Catches a partial prior install causing a duplicated or reordered rule block."""
    from integration.madi.install_claude import InstallError, install

    project = tmp_path / "project"
    project.mkdir()
    mcp_path = project / ".mcp.json"
    original_mcp = '{"mcpServers":{"other":{"type":"stdio"}}}'
    mcp_path.write_text(original_mcp, encoding="utf-8")
    rules_path = project / "CLAUDE.md"
    rules_path.write_text(rules, encoding="utf-8")

    with pytest.raises(InstallError):
        install(project)

    assert mcp_path.read_text(encoding="utf-8") == original_mcp
    assert rules_path.read_text(encoding="utf-8") == rules

"""Opt-in Codex hook runner and installer behavior in disposable projects."""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path

import pytest


_REPOSITORY = Path(__file__).parents[2]
_FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"
_HOOK_COMMAND = "python -m integration.madi.codex.pre_compact_hook"
_DESIRED_REGISTRATION = {
    "matcher": "manual|auto",
    "hooks": [{"type": "command", "command": _HOOK_COMMAND}],
}


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_hook_runner_reads_native_stdin_and_invokes_the_translator_once():
    """Catches the runner bypassing translation or echoing private native fields."""
    from integration.madi.codex.pre_compact_hook import run_hook
    from mneme.adapters.codex import translate_pre_compact

    native = _fixture("pre_compact.native.json")
    expected = _fixture("pre_compact.normalized.json")
    calls: list[tuple[object, object]] = []

    def recording_translator(payload, binding):
        calls.append((payload, binding))
        return translate_pre_compact(payload, binding)

    output = StringIO()
    exit_code = run_hook(
        StringIO(json.dumps(native)),
        output,
        local_binding={"workstream_id": "ws-1"},
        translator=recording_translator,
    )

    diagnostic = json.loads(output.getvalue())
    encoded = json.dumps(diagnostic)
    assert exit_code == 0
    assert calls == [(native, {"workstream_id": "ws-1"})]
    assert diagnostic == {
        "block_host": False,
        "checkpoint_required": True,
        "context_required": False,
        "event": "pre_compact",
        "lifecycle": expected["event"],
        "native_metadata": expected["native_metadata"],
        "ok": True,
        "version": 1,
        "warning": None,
    }
    for field in ("transcript_path", "cwd", "model", "agent_id", "agent_type"):
        assert str(native[field]) not in encoded


@pytest.mark.parametrize(
    ("raw_input", "binding"),
    [
        ("not-json PRIVATE_INPUT_SENTINEL", {"workstream_id": "ws-1"}),
        (json.dumps({"hook_event_name": "PreCompact"}), {"workstream_id": "ws-1"}),
        (json.dumps(_fixture("pre_compact.native.json")), {}),
    ],
    ids=("invalid-json", "invalid-payload", "missing-binding"),
)
def test_hook_runner_returns_closed_warning_and_success_exit_for_invalid_input(
    raw_input, binding
):
    """Catches malformed optional hooks blocking Codex or disclosing their input."""
    from integration.madi.codex.pre_compact_hook import run_hook

    output = StringIO()
    exit_code = run_hook(StringIO(raw_input), output, local_binding=binding)

    diagnostic = json.loads(output.getvalue())
    encoded = json.dumps(diagnostic)
    assert exit_code == 0
    assert diagnostic["ok"] is False
    assert diagnostic["block_host"] is False
    assert diagnostic["event"] == "unknown"
    assert diagnostic["warning"] == (
        "Madi could not process this Codex PreCompact hook; continue ordinary Codex work."
    )
    assert "PRIVATE_INPUT_SENTINEL" not in encoded
    assert "private-codex" not in encoded


def test_hook_runner_closes_translator_failures_without_blocking_or_echoing():
    """Catches an unavailable integration exception escaping through the native hook."""
    from integration.madi.codex.pre_compact_hook import run_hook

    def unavailable_translator(payload, binding):
        raise OSError("C:/private/madi-token.txt is unavailable")

    output = StringIO()
    exit_code = run_hook(
        StringIO(json.dumps(_fixture("pre_compact.native.json"))),
        output,
        local_binding={"workstream_id": "ws-1"},
        translator=unavailable_translator,
    )

    encoded = output.getvalue()
    assert exit_code == 0
    assert json.loads(encoded)["block_host"] is False
    assert "madi-token" not in encoded
    assert "C:/private" not in encoded


def test_bundled_hook_registers_only_precompact_for_manual_or_auto():
    """Catches installation of an unrelated event, trigger, or command surface."""
    bundled = _read_json(_REPOSITORY / "integration" / "madi" / "codex" / "hooks.json")

    assert bundled == {"hooks": {"PreCompact": [_DESIRED_REGISTRATION]}}


def test_install_codex_preserves_existing_config_hooks_and_agents_and_is_idempotent(
    tmp_path,
):
    """Catches the opt-in installer clobbering project data or adding Madi twice."""
    from integration.madi.install_codex import install

    project = tmp_path / "project"
    codex_dir = project / ".codex"
    codex_dir.mkdir(parents=True)
    existing_hook = {
        "matcher": "manual",
        "hooks": [{"type": "command", "command": "existing-tool --keep"}],
    }
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "custom": {"keep": True},
                "hooks": {
                    "SessionStart": [existing_hook],
                    "PreCompact": [existing_hook],
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = codex_dir / "config.toml"
    original_config = b'model = "keep-this"\r\ncustom = true\r\n'
    config_path.write_bytes(original_config)
    agents_path = project / "AGENTS.md"
    original_agents = b"# Existing Codex guidance\r\nKeep these exact bytes.\r\n"
    agents_path.write_bytes(original_agents)

    first = install(project)
    first_hooks = hooks_path.read_bytes()
    first_agents = agents_path.read_bytes()
    second = install(project)

    installed = _read_json(hooks_path)
    assert first.changed is True
    assert second.changed is False
    assert installed["custom"] == {"keep": True}
    assert installed["hooks"]["SessionStart"] == [existing_hook]
    assert installed["hooks"]["PreCompact"][0] == existing_hook
    assert installed["hooks"]["PreCompact"].count(_DESIRED_REGISTRATION) == 1
    assert agents_path.read_bytes().startswith(original_agents)
    assert agents_path.read_bytes().count(b"<!-- MADI-CODEX-ADAPTER: BEGIN -->") == 1
    assert config_path.read_bytes() == original_config
    assert hooks_path.read_bytes() == first_hooks
    assert agents_path.read_bytes() == first_agents
    rendered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in project.rglob("*")
        if path.is_file()
    )
    assert str(tmp_path / "private-vault") not in rendered
    assert "API_KEY=" not in rendered
    assert "Bearer " not in rendered


@pytest.mark.parametrize(
    "hooks_text",
    [
        '{"hooks": {}, "hooks": {}}',
        '{"hooks": []}',
        '{"hooks": {"PreCompact": {}}}',
        json.dumps(
            {
                "hooks": {
                    "PreCompact": [
                        {
                            "matcher": "manual",
                            "hooks": [
                                {"type": "command", "command": _HOOK_COMMAND}
                            ],
                        }
                    ]
                }
            }
        ),
    ],
    ids=("duplicate-root", "hooks-not-object", "event-not-list", "command-conflict"),
)
def test_install_codex_rejects_unsafe_hooks_without_touching_project(
    tmp_path, hooks_text
):
    """Catches ambiguous or conflicting hooks causing a partial installation."""
    from integration.madi.install_codex import InstallError, install

    project = tmp_path / "project"
    codex_dir = project / ".codex"
    codex_dir.mkdir(parents=True)
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(hooks_text, encoding="utf-8")
    agents_path = project / "AGENTS.md"
    original_agents = "# Keep existing guidance exactly"
    agents_path.write_text(original_agents, encoding="utf-8")

    with pytest.raises(InstallError):
        install(project)

    assert hooks_path.read_text(encoding="utf-8") == hooks_text
    assert agents_path.read_text(encoding="utf-8") == original_agents


@pytest.mark.parametrize(
    "agents",
    [
        "# Existing\n<!-- MADI-CODEX-ADAPTER: BEGIN -->",
        "# Existing\n<!-- MADI-CODEX-ADAPTER: END -->",
        (
            "<!-- MADI-CODEX-ADAPTER: BEGIN -->\n"
            "User-authored text, not this integration.\n"
            "<!-- MADI-CODEX-ADAPTER: END -->\n"
        ),
    ],
    ids=("begin-only", "end-only", "spoofed-complete-block"),
)
def test_install_codex_rejects_ambiguous_agents_markers_before_writing_hooks(
    tmp_path, agents
):
    """Catches incomplete or spoofed marker content producing duplicate guidance."""
    from integration.madi.install_codex import InstallError, install

    project = tmp_path / "project"
    codex_dir = project / ".codex"
    codex_dir.mkdir(parents=True)
    hooks_path = codex_dir / "hooks.json"
    original_hooks = '{"hooks":{"SessionStart":[]}}'
    hooks_path.write_text(original_hooks, encoding="utf-8")
    agents_path = project / "AGENTS.md"
    agents_path.write_text(agents, encoding="utf-8")

    with pytest.raises(InstallError):
        install(project)

    assert hooks_path.read_text(encoding="utf-8") == original_hooks
    assert agents_path.read_text(encoding="utf-8") == agents


def test_install_codex_refuses_a_linked_hooks_target_without_modifying_its_target(
    tmp_path,
):
    """Catches a project hook path redirecting installer writes outside the project."""
    from integration.madi.install_codex import InstallError, install

    project = tmp_path / "project"
    codex_dir = project / ".codex"
    codex_dir.mkdir(parents=True)
    outside = tmp_path / "outside-hooks.json"
    original = '{"outside":true}'
    outside.write_text(original, encoding="utf-8")
    linked = codex_dir / "hooks.json"
    try:
        os.symlink(outside, linked)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc.__class__.__name__}")

    with pytest.raises(InstallError):
        install(project)

    assert outside.read_text(encoding="utf-8") == original

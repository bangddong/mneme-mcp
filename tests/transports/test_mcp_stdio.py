"""On-demand stdio MCP transport regressions."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest


@pytest.fixture
def service(tmp_path):
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    RegistryStore(vault).create_workstream("ws-1", project=None, mode="parallel")
    return CoreService(vault)


def _checkpoint_payload() -> dict[str, object]:
    return {
        "workstream_id": "ws-1",
        "session_id": "session-1",
        "storage_class": "portable",
        "expected_parent": None,
        "expected_registry_generation": 0,
        "body": {
            "adapter_id": "codex",
            "objective": "Exercise stdio checkpoint",
            "current_state": "Direct handler call",
            "verified_facts": [],
            "completed_work": [],
            "blockers": [],
            "next_actions": ["Continue ordinary agent work."],
            "source_refs": [],
        },
        "relations": [],
    }


def _memory_payload(memory_id: str) -> dict[str, object]:
    return {
        "authority": "personal",
        "body": f"Remember {memory_id} through stdio.",
        "kind": "knowledge",
        "memory_id": memory_id,
        "portability": "personal-vault",
        "scope": {"type": "personal-global"},
    }


def _handler(app, name: str):
    tool = asyncio.run(app.get_tool(name))
    assert tool is not None
    return tool.fn


def _assert_closed_envelope(value: dict[str, object], command: str) -> None:
    assert set(value) == {
        "command",
        "domain_events",
        "error",
        "ok",
        "operational_events",
        "result",
        "status",
        "version",
    }
    assert value["command"] == command
    assert value["version"] == 1
    assert value["error"] is None
    json.dumps(value)


def test_stdio_registers_five_directly_callable_structured_tools(service):
    """Catches transport registration drifting from the five D3 MCP tools."""
    from mneme.transports.mcp_stdio import create_app

    app = create_app(service)
    handlers = {
        name: _handler(app, name)
        for name in (
            "madi_context",
            "madi_recall",
            "madi_remember",
            "madi_checkpoint",
            "madi_decision",
        )
    }

    context = handlers["madi_context"]("ws-1")
    recall = handlers["madi_recall"]("no-such-result", 5, None)
    remembered = handlers["madi_remember"](_memory_payload("memory-1"))
    decision_payload = _memory_payload("decision-1")
    decision_payload.pop("kind")
    decision = handlers["madi_decision"](decision_payload)
    checkpoint = handlers["madi_checkpoint"](_checkpoint_payload())

    _assert_closed_envelope(context, "get_context")
    _assert_closed_envelope(recall, "madi_recall")
    _assert_closed_envelope(remembered, "submit_memory")
    _assert_closed_envelope(decision, "submit_memory")
    _assert_closed_envelope(checkpoint, "create_session_revision")
    assert context["result"]["workstream_id"] == "ws-1"
    assert isinstance(recall["result"]["hits"], list)
    assert remembered["domain_events"][0]["name"] == "memory_submitted"
    assert decision["result"]["kind"] == "decision"
    assert checkpoint["domain_events"][0]["name"] == "session_revision_created"


def test_direct_handler_errors_are_closed_and_hide_raw_input(service, tmp_path):
    """Catches FastMCP surfacing a local path or raw validation exception."""
    from mneme.transports.mcp_stdio import create_app

    handler = _handler(create_app(service), "madi_checkpoint")
    secret = tmp_path / "confidential-checkpoint.json"
    result = handler({"path": str(secret)})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-artifact"
    assert str(secret) not in json.dumps(result)
    assert set(result["error"]) == {"code", "message"}


def test_madi_checkpoint_rejects_detectable_credentials_without_writing(service):
    """Catches stdio bypassing the Core secret guard for durable checkpoints."""
    from mneme.transports.mcp_stdio import create_app

    payload = _checkpoint_payload()
    payload["body"]["current_state"] = "API_KEY=SENSITIVE_SENTINEL"

    result = _handler(create_app(service), "madi_checkpoint")(payload)

    encoded = json.dumps(result)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-artifact"
    assert "SENSITIVE_SENTINEL" not in encoded
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


@pytest.mark.parametrize(
    ("storage_class", "workstream_id"),
    [("portable", "ws-1"), ("local-only", "ws-local")],
)
def test_madi_checkpoint_rejects_detectable_credential_reference_without_writing(
    service, storage_class, workstream_id
):
    """Catches stdio persisting a credential held only in a reference value."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.registries import RegistryStore
    from mneme.transports.mcp_stdio import create_app

    selected_class = StorageClass(storage_class)
    if selected_class is StorageClass.LOCAL_ONLY:
        RegistryStore(service.vault, selected_class).create_workstream(
            workstream_id, project=None, mode="parallel"
        )
    payload = _checkpoint_payload()
    payload["storage_class"] = storage_class
    payload["workstream_id"] = workstream_id
    payload["body"]["source_refs"] = [
        {
            "kind": "label",
            "storage_class": "portable",
            "value": "API_KEY=SENSITIVE_SENTINEL",
        }
    ]

    result = _handler(create_app(service), "madi_checkpoint")(payload)

    encoded = json.dumps(result)
    root = (
        service.vault.root
        if selected_class is StorageClass.PORTABLE
        else service.vault.local_root / "overlays"
    )
    registry = RegistryStore(service.vault, selected_class).load_workstream(workstream_id)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-artifact"
    assert "SENSITIVE_SENTINEL" not in encoded
    assert list(root.glob("workstreams/*/sessions/*/*.md")) == []
    assert registry.generation == 0
    assert registry.active_heads == ()


def test_importing_core_does_not_require_fastmcp():
    """Catches a transport dependency entering the import-safe Core boundary."""
    script = r'''
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "fastmcp" or name.startswith("fastmcp."):
        raise RuntimeError("fastmcp is blocked")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from mneme.core.contracts import CoreCommand, LifecycleEvent
from mneme.core.service import CoreService
assert CoreCommand and LifecycleEvent and CoreService
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

def test_stdio_main_runs_injected_service_without_startup_work(service, monkeypatch):
    """Catches stdio startup initializing legacy state, reindexing, or binding HTTP."""
    from mneme.transports import mcp_stdio

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class App:
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))

    def forbidden_reindex():
        raise AssertionError("stdio startup must not rebuild the index")

    monkeypatch.setattr(service, "reindex", forbidden_reindex)
    monkeypatch.setattr(mcp_stdio, "create_app", lambda injected: App())

    mcp_stdio.main(service)

    assert calls == [((), {"show_banner": False, "transport": "stdio"})]


def test_stdio_transport_imports_no_legacy_daemon_or_generation_modules():
    """Catches the new on-demand transport acquiring legacy startup side effects."""
    script = r'''
import builtins
blocked_modules = {
    "mneme.index",
    "mneme.llm",
    "mneme.memory",
    "mneme.scheduler",
    "mneme.transports.legacy_http",
    "mneme.watcher",
}
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name in blocked_modules:
        raise RuntimeError(f"forbidden import: {name}")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from mneme.transports.mcp_stdio import create_app, main
assert create_app and main
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

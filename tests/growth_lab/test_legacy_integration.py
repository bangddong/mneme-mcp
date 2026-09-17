"""Integration contracts for optional Growth runtime dependencies."""

from __future__ import annotations

import importlib
import inspect
import subprocess
import sys
import textwrap

import pytest


@pytest.fixture()
def legacy_growth_runtime(tmp_path, monkeypatch):
    """Use a fresh legacy SQLite database without provider or network state."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy-growth.db"))
    from mneme import memory

    memory.init_db()
    return memory


@pytest.mark.parametrize(
    ("module_name", "public_names"),
    [
        ("scheduler", ("start_scheduler", "stop_scheduler")),
        ("notify", ("is_enabled", "send", "notify_degrading")),
    ],
)
def test_legacy_module_is_growth_lab_integration_with_same_interface(
    module_name, public_names
):
    """A wrapping facade must not split globals or monkeypatch behavior."""
    legacy = importlib.import_module(f"mneme.{module_name}")
    implementation = importlib.import_module(f"mneme.growth_lab.{module_name}")

    assert legacy is implementation
    for name in public_names:
        assert getattr(legacy, name) is getattr(implementation, name)
        assert inspect.signature(getattr(legacy, name)) == inspect.signature(
            getattr(implementation, name)
        )


def test_scheduler_tick_runs_outer_loop_before_self_model(monkeypatch):
    """A reversed tick would assess stale state before the lifecycle cycle."""
    from mneme import scheduler as legacy
    from mneme.growth_lab import scheduler as implementation

    calls = []
    monkeypatch.setattr(
        legacy.outer_loop,
        "run_cycle",
        lambda *, force: calls.append(("outer_loop", force)) or {"ran": False},
    )
    monkeypatch.setattr(
        legacy.self_model,
        "assess",
        lambda *, force: calls.append(("self_model", force)) or {"ran": False},
    )

    implementation._tick()

    assert calls == [("outer_loop", False), ("self_model", False)]


def test_scheduler_start_stop_preserves_job_lifecycle(monkeypatch):
    """Duplicate starts or reordered shutdown would leak a background job."""
    from mneme.growth_lab import scheduler

    calls = []

    class FakeScheduler:
        def __init__(self, *, daemon):
            calls.append(("construct", daemon))

        def add_job(self, callback, trigger, **kwargs):
            calls.append(("add_job", callback, trigger, kwargs))

        def start(self):
            calls.append(("start",))

        def shutdown(self, *, wait):
            calls.append(("shutdown", wait))

    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)

    scheduler.start_scheduler()
    scheduler.start_scheduler()
    scheduler.stop_scheduler()
    scheduler.stop_scheduler()

    assert calls == [
        ("construct", True),
        (
            "add_job",
            scheduler._tick,
            "interval",
            {"minutes": scheduler.INTERVAL_MIN, "id": "outer_loop"},
        ),
        ("start",),
        ("shutdown", False),
    ]
    assert scheduler._scheduler is None


def test_notify_success_posts_bounded_discord_payload(monkeypatch):
    """A moved notifier must keep its external request and success contract."""
    from mneme import notify as legacy
    from mneme.growth_lab import notify as implementation

    calls = []

    class Response:
        status_code = 204

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://notify.invalid/webhook")
    monkeypatch.setattr(
        legacy.httpx,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )

    assert implementation.send("x" * 2001) is True
    assert calls == [
        (
            ("https://notify.invalid/webhook",),
            {"json": {"content": "x" * 2000}, "timeout": 5.0},
        )
    ]


def test_notify_network_failure_returns_false_without_raising(monkeypatch):
    """Optional network failure must remain confined to the notifier."""
    from mneme import notify as legacy
    from mneme.growth_lab import notify as implementation

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://notify.invalid/webhook")

    def unavailable(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(legacy.httpx, "post", unavailable)

    assert implementation.send("notice") is False
    assert implementation.notify_degrading(
        [{"name": "study", "to": "degrading", "reason": "low rate"}]
    ) is False


def test_legacy_http_uses_growth_lab_integrations():
    """The legacy server alone owns optional Growth integration imports."""
    from mneme.growth_lab import growth, log, outer_loop, scheduler, self_model, skills
    from mneme.transports import legacy_http

    assert legacy_http.skill_layer is skills
    assert legacy_http.outer_loop is outer_loop
    assert legacy_http.self_model_mod is self_model
    assert legacy_http.access_log is log
    assert legacy_http.growth is growth
    assert legacy_http.start_scheduler is scheduler.start_scheduler
    assert legacy_http.stop_scheduler is scheduler.stop_scheduler


def test_legacy_http_growth_responses_keep_exact_shapes(legacy_growth_runtime):
    """Moving integrations must not alter any exposed legacy Growth envelope."""
    from mneme.growth_lab import growth
    from mneme.transports import legacy_http

    assert legacy_http.outer_loop_run() == {
        "ran": False,
        "reason": "new episodes 0 < 20",
        "new_episodes": 0,
    }
    assert legacy_http.outer_loop_status() == {
        "recent_cycles": [],
        "skill_states": {},
    }
    assert legacy_http.self_model_status() == {
        "recent": [],
        "regulation": None,
        "difficulty": None,
        "alert": False,
    }
    assert legacy_http.curriculum_suggest() == {"difficulty": 0.5, "targets": []}

    action_id = growth.record(
        "regulation",
        "critical",
        "growth-rate",
        "rate dropped",
        "regulation:crash",
    )
    action = growth.open_actions()[0]
    assert set(action) == {
        "id",
        "created_at",
        "last_seen_at",
        "seen_count",
        "kind",
        "severity",
        "subject",
        "detail",
        "dedup_key",
        "status",
        "resolved_at",
        "resolution_note",
    }
    assert {
        key: action[key]
        for key in (
            "id",
            "seen_count",
            "kind",
            "severity",
            "subject",
            "detail",
            "dedup_key",
            "status",
        )
    } == {
        "id": action_id,
        "seen_count": 1,
        "kind": "regulation",
        "severity": "critical",
        "subject": "growth-rate",
        "detail": "rate dropped",
        "dedup_key": "regulation:crash",
        "status": "open",
    }
    assert legacy_http.growth_log() == {
        "count": 1,
        "status": "open",
        "actions": [action],
    }
    assert legacy_http.growth_resolve(action_id, "reviewed") == {
        "resolved": True,
        "id": action_id,
    }
    all_actions = legacy_http.growth_log("all")
    assert set(all_actions) == {"count", "status", "actions"}
    assert all_actions["count"] == 1
    assert all_actions["status"] == "all"
    assert all_actions["actions"][0]["status"] == "resolved"
    assert all_actions["actions"][0]["resolution_note"] == "reviewed"
    assert legacy_http.growth_log() == {
        "count": 0,
        "status": "open",
        "actions": [],
    }


def test_legacy_http_startup_and_cleanup_order(monkeypatch):
    """Watcher and scheduler ordering is part of the historical server contract."""
    from mneme.transports import legacy_http

    calls = []
    monkeypatch.setattr(legacy_http, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(
        legacy_http.idx, "reindex_all", lambda: calls.append("reindex")
    )
    monkeypatch.setattr(
        legacy_http, "start_watcher", lambda: calls.append("watcher-start")
    )
    monkeypatch.setattr(
        legacy_http, "start_scheduler", lambda: calls.append("scheduler-start")
    )
    monkeypatch.setattr(
        legacy_http, "stop_scheduler", lambda: calls.append("scheduler-stop")
    )
    monkeypatch.setattr(
        legacy_http, "stop_watcher", lambda: calls.append("watcher-stop")
    )
    monkeypatch.setattr(
        legacy_http.mcp,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )

    with pytest.raises(RuntimeError, match="stop"):
        legacy_http.main()

    assert calls == [
        "init",
        "reindex",
        "watcher-start",
        "scheduler-start",
        "scheduler-stop",
        "watcher-stop",
    ]


def test_core_and_stdio_run_with_optional_growth_runtime_blocked():
    """Core recall/context and stdio must not import the legacy Growth runtime."""
    script = textwrap.dedent(
        r"""
        import asyncio
        import importlib.abc
        import pathlib
        import sys
        import tempfile

        blocked_modules = {
            "mneme.notify",
            "mneme.scheduler",
            "mneme.server",
            "mneme.transports.legacy_http",
        }

        class BlockLegacyGrowth(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if (
                    fullname in blocked_modules
                    or fullname == "mneme.growth_lab"
                    or fullname.startswith("mneme.growth_lab.")
                ):
                    raise ImportError(f"blocked optional module: {fullname}")
                return None

        sys.meta_path.insert(0, BlockLegacyGrowth())

        from mneme.core.contracts import CONTRACT_VERSION, CoreCommand
        from mneme.core.registries import RegistryStore
        from mneme.core.service import CoreService
        from mneme.core.vault import Vault
        from mneme.transports import mcp_stdio

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            vault = Vault.initialize(base / "vault", base / "state", "person-01")
            RegistryStore(vault).create_workstream(
                "ws-1", project=None, mode="parallel"
            )
            service = CoreService(vault)

            context = service.execute(
                CoreCommand(
                    CONTRACT_VERSION,
                    "get_context",
                    {"mode": "portable", "workstream_id": "ws-1"},
                )
            )
            recall = service.recall("nothing-yet", 5, None)
            assert context.ok
            assert recall.hits == ()

            app = mcp_stdio.create_app(service)
            tool = asyncio.run(app.get_tool("madi_context"))
            assert tool.fn("ws-1")["ok"] is True

            calls = []
            class App:
                def run(self, *args, **kwargs):
                    calls.append((args, kwargs))

            mcp_stdio.create_app = lambda injected: App()
            mcp_stdio.main(service)
            assert calls == [((), {"transport": "stdio", "show_banner": False})]

        assert not any(
            name in sys.modules
            for name in blocked_modules
        )
        assert not any(
            name == "mneme.growth_lab" or name.startswith("mneme.growth_lab.")
            for name in sys.modules
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

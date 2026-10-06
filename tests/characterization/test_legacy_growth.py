"""Executable boundary for Mneme's legacy Growth modules before relocation."""

import pytest


@pytest.fixture()
def legacy_growth_runtime(tmp_path, monkeypatch):
    """Use a fresh legacy SQLite database without provider or network state."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy-growth.db"))
    from mneme import memory

    memory.init_db()
    return memory


def test_cib_clip_preserves_interval_boundaries():
    from mneme.cib import clip

    assert clip(-0.25) == 0.0
    assert clip(0.4) == 0.4
    assert clip(1.25) == 1.0
    assert clip(-0.25, -0.1, 0.1) == -0.1


def test_seed_protected_negative_reward_is_rejected_before_generation(
    legacy_growth_runtime, monkeypatch
):
    from mneme import llm, skills

    monkeypatch.setattr(
        llm,
        "score_coherence",
        lambda **_kwargs: pytest.fail("protected negative reward must not generate"),
    )
    skills.seed_skill("protected", "must remain protected", seed_protected=True)

    result = skills.update_propensity("protected", reward=-1.0)

    assert result == {
        "applied": False,
        "propensity": 0.5,
        "reason": "seed_protected skill cannot be demoted",
    }
    conn = legacy_growth_runtime.get_connection()
    row = conn.execute(
        "SELECT delta, propensity, use_count, success_count FROM skills WHERE name='protected'"
    ).fetchone()
    conn.close()
    assert tuple(row) == (0.0, 0.5, 1, 0)


def test_outer_loop_insufficient_episode_gate_preserves_response_shape(legacy_growth_runtime):
    from mneme import outer_loop

    assert outer_loop.run_cycle() == {
        "ran": False,
        "reason": "new episodes 0 < 20",
        "new_episodes": 0,
    }


def test_self_model_assess_without_generation_is_graceful_and_persists_shape(
    legacy_growth_runtime, monkeypatch
):
    from mneme import llm, self_model

    conn = legacy_growth_runtime.get_connection()
    with conn:
        conn.execute(
            """INSERT INTO episodes
               (session_id, agent, tool, query, result_summary, success, score)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("legacy-growth", "tester", "episode_reflect", "task", "outcome", 1, 0.75),
        )
    conn.close()

    monkeypatch.setattr(llm, "is_available", lambda: False)
    monkeypatch.setattr(
        llm,
        "assess_episode",
        lambda *_args: pytest.fail("unavailable provider must not assess an episode"),
    )

    result = self_model.assess(force=True)

    assert result == {
        "ran": True,
        "episodes_seen": 1,
        "success_rate": 1.0,
        "growth_rate": 0.0,
        "calibration_error": None,
        "difficulty": 0.6,
        "regulation": "healthy",
        "curriculum": [],
    }
    conn = legacy_growth_runtime.get_connection()
    row = conn.execute("SELECT * FROM self_model").fetchone()
    conn.close()
    assert set(row.keys()) == {
        "id",
        "assessed_at",
        "episodes_seen",
        "success_rate",
        "growth_rate",
        "calibration_error",
        "difficulty",
        "regulation",
        "curriculum",
        "notes",
    }
    assert row["id"] == 1
    assert row["assessed_at"]
    assert {
        key: row[key]
        for key in (
            "episodes_seen",
            "success_rate",
            "growth_rate",
            "calibration_error",
            "difficulty",
            "regulation",
            "curriculum",
            "notes",
        )
    } == {
        "episodes_seen": 1,
        "success_rate": 1.0,
        "growth_rate": 0.0,
        "calibration_error": None,
        "difficulty": 0.6,
        "regulation": "healthy",
        "curriculum": "[]",
        "notes": None,
    }


def test_growth_db_log_and_legacy_mcp_growth_response_shapes(legacy_growth_runtime):
    from mneme import growth
    from mneme.transports import legacy_http

    assert legacy_http.outer_loop_run() == {
        "ran": False,
        "reason": "new episodes 0 < 20",
        "new_episodes": 0,
    }
    assert legacy_http.outer_loop_status() == {"recent_cycles": [], "skill_states": {}}
    assert legacy_http.self_model_status() == {
        "recent": [],
        "regulation": None,
        "difficulty": None,
        "alert": False,
    }
    assert legacy_http.curriculum_suggest() == {"difficulty": 0.5, "targets": []}

    action_id = growth.record(
        "regulation", "critical", "growth-rate", "rate dropped", "regulation:crash"
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
        for key in ("id", "seen_count", "kind", "severity", "subject", "detail", "dedup_key", "status")
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
    assert legacy_http.growth_log() == {"count": 0, "status": "open", "actions": []}


def test_scheduler_tick_runs_outer_loop_before_self_model(monkeypatch):
    from mneme import scheduler

    calls = []
    monkeypatch.setattr(
        scheduler.outer_loop,
        "run_cycle",
        lambda *, force: calls.append(("outer_loop", force)) or {"ran": False},
    )
    monkeypatch.setattr(
        scheduler.self_model,
        "assess",
        lambda *, force: calls.append(("self_model", force)) or {"ran": False},
    )

    scheduler._tick()

    assert calls == [("outer_loop", False), ("self_model", False)]


def test_notify_network_failure_returns_false_without_raising(monkeypatch):
    from mneme import notify

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://notify.invalid/webhook")

    def unavailable(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(notify.httpx, "post", unavailable)

    assert notify.send("notice") is False
    assert notify.notify_degrading([{"name": "study", "to": "degrading", "reason": "low rate"}]) is False

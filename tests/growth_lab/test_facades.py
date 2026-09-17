"""Compatibility contracts for the optional Growth Lab package."""

import importlib
import inspect
from pathlib import Path

import pytest


PUBLIC_INTERFACES = {
    "cib": ("clip", "check"),
    "constitution": (
        "load_constitution",
        "get_coherence_threshold",
        "get_test_scenarios",
        "get_absolute_values",
    ),
    "skills": ("seed_skill", "suggest", "update_propensity"),
    "outer_loop": ("run_cycle", "status"),
    "self_model": ("assess", "status", "curriculum_suggest"),
    "growth": ("record", "resolve", "open_actions", "all_actions", "emit", "main"),
    "log": ("fetch_episodes", "main"),
}


@pytest.mark.parametrize("module_name", PUBLIC_INTERFACES)
def test_legacy_module_is_growth_lab_implementation_with_same_interface(module_name):
    """A copied or wrapping facade must not split module globals or signatures."""
    legacy = importlib.import_module(f"mneme.{module_name}")
    implementation = importlib.import_module(f"mneme.growth_lab.{module_name}")

    assert legacy is implementation
    for name in PUBLIC_INTERFACES[module_name]:
        assert getattr(legacy, name) is getattr(implementation, name)
        assert inspect.signature(getattr(legacy, name)) == inspect.signature(
            getattr(implementation, name)
        )


def test_top_level_monkeypatch_changes_growth_lab_runtime_lookup(monkeypatch):
    """Patching a historical module global must affect implementation calls."""
    from mneme import cib as legacy
    from mneme.growth_lab import cib as implementation

    monkeypatch.setattr(legacy, "DELTA_MAX", 0.01)

    assert implementation.check({}, candidate_delta=0.02, reward=1.0) == {
        "pass": False,
        "reason": "delta 0.020 exceeds ±0.01",
    }


def test_database_path_environment_seam_survives_move(tmp_path, monkeypatch):
    """Growth functions must continue using memory's runtime DB_PATH selection."""
    database = tmp_path / "growth-lab.db"
    monkeypatch.setenv("DB_PATH", str(database))

    from mneme import memory
    from mneme import skills as legacy
    from mneme.growth_lab import skills as implementation

    memory.init_db()
    implementation.seed_skill("portable", "uses the configured database")

    assert database.is_file()
    assert [row["name"] for row in legacy.suggest("portable")] == ["portable"]


def test_constitution_resource_resolves_from_growth_lab_package(tmp_path, monkeypatch):
    """Relocation must move the YAML beside the module and remain CWD-independent."""
    from mneme.growth_lab import constitution

    resource = Path(constitution.__file__).with_name("constitution.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(constitution, "_cache", None)

    loaded = constitution.load_constitution()

    assert resource.is_file()
    assert constitution._CONSTITUTION_PATH == resource
    assert "absolute" in loaded
    assert "test_scenarios" in loaded

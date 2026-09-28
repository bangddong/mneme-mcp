"""Exact integer contract-version regressions."""

import pytest

from mneme.adapters.base import AdapterEnvelope, AdapterResult
from mneme.core.contracts import CoreCommand
from mneme.core.errors import InvalidArtifact


def test_core_contract_rejects_float_version_equal_to_one():
    with pytest.raises(InvalidArtifact, match="version"):
        CoreCommand(1.0, "get_context", {"workstream_id": "ws"})


def test_adapter_envelope_rejects_float_version_equal_to_one():
    with pytest.raises(InvalidArtifact, match="version"):
        AdapterEnvelope.from_mapping(
            {
                "version": 1.0,
                "event": "session_started",
                "adapter": "claude",
                "session_id": "session",
                "workstream_id": "ws",
            }
        )


def test_adapter_result_rejects_float_version_equal_to_one():
    with pytest.raises(InvalidArtifact, match="version"):
        AdapterResult(1.0, "session_started", True, False, False, False)

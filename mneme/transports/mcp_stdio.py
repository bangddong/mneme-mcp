"""On-demand stdio MCP transport for the agent-neutral Madi Core."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from fastmcp import FastMCP

from mneme.core.contracts import (
    CONTRACT_VERSION,
    CommandResult,
    CoreCommand,
    OperationalEvent,
)
from mneme.core.errors import InvalidArtifact
from mneme.core.service import CoreService
from mneme.core.vault import Vault


def create_app(service: CoreService) -> FastMCP:
    """Register transport-only handlers around one explicitly injected Core."""
    if not isinstance(service, CoreService):
        raise TypeError("stdio transport requires a CoreService")
    app = FastMCP("madi")

    @app.tool(name="madi_context")
    def madi_context(workstream_id: str | None = None, mode: str = "portable") -> dict[str, object]:
        """Return bounded context for an explicit or deterministic workstream selection."""
        return _execute(
            service,
            "get_context",
            {"mode": mode, "workstream_id": workstream_id},
        )

    @app.tool(name="madi_recall")
    def madi_recall(
        query: str, limit: int = 10, scope: dict[str, Any] | None = None
    ) -> dict[str, object]:
        """Search deterministic generated recall under current policy."""
        return _recall(service, query, limit, scope)

    @app.tool(name="madi_remember")
    def madi_remember(payload: dict[str, Any]) -> dict[str, object]:
        """Submit one host-selected semantic Memory candidate."""
        return _execute(service, "submit_memory", payload)

    @app.tool(name="madi_checkpoint")
    def madi_checkpoint(payload: dict[str, Any]) -> dict[str, object]:
        """Persist one host-composed immutable Session revision."""
        return _execute(service, "create_session_revision", payload)

    @app.tool(name="madi_decision")
    def madi_decision(payload: dict[str, Any]) -> dict[str, object]:
        """Submit a host-selected personal decision as a Memory candidate."""
        try:
            if not isinstance(payload, Mapping):
                raise InvalidArtifact("decision payload must be a JSON object")
            decision = dict(payload)
            decision["kind"] = "decision"
            return _execute(service, "submit_memory", decision)
        except Exception as exc:
            return CommandResult.failure("submit_memory", exc).as_dict()

    return app


def main(service: CoreService | None = None) -> None:
    """Run stdio only when invoked; startup performs no indexing or daemon work."""
    selected = service if service is not None else _service_from_environment()
    create_app(selected).run(transport="stdio", show_banner=False)


def _service_from_environment() -> CoreService:
    root = os.environ.get("MADI_VAULT_ROOT")
    state_home = os.environ.get("MADI_STATE_HOME")
    if not root or not state_home:
        raise InvalidArtifact(
            "MADI_VAULT_ROOT and MADI_STATE_HOME are required for stdio"
        )
    return CoreService(Vault.open(Path(root), Path(state_home)))


def _execute(
    service: CoreService, command_name: str, payload: object
) -> dict[str, object]:
    try:
        command = CoreCommand(CONTRACT_VERSION, command_name, payload)
    except Exception as exc:
        return CommandResult.failure(command_name, exc).as_dict()
    return service.execute(command).as_dict()


def _recall(
    service: CoreService,
    query: object,
    limit: object,
    scope: object,
) -> dict[str, object]:
    try:
        if not isinstance(query, str) or not query.strip():
            raise InvalidArtifact("recall query must be non-empty text")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise InvalidArtifact("recall limit must be a positive integer")
        if scope is not None and (
            not isinstance(scope, Mapping)
            or not all(isinstance(key, str) for key in scope)
        ):
            raise InvalidArtifact("recall scope must be a JSON object or null")
        recalled = service.recall(query, limit, scope)
        operational_events: tuple[OperationalEvent, ...] = ()
        diagnostic_codes: list[str] = []
        if any(
            "source" in diagnostic and "unavailable" in diagnostic
            for diagnostic in recalled.diagnostics
        ):
            diagnostic_codes.append("source_unavailable")
            operational_events = (
                OperationalEvent(
                    CONTRACT_VERSION,
                    "source_unavailable",
                    {"category": "source"},
                ),
            )
        envelope = CommandResult.success(
            "madi_recall",
            {
                "diagnostic_codes": diagnostic_codes,
                "hits": [_recall_hit(hit) for hit in recalled.hits],
            },
            status=recalled.status,
            operational_events=operational_events,
        )
        return envelope.as_dict()
    except Exception as exc:
        return CommandResult.failure("madi_recall", exc).as_dict()


def _recall_hit(hit: object) -> dict[str, object]:
    path = getattr(hit, "path", None)
    if path is not None and _is_absolute_path(path):
        raise InvalidArtifact("recall result contains an unsafe local path")
    return {
        "artifact_id": hit.artifact_id,
        "authority": hit.authority,
        "category": hit.category,
        "excerpt": hit.excerpt,
        "excerpt_end": hit.excerpt_end,
        "excerpt_start": hit.excerpt_start,
        "path": path,
        "policy_status": hit.policy_status,
        "portability": hit.portability,
        "revision": hit.revision,
        "scope": hit.scope,
        "source_id": hit.source_id,
    }


def _is_absolute_path(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return Path(value).is_absolute() or bool(PureWindowsPath(value).drive)


if __name__ == "__main__":
    main()

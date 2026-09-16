# Optional Madi adapter for Codex `PreCompact`

This integration binds one pinned native Codex command-hook payload to Madi's
agent-neutral lifecycle contract. It is optional and fail-open: a hook or Core
failure emits a closed warning and ordinary Codex work continues.

## Pinned provenance

- Adapter schema ID: `openai-codex-hooks/pre-compact@2026-08-26+a26f1806`
- Release-behavior source, checked 2026-08-26:
  <https://learn.chatgpt.com/docs/hooks>
- Generated input schema pinned to OpenAI Codex commit
  `a26f1806a4f4b8cfec2ea1be129963815a61e58c`:
  <https://github.com/openai/codex/blob/a26f1806a4f4b8cfec2ea1be129963815a61e58c/codex-rs/hooks/schema/generated/pre-compact.command.input.schema.json>

**Official warning:** the release documentation is the behavioral authority.
Schemas on the repository's `main` branch may contain unreleased fields and
must not be treated as released behavior.

The pinned payload requires exactly these seven fields:
`session_id: string`, `transcript_path: string|null`, `cwd: string`,
`hook_event_name: "PreCompact"`, `model: string`, `turn_id: string`, and
`trigger: "manual"|"auto"`. It also permits optional `agent_id` and
`agent_type` fields, which this adapter ignores.

## Boundary

`transcript_path`, `cwd`, `model`, and optional agent fields are never opened or
copied into Core commands or canonical artifacts. Trigger and turn ID remain
adapter-local diagnostic metadata. Session-to-workstream association comes only
from the machine-local `MADI_CODEX_WORKSTREAM_ID` binding; the native payload
does not define a workstream.

The command hook normalizes stdin and exits successfully even when translation
fails. A bare event requests a semantic checkpoint but writes nothing. Only a
separately host-composed, sanitized checkpoint may issue
`create_session_revision`, using the same Core path and privacy checks as the
Claude adapter.

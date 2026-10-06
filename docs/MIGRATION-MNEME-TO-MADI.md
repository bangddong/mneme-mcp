# Staging Legacy Mneme Data for Madi Review

Legacy migration is intentionally a staging workflow, not an import. The existing
Mneme `state.db` and external Wiki remain authoritative and runnable. Staging does
not delete, move, replace, vacuum, checkpoint, or otherwise write either source.
It also does not create a Madi memory, session revision, project, or source
registry.

## Before staging

Stop every Mneme process that can write `state.db`, including the legacy HTTP
server, watcher, scheduler, and ad-hoc SQLite clients. A closed database must have
no adjacent `-wal`, `-shm`, or `-journal` file. Inspection and staging fail closed
when a sidecar is present or the database changes while it is being read.

The V1 staging boundary assumes source writers and filesystem actors are trusted
and quiescent, or cooperate with the staging command. Repeated source snapshots
detect deterministic and cooperative changes; they are not an operating-system
snapshot or a defense against a malicious process that can rewrite paths between
checks.

Keep the external Wiki in place. Madi treats it as a read-only external source,
not project truth to copy into the personal Vault.

## Inspect without writing

Run the CLI with an initialized Vault and its machine-local state root:

```powershell
python -m mneme.cli `
  --vault-root C:\path\to\my-madi `
  --state-home C:\path\to\madi-state `
  migrate inspect `
  --db-path C:\path\to\legacy\state.db
```

The JSON response contains a local operator view of the database digest, table
names, row counts, classifications, and any unknown tables that block portable
action. Inspection creates no staging bundle and does not mutate the Vault or
legacy source.

## Create the local review bundle

```powershell
python -m mneme.cli `
  --vault-root C:\path\to\my-madi `
  --state-home C:\path\to\madi-state `
  migrate stage `
  --db-path C:\path\to\legacy\state.db `
  --wiki-path C:\path\to\legacy-wiki
```

The only published output is below the initialized Vault's machine-local state:

```text
<MADI_STATE_HOME>/vaults/<vault-id>/pending/legacy/<db-sha256>/
|-- manifest.json
|-- source/
|   `-- state.db
`-- tables/
    |-- 000001.json
    `-- ...
```

`source/state.db` is byte-for-byte identical to the closed legacy database. The
Wiki is not copied. Its external path, aggregate digest, and per-file digests are
recorded only in the confidential local manifest. Nothing in the bundle is
written beneath the portable Vault checkout.

The command rejects either direction of canonical overlap between the Wiki and
the local pending root before it creates a lock or output directory. It builds
the bundle in a private sibling temporary directory, writes the manifest, then
revalidates both sources immediately before renaming the directory into place. A
detected database/Wiki mutation, unsafe path, export error, or cooperative
publication conflict removes the temporary bundle and leaves no partial
`<db-sha256>` directory. Existing bundles are checked and refused without being
overwritten.

The machine-local lock serializes cooperating Madi staging commands. Current
cross-platform Python/filesystem primitives do not provide both handle-relative
path traversal and an atomic rename-if-absent operation for directories. There
is therefore a residual check/use window between the final path/source checks
and rename if an untrusted local process can mutate the filesystem. Do not run
staging in a directory writable by an adversarial process; the V1 contract does
not claim protection from that actor.

## Lossless typed export format

`manifest.json` uses `madi.legacy-staging-manifest.v1`. Every table returned by
`sqlite_master`, including recognized SQLite internal tables, generated FTS
shadow tables, user tables whose names resemble `sqlite_*`, and unknown future
tables, has one local `madi.legacy-sqlite-table.v1` JSON export. Each export
records:

- the exact `sqlite_master` schema SQL;
- every `pragma_table_xinfo` column's `cid`, name, declared type, nullability,
  default, primary-key position, hidden code, and generated kind;
- the inspected and exported row count;
- a deterministic ordinal and, in priority order, a hidden `rowid`, guaranteed
  non-null/unique primary key, or full-row deterministic fallback locator;
- every directly selectable visible, generated, and virtual hidden value with
  its SQLite storage class; a virtual-table hidden column that a module refuses
  to select remains present with an explicit unavailable marker rather than
  being silently omitted;
- integers as decimal strings, reals as `float.hex`, valid UTF-8 text as JSON
  text, undecodable `TEXT` bytes as tagged base64 while retaining storage class
  `TEXT`, `NULL` as `null`, and BLOBs as tagged base64;
- a SHA-256 digest of the canonical typed JSON in the manifest.

FTS5's table-named hidden control column is directly selectable, but SQLite
returns a query-context cursor token rather than stored row content. The export
records the observed typed control value and keeps the byte-identical database
snapshot as the durable authority; reviewers must not interpret that token as a
portable datum.

These JSON files are review material. They are not durable Markdown and are not
accepted memory.

## Classification and destination rules

| Legacy data | Staging classification | Destination and action |
| --- | --- | --- |
| `facts`, `episodes` | `candidate-durable-local` / `needs-review` | Confidential local review only; never accepted or portable automatically |
| `skills`, `loop_cycles`, `self_model`, `growth_actions` | `growth-local` | Optional local Growth Lab review |
| Wiki/FTS generated tables | `generated` | Preserved in the snapshot/export but reported rebuildable; no durable Markdown |
| SQLite planner statistics | `generated` | Preserved locally and reported rebuildable |
| SQLite AUTOINCREMENT sequence state | `local-transient` | Preserved locally; no automatic promotion |
| `working`, `meta` with known schema | `local-transient` | Preserved locally; no automatic promotion |
| Any unknown or schema-lookalike table | `needs-review` | Preserved losslessly and blocks portable registration/promotion |

Candidate classification is not acceptance. It never raises a source's
portability ceiling. Table names, counts, hashes, source paths, local IDs, and
even the bundle's existence are potentially confidential and must not be copied
into portable artifacts.

## Register the Wiki separately

After an authorized human chooses an identifier and policy, register the Wiki
through the official policy, registry, and binding APIs. Do not edit YAML files
directly. A local-only registration follows this shape:

```python
from pathlib import Path

from mneme.core.artifacts import StorageClass
from mneme.core.policy import PolicyStore
from mneme.core.registries import RegistryStore
from mneme.core.vault import Vault

vault = Vault.open(Path("C:/path/to/my-madi"), Path("C:/path/to/madi-state"))
policies = PolicyStore(vault)
policy = policies.create_revision(
    "legacy-wiki-policy", "1", {"ceiling": "local-only"}, StorageClass.LOCAL_ONLY
)
policies.activate(policy, expected_generation=0)

sources = RegistryStore(vault, StorageClass.LOCAL_ONLY)
source = sources.register_source(
    "legacy-wiki", kind="filesystem", authority="external-reference"
)
sources.assign_source_policy(source.id, policy, expected_generation=source.generation)
sources.bind_source(source.id, Path("C:/path/to/legacy-wiki"))
```

Choose IDs, revisions, and expected generations from current state rather than
copying the example blindly. The source registry describes the logical,
read-only external authority; only the machine-local binding contains its
absolute path.

## Promote reviewed content separately

Review and sanitize one semantic item at a time, then invoke `remember` with all
independent axes and an explicit requested portability. That command performs a
fresh policy evaluation and initially creates a candidate. It does not reuse a
staging classification as an admission receipt. Acceptance remains a separate
reviewed action through the Memory API.

Never copy raw episodes, raw tool output, secrets, customer data, internal paths,
or unreviewed facts into portable Markdown. Unknown tables must be classified by
an authorized human before any portable action is considered. Keep the legacy
database and Wiki until a separately reviewed retention/deletion decision is
complete; this staging command authorizes no deletion.

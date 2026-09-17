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

The command builds the bundle in a private sibling temporary directory and
renames it into place only after all checks succeed. A database/Wiki mutation,
unsafe path, export error, or publication conflict removes the temporary bundle
and leaves no partial `<db-sha256>` directory. An existing bundle is never
overwritten.

## Lossless typed export format

`manifest.json` uses `madi.legacy-staging-manifest.v1`. Every SQLite table,
including generated FTS shadow tables and unknown future tables, has one local
`madi.legacy-sqlite-table.v1` JSON export. Each export records:

- the exact `sqlite_master` schema SQL;
- every column's name, declared type, nullability, default, and primary-key
  position;
- the inspected and exported row count;
- a deterministic ordinal and a primary-key, `rowid`, or ordinal locator;
- every value with its SQLite storage class;
- integers as decimal strings, reals as `float.hex`, valid UTF-8 text as JSON
  text, undecodable `TEXT` bytes as tagged base64 while retaining storage class
  `TEXT`, `NULL` as `null`, and BLOBs as tagged base64;
- a SHA-256 digest of the canonical typed JSON in the manifest.

These JSON files are review material. They are not durable Markdown and are not
accepted memory.

## Classification and destination rules

| Legacy data | Staging classification | Destination and action |
| --- | --- | --- |
| `facts`, `episodes` | `candidate-durable-local` / `needs-review` | Confidential local review only; never accepted or portable automatically |
| `skills`, `loop_cycles`, `self_model`, `growth_actions` | `growth-local` | Optional local Growth Lab review |
| Wiki/FTS generated tables | `generated` | Preserved in the snapshot/export but reported rebuildable; no durable Markdown |
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

# Local Knowledge Hub

Local Knowledge Hub gives Codex and Antigravity one project-isolated local knowledge layer. It indexes project files locally, keeps controlled long-term memory, and optionally provides Onyx and private SearXNG web search. Legacy Antigravity IDE integration remains available as an explicit installer option.

It does **not** synchronize or modify native chat databases.

## What's new in 1.4.0

- Scoped Chinese phrase retries, exact file/requirement-ID match explanations,
  and explicit empty-result diagnostics.
- Client-reported task-end memory review receipts, separated into business,
  maintenance, test and unclassified usage. This is not a native task-end hook.
- A separate, default-off, owner-approved document reference layer. Historical
  memory candidates are not automatically promoted.
- Empty workspaces report that indexing is awaiting content; loaded-code hashes
  show whether a reconnect is needed.
- Unified data-root selection, read-only bounded health probes, explicit macOS
  scheduler errors, Onyx environment propagation, and bounded web decompression.

Reconnect Local Knowledge MCP in Codex and Antigravity after updating to load
the new tool catalog. See [retrieval and review quality](docs/usage-quality.md)
for safety boundaries, configuration and rollback controls.

## Requirements

### Windows

- 64-bit Windows 10 or Windows 11
- Windows PowerShell 5.1 or PowerShell 7
- 64-bit Python 3.11 or newer
- For Onyx and web search: Docker Desktop with Linux containers
- At least 10GB RAM is recommended when running Onyx

### macOS

- macOS 13 or newer
- Python 3.11 or newer
- For Onyx and web search: Docker Desktop, or Homebrew `docker` + `colima`
- At least 10GB RAM is recommended when running Onyx

## Install

Clone the repository or download the platform archive from
[GitHub Releases](https://github.com/aoright/local-knowledge-hub/releases).

### Windows one-click install

Download `LocalKnowledgeHub-Setup-1.4.0.exe` and double-click it. The setup
wizard lets you choose between the complete installation and the core-only
installation. It installs for the current user and does not require administrator
privileges. The **Enable automatic updates (recommended)** option is selected by
default and may be cleared before installation.

The executable is currently unsigned, so Windows SmartScreen may show an
unknown-publisher warning. Verify its SHA-256 file from the same GitHub Release
before running it.

Portable alternative: extract `local-knowledge-hub-windows-1.4.0.zip` and
double-click `Install-Local-Knowledge-Hub.cmd`, or open PowerShell and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The default location is:

```text
%LOCALAPPDATA%\LocalKnowledgeHub
```

Core-only installation without Onyx or SearXNG:

```powershell
.\install.ps1 -WithoutServices
```

### macOS install

```bash
./install.sh
```

Core-only installation without Onyx or SearXNG:

```bash
./install.sh --without-services
```

The default macOS location is `~/.local/share/local-knowledge-hub`.

After installation, restart Codex and Antigravity. New tasks automatically retrieve relevant local project context; no special prompt is required.

The installer records one canonical data root in `.knowledge-hub-data-root` and
uses it consistently for clients, wrappers, indexing, backups, services, and
updates. When upgrading a legacy installation, a substantially richer existing
`runtime` database is retained automatically instead of silently creating a
second empty `data` database.

Version 1.3.3 adds a conservative multi-term fallback when strict lexical
quality filtering would otherwise turn useful candidates into a false zero-hit.
Global context is routed to the relevant user, engineering, hardware, or
operations scope and now reports trusted and candidate coverage explicitly.
Backup maintenance can remove rebuildable full-index archives while retaining
verified critical backups, and health output includes the same global coverage
diagnostics used by status checks.

Version 1.3.2 restores the macOS automatic-update LaunchAgent at login and on
its daily schedule, reports the built-in global supplement as global instead of
as a user collection, and requires complete evidence-backed arguments for memory
updates, moves, and deletion. Context retrieval now stays on the fast lexical path
until the local embedding model has completed its first real inference, avoiding
multi-second foreground stalls during background warmup.

Version 1.3.1 bounds each project to 25,000 documents and 300,000 chunks by
default, keeps manifests and top-level areas fairly represented, and prevents an
existing oversized index from growing until it is explicitly reviewed. Usage
telemetry no longer blocks foreground retrieval behind indexing transactions,
and indexing commits smaller batches. Index pruning and SQLite compaction are
dry-run by default and require exact, explicit confirmation. Index and backup
jobs run once at login as well as on their normal schedules, while the service
watchdog repairs stale maintenance serially. Health checks use lightweight probes,
and automatic memory capture requires one complete, evidence-backed argument set
instead of retrying partial calls.

Version 1.3.0 adds verified self-updates. A per-user daily task checks GitHub's
stable latest Release, compares semantic versions, downloads only the asset for the
current platform, verifies its SHA-256 digest, and preserves the existing data,
service mode, and update preference during installation. Automatic updates are on
for new installations but remain user-controlled.

Version 1.2.3 treats healthy Onyx and SearXNG HTTP endpoints as authoritative, so
the watchdog no longer runs `docker info` every minute or starts a recovery cycle
because of a transient Docker CLI timeout. MCP audits distinguish Codex,
Antigravity, and Antigravity IDE from safe parent-process markers without storing
command arguments or CSRF tokens. Project status now identifies zero-document and
missing-path registrations. `project-quality-review` is dry-run by default and can
remove only explicitly named metadata records; it never deletes project directories.
An explicit, previously unknown workspace path is now registered even when it is a
non-Git document project; inferred Antigravity paths remain Git-only. PDF indexing
extracts bounded text through Poppler or the cross-platform `pypdf` fallback, keeps
titles for scans and oversized files, and adds a project-scoped substring fallback
for unsegmented Chinese search terms.

Version 1.2.2 validates automatic memory from the user's original evidence only.
Questions, transient UI feedback, and implementation requests are rejected, and an
assistant-generated title cannot promote project memory into a global scope. Existing
noisy automatic memories can be reviewed and reversibly demoted to candidates. WAL
checkpoints now run with short busy timeouts, while backup pruning remains a dry run
until an operator explicitly applies the exact target list.

Version 1.2.1 also reviews the user's own messages at task completion for explicit,
durable project decisions, facts, constraints, and runbooks. Eligible items are
captured automatically in the current project; ordinary requests, implementation
results, transient debugging, and inferred information are not stored as memory.
Generated `test.log` files are excluded from indexing, and empty global or memory
scopes are skipped before full-text or embedding work begins.

Project hints may be a slug, display name, workspace path, or a longer human label
containing a unique project name. Ambiguous or unknown hints fail closed and never
fall through to another project. Context lookup uses a project-partitioned full-text
index and returns its lexical fast path immediately while the optional embedding
model warms in the background. Web search retries through SearXNG's default engines
and a conservatively simplified query when configured engines return no usable
results. The `knowledge_context` tool schema requires `workspace_path`, preventing
IDE clients from silently issuing an unscoped query when the MCP process itself was
started from `/`. If Antigravity omits the field anyway, the gateway reads only the
most recently user-active conversation's local workspace metadata, requires a unique
recent match, and resolves that path without reading conversation content. A new Git
workspace inferred this way is registered and indexed automatically; an explicit
absolute workspace path may also create a non-Git document project. Stale or
ambiguous inferred activity still fails closed.

## Commands

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\khub.cmd" status
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-doctor.cmd"
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\khub.cmd" web-search "latest MCP specification"
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-update.cmd" status
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-update.cmd" check
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-update.cmd" install
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-update.cmd" set-auto off
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-update.cmd" set-auto on
```

macOS:

```bash
~/.local/share/local-knowledge-hub/bin/khub status
~/.local/share/local-knowledge-hub/bin/knowledge-hub-doctor
~/.local/share/local-knowledge-hub/bin/khub web-search 'latest MCP specification'
~/.local/share/local-knowledge-hub/bin/knowledge-hub-update status
~/.local/share/local-knowledge-hub/bin/knowledge-hub-update check
~/.local/share/local-knowledge-hub/bin/knowledge-hub-update install
~/.local/share/local-knowledge-hub/bin/knowledge-hub-update set-auto off
~/.local/share/local-knowledge-hub/bin/knowledge-hub-update set-auto on
```

Onyx is available at <http://127.0.0.1:3000>. On first use, create the first account using the generated credentials under `config/admin.env` inside the selected data root. Do not share this file.

## Automatic maintenance

- Windows uses per-user Task Scheduler jobs for service health, 30-minute incremental indexing, daily backups, and daily verified updates.
- The always-running service watchdog also checks index and critical-backup freshness once per minute. If the operating-system scheduler misses work, it serially dispatches a critical backup first and indexing next. Default stale thresholds are two hours for indexing and 26 hours for backups; `maintenance.py health` reports both without running a blocking full-database integrity scan.
- Zero-document project review is non-destructive by default:

  ```bash
  python app/src/maintenance.py project-quality-review --minimum-age-days 7
  ```

  Existing project paths require both `--allow-existing-zero-docs` and an exact
  `--project <slug>` together with `--apply`. Only the knowledge-hub registration
  is removed; the local directory and its files are never touched.
- macOS uses per-user LaunchAgents for the same jobs. Index and backup agents also run once at login before their normal interval/calendar schedules. Turning automatic updates off keeps the local job installed but makes it exit before any network request, so it can be re-enabled without reinstalling.
- Existing Codex and Antigravity MCP configuration is preserved; only the managed `local-knowledge` entry is added or updated. Obsolete Local Knowledge Hub LaunchAgents from legacy installations are removed only when they point into the same installation directory.
- Git projects—and collection folders containing multiple Git repositories—use content-sensitive working-tree fingerprints, so unchanged projects avoid repeated full file walks. A full verification scan still runs at least once every 24 hours.
- Project index budgets default to 25,000 documents, 300,000 chunks, and 4,096 chunks per document. Review them with `python app/src/maintenance.py index-budget-review`. Applying a review requires both an exact `--project <slug>` and `--apply`; a critical backup is created first and project source files are never deleted. Run `compact-index` for a dry-run disk-space check. Actual compaction additionally requires `--apply --confirm-clients-stopped` after all configured clients are closed.
- Daily scheduled backups preserve project registration, collections, long-term memories, history, embeddings, and audit records while omitting rebuildable file indexes and web caches. Existing full backups are retained separately; `maintenance.py backup --mode full` remains available for manual snapshots.
- MCP initialization and tool calls are recorded locally with client name, success state, and duration. Tool arguments and project contents are not written to the usage audit.
- Codex automation rules are stored in `~/.codex/AGENTS.md`; Antigravity rules are stored in the official `~/.gemini/GEMINI.md` location. Older managed Antigravity rules are migrated without removing unrelated user instructions.

## Upgrade

Automatic updates are enabled by default. Use `knowledge-hub-update status` to inspect the setting, `set-auto off|on` to change it, `check` to check without installing, and `install` to update immediately. The updater accepts only the stable GitHub latest Release, restricts downloads to GitHub asset hosts, and requires a matching SHA-256 digest from Release metadata or the companion checksum asset. Manual installation remains supported: extract a newer release and run the platform installer again. Application files and the virtual environment are updated; the selected data root and generated secrets are retained.

## Uninstall

Windows:

```powershell
.\uninstall.ps1
.\uninstall.ps1 -PurgeData
```

macOS:

```bash
./uninstall.sh
./uninstall.sh --purge-data
```

The normal uninstall keeps the knowledge database and backups. The purge option removes local data and Docker volumes as well.

## Privacy boundary

- Project databases, memories, embeddings, web caches, credentials, and backups stay under the installer-selected local data root.
- Project retrieval is isolated by project or explicit collection.
- Web content is marked untrusted and is never automatically promoted to durable memory.
- The distribution contains no publisher project data or credentials.

## License

Local Knowledge Hub integration code is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Personal, research,
educational, charitable, and other noncommercial use is permitted under that
license. Commercial use requires separate permission from the licensor.

This is a noncommercial source-available license, not an OSI-approved open
source license. The included Onyx deployment material remains under Onyx's own
license; see `app/vendor/onyx/LICENSE`. Other dependencies retain their own
licenses; see `THIRD_PARTY_NOTICES.md`.

# Local Knowledge Hub

Local Knowledge Hub gives Codex, Antigravity, and Antigravity IDE one project-isolated local knowledge layer. It indexes project files locally, keeps controlled long-term memory, and optionally provides Onyx and private SearXNG web search.

It does **not** synchronize or modify native chat databases.

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

Download `LocalKnowledgeHub-Setup-1.1.1.exe` and double-click it. The setup
wizard lets you choose between the complete installation and the core-only
installation. It installs for the current user and does not require administrator
privileges.

The executable is currently unsigned, so Windows SmartScreen may show an
unknown-publisher warning. Verify its SHA-256 file from the same GitHub Release
before running it.

Portable alternative: extract `local-knowledge-hub-windows-1.1.1.zip` and
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

After installation, restart Codex, Antigravity, and Antigravity IDE. New tasks automatically retrieve relevant local project context; no special prompt is required.

## Commands

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\khub.cmd" status
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\knowledge-hub-doctor.cmd"
& "$env:LOCALAPPDATA\LocalKnowledgeHub\bin\khub.cmd" web-search "latest MCP specification"
```

macOS:

```bash
~/.local/share/local-knowledge-hub/bin/khub status
~/.local/share/local-knowledge-hub/bin/knowledge-hub-doctor
~/.local/share/local-knowledge-hub/bin/khub web-search 'latest MCP specification'
```

Onyx is available at <http://127.0.0.1:3000>. On first use, create the first account using the generated credentials under `data/config/admin.env` in the installation directory. Do not share this file.

## Automatic maintenance

- Windows uses three per-user Task Scheduler jobs for service health, 30-minute incremental indexing, and daily backups.
- macOS uses per-user LaunchAgents for the same jobs.
- Existing Codex and Antigravity MCP configuration is preserved; only the managed `local-knowledge` entry is added or updated.

## Upgrade

Extract a newer release and run the platform installer again. Application files and the virtual environment are updated; the `data` directory and generated secrets are retained.

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

- Project databases, memories, embeddings, web caches, credentials, and backups stay under the local `data` directory.
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

# Local Knowledge Hub for macOS

Local Knowledge Hub gives Codex, Antigravity, and Antigravity IDE one project-isolated local knowledge layer. It indexes project files locally, keeps controlled long-term memory, and optionally provides Onyx and private SearXNG web search.

It does **not** synchronize or modify native chat databases.

## Requirements

- macOS 13 or newer
- Python 3.11 or newer
- For Onyx and web search: Docker Desktop, or Homebrew `docker` + `colima`
- At least 10GB RAM is recommended when running Onyx

## Install

Clone the repository and install:

```bash
git clone https://github.com/aoright/local-knowledge-hub.git
cd local-knowledge-hub
./install.sh
```

Alternatively, download the latest `.tar.gz` and `.sha256` files from
[GitHub Releases](https://github.com/aoright/local-knowledge-hub/releases),
then verify the archive before extracting it:

```bash
shasum -a 256 -c local-knowledge-hub-macos-1.0.0.tar.gz.sha256
```

Core-only installation without Docker services:

```bash
./install.sh --without-services
```

The default installation directory is `~/.local/share/local-knowledge-hub`.

After installation, restart Codex, Antigravity, and Antigravity IDE. New tasks automatically retrieve relevant local project context; no special prompt is required.

## Commands

```bash
~/.local/share/local-knowledge-hub/bin/khub status
~/.local/share/local-knowledge-hub/bin/knowledge-hub-doctor
~/.local/share/local-knowledge-hub/bin/khub web-search 'latest MCP specification'
```

Onyx is available at <http://127.0.0.1:3000>. On first use, create the first account using the locally generated credentials in:

```text
~/.local/share/local-knowledge-hub/data/config/admin.env
```

This file is mode `0600`; do not share it.

## Upgrade

Extract a newer release and run `./install.sh` again. Application files and the virtual environment are updated; the `data` directory is retained.

## Uninstall

```bash
./uninstall.sh
```

This removes the application and client integration but keeps the knowledge database and backups. To remove everything:

```bash
./uninstall.sh --purge-data
```

## Privacy boundary

- Project databases, memories, embeddings, web caches, credentials, and backups stay under the local `data` directory.
- Project retrieval is isolated by project or explicit collection.
- Web content is marked untrusted and is never automatically promoted to durable memory.
- The distribution contains no publisher project data or credentials.

## Licensing note

Local Knowledge Hub integration code is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Personal, research,
educational, charitable, and other noncommercial use is permitted under that
license. Commercial use requires separate permission from the licensor.

This is a noncommercial source-available license, not an OSI-approved open
source license. The included Onyx deployment material remains under Onyx's own
license; see `app/vendor/onyx/LICENSE`. Other dependencies retain their own
licenses; see `THIRD_PARTY_NOTICES.md`.

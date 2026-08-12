#!/bin/sh
set -eu

PACKAGE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_ROOT=${LOCAL_KNOWLEDGE_HOME:-"$HOME/.local/share/local-knowledge-hub"}
WITH_SERVICES=1
START_NOW=1
INSTALL_DEPS=1

usage() {
  printf '%s\n' \
    "Usage: ./install.sh [options]" \
    "  --install-dir PATH       Installation directory" \
    "  --without-services       Do not install/start Onyx and SearXNG launch agent" \
    "  --no-start               Install services but do not start them now" \
    "  --skip-python-deps       Test/offline mode; do not pip install requirements"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_ROOT=$2; shift 2 ;;
    --without-services) WITH_SERVICES=0; shift ;;
    --no-start) START_NOW=0; shift ;;
    --skip-python-deps) INSTALL_DEPS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$INSTALL_ROOT" in
  ""|/|"$HOME"|"$HOME"/) printf 'Unsafe install directory: %s\n' "$INSTALL_ROOT" >&2; exit 2 ;;
esac

if [ "$(uname -s)" != "Darwin" ]; then
  printf 'This release supports macOS only.\n' >&2
  exit 1
fi
if [ ! -d "$PACKAGE_ROOT/app" ]; then
  printf 'Package is incomplete: app/ is missing.\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Python 3.11 or newer is required. Install it with Homebrew or python.org.\n' >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf 'Python 3.11 or newer is required.\n' >&2
  exit 1
fi
if [ "$WITH_SERVICES" -eq 1 ] && ! command -v docker >/dev/null 2>&1; then
  printf 'Docker is required for Onyx/SearXNG. Install Docker Desktop, or rerun with --without-services.\n' >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT" "$INSTALL_ROOT/data" "$INSTALL_ROOT/bin"
chmod 700 "$INSTALL_ROOT" "$INSTALL_ROOT/data" "$INSTALL_ROOT/bin"

NEW_APP="$INSTALL_ROOT/.app.new.$$"
PREVIOUS_APP="$INSTALL_ROOT/.app.previous"
/bin/cp -R "$PACKAGE_ROOT/app" "$NEW_APP"
if [ -d "$PREVIOUS_APP" ]; then
  /bin/rm -rf "$PREVIOUS_APP"
fi
if [ -d "$INSTALL_ROOT/app" ]; then
  mv "$INSTALL_ROOT/app" "$PREVIOUS_APP"
fi
mv "$NEW_APP" "$INSTALL_ROOT/app"
/bin/cp "$PACKAGE_ROOT/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"
/bin/cp "$PACKAGE_ROOT/README.md" "$INSTALL_ROOT/README.md"
chmod 700 "$INSTALL_ROOT/uninstall.sh"

if [ ! -x "$INSTALL_ROOT/venv/bin/python3" ]; then
  python3 -m venv "$INSTALL_ROOT/venv"
fi
if [ "$INSTALL_DEPS" -eq 1 ]; then
  "$INSTALL_ROOT/venv/bin/python3" -m pip install --disable-pip-version-check -r "$INSTALL_ROOT/app/requirements.txt"
fi

MANAGER_ARGS=""
if [ "$WITH_SERVICES" -eq 0 ]; then
  MANAGER_ARGS="--without-services"
fi
# shellcheck disable=SC2086
"$INSTALL_ROOT/venv/bin/python3" "$INSTALL_ROOT/app/src/install_manager.py" initialize \
  --install-root "$INSTALL_ROOT" --source-app "$INSTALL_ROOT/app" $MANAGER_ARGS

export KHUB_DATA_DIR="$INSTALL_ROOT/data"
"$INSTALL_ROOT/bin/khub" init >/dev/null
"$INSTALL_ROOT/venv/bin/python3" "$INSTALL_ROOT/app/src/discover_projects.py"
"$INSTALL_ROOT/venv/bin/python3" "$INSTALL_ROOT/app/src/export_mcp_catalog.py" >/dev/null

if [ "$WITH_SERVICES" -eq 1 ] && [ "$START_NOW" -eq 1 ]; then
  "$INSTALL_ROOT/venv/bin/python3" "$INSTALL_ROOT/app/src/start_services.py" --once
fi

"$INSTALL_ROOT/bin/knowledge-hub-doctor"
printf '\nInstalled Local Knowledge Hub at:\n  %s\n' "$INSTALL_ROOT"
printf 'Restart Codex, Antigravity, and Antigravity IDE to load local-knowledge.\n'
if [ "$WITH_SERVICES" -eq 1 ]; then
  printf 'Onyx: http://127.0.0.1:3000\n'
  printf 'First-account credentials: %s/data/config/admin.env\n' "$INSTALL_ROOT"
fi

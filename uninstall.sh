#!/bin/sh
set -eu

SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$SCRIPT_ROOT/app" ] || [ -d "$SCRIPT_ROOT/data" ]; then
  DEFAULT_INSTALL_ROOT=$SCRIPT_ROOT
else
  DEFAULT_INSTALL_ROOT="$HOME/.local/share/local-knowledge-hub"
fi
INSTALL_ROOT=${LOCAL_KNOWLEDGE_HOME:-"$DEFAULT_INSTALL_ROOT"}
PURGE_DATA=0

usage() {
  printf '%s\n' \
    "Usage: ./uninstall.sh [--install-dir PATH] [--purge-data]" \
    "Without --purge-data, the database, models, and backups are retained."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_ROOT=$2; shift 2 ;;
    --purge-data) PURGE_DATA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$INSTALL_ROOT" in
  ""|/|"$HOME"|"$HOME"/) printf 'Unsafe install directory: %s\n' "$INSTALL_ROOT" >&2; exit 2 ;;
esac

PYTHON="$INSTALL_ROOT/venv/bin/python3"
MANAGER="$INSTALL_ROOT/app/src/install_manager.py"
if [ -x "$PYTHON" ] && [ -f "$MANAGER" ]; then
  "$PYTHON" "$MANAGER" unconfigure --install-root "$INSTALL_ROOT"
fi

if command -v docker >/dev/null 2>&1; then
  docker compose -p knowledge-search down >/dev/null 2>&1 || true
  docker compose -p knowledge-hub down >/dev/null 2>&1 || true
fi

for target in app .app.previous venv bin uninstall.sh README.md; do
  if [ -e "$INSTALL_ROOT/$target" ]; then
    /bin/rm -rf "$INSTALL_ROOT/$target"
  fi
done

if [ "$PURGE_DATA" -eq 1 ]; then
  /bin/rm -rf "$INSTALL_ROOT/data"
  rmdir "$INSTALL_ROOT" 2>/dev/null || true
  printf 'Removed application and data from %s\n' "$INSTALL_ROOT"
else
  printf 'Removed application; retained data at %s/data\n' "$INSTALL_ROOT"
fi

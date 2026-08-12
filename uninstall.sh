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
SERVICES="$INSTALL_ROOT/app/src/start_services.py"
export KHUB_DATA_DIR="$INSTALL_ROOT/data"
if [ -x "$PYTHON" ] && [ -f "$SERVICES" ]; then
  if [ "$PURGE_DATA" -eq 1 ]; then
    "$PYTHON" "$SERVICES" --stop --purge >/dev/null 2>&1 || true
  else
    "$PYTHON" "$SERVICES" --stop >/dev/null 2>&1 || true
  fi
fi
if [ -x "$PYTHON" ] && [ -f "$MANAGER" ]; then
  "$PYTHON" "$MANAGER" unconfigure --install-root "$INSTALL_ROOT"
fi

for target in app .app.previous venv bin uninstall.sh README.md LICENSE THIRD_PARTY_NOTICES.md; do
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

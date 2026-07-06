#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${FENGLING_APP_ROOT:-$HOME/fengling-studio}"

mkdir -p "$APP_ROOT/scripts" "$APP_ROOT/evidence"
cp "$PLUGIN_ROOT/scripts/fengling-backend/scripts/suno_auto_recut_upload.py" \
  "$APP_ROOT/scripts/suno_auto_recut_upload.py"
cp "$PLUGIN_ROOT/scripts/fengling-backend/evidence/studio_blank_state_template.json" \
  "$APP_ROOT/evidence/studio_blank_state_template.json"

make -C "$PLUGIN_ROOT/scripts/fengling-cli" install-local
fengling --json config init --app-root "$APP_ROOT"

printf 'Installed Fengling app root: %s\n' "$APP_ROOT"

#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install firefox

printf '\nInstalação concluída. Valide com:\n  %s/.venv/bin/python %s/watcher.py --dry-run\n' "$ROOT" "$ROOT"

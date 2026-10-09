#!/usr/bin/env bash
set -euo pipefail
umask 077
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="${PYTHON:-python3}"
credential_mode="${1:-}"
if [[ "$credential_mode" != "" && "$credential_mode" != "--credentials" && "$credential_mode" != "--no-credentials" ]] || [[ "$#" -gt 1 ]]; then
  echo 'Usage: ./install.sh [--credentials|--no-credentials]' >&2; exit 2
fi
"$python_bin" - <<'PY'
import sys, sqlite3
if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10 or newer is required.')
try:
    sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE probe USING fts5(content)')
except sqlite3.Error:
    raise SystemExit('This Python SQLite build lacks FTS5; use a Python build with FTS5 enabled.')
PY
command -v codex >/dev/null || { echo 'Codex CLI is required on PATH.' >&2; exit 1; }
if [[ ! -x "$app_dir/.venv/bin/python" ]]; then
  "$python_bin" -m venv "$app_dir/.venv" || { echo 'Python venv support is unavailable; install the matching python3-venv package through your system administrator.' >&2; exit 1; }
fi
"$app_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$app_dir/requirements.txt"
if [[ "$credential_mode" == "--credentials" ]]; then
  "$app_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$app_dir/requirements-credentials.txt"
  "$app_dir/.venv/bin/python" -m playwright install chromium
fi
"$app_dir/.venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$app_dir"
"$app_dir/.venv/bin/python" -m codex_memory.maintenance status
if [[ "$credential_mode" != "" ]]; then
  "$app_dir/.venv/bin/python" -m codex_memory.configure install --app "$app_dir" "$credential_mode"
else
  "$app_dir/.venv/bin/python" -m codex_memory.configure install --app "$app_dir"
fi
echo 'Codex Recall installed. Start a new Codex session to load the tools and guidance.'
if [[ "$credential_mode" == "--credentials" ]]; then
  echo 'Saved login support enabled. Enter passwords in your terminal with .venv/bin/codex-recall-credentials add; see docs/credentials.md.'
fi

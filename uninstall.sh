#!/usr/bin/env bash
set -euo pipefail
umask 077
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${1:-}" != "" && "${1:-}" != "--delete-data" ]]; then
  echo 'Usage: ./uninstall.sh [--delete-data]' >&2; exit 2
fi
if [[ ! -x "$app_dir/.venv/bin/python" ]]; then
  echo 'Virtual environment is missing. Re-run install.sh first to restore administration tools.' >&2; exit 1
fi
uninstall_result="$("$app_dir/.venv/bin/python" -m codex_memory.configure uninstall --app "$app_dir")"
printf '%s\n' "$uninstall_result"
if [[ "${1:-}" == "--delete-data" ]]; then
  CODEX_RECALL_UNINSTALL_RESULT="$uninstall_result" "$app_dir/.venv/bin/python" - <<'PY'
import json, os
from pathlib import Path
from codex_memory.database import database_lock
registered = json.loads(os.environ['CODEX_RECALL_UNINSTALL_RESULT']).get('registered_database')
if not registered:
    raise SystemExit('No managed database path was registered; memories were retained. Inspect and remove the intended data files explicitly.')
path = Path(registered)
with database_lock(path, exclusive=True):
    for target in [path, path.with_name(path.name+'-wal'), path.with_name(path.name+'-shm')]:
        if target.is_symlink():
            raise SystemExit('Refusing symlinked data. Inspect the database path.')
    for target in [path, path.with_name(path.name+'-wal'), path.with_name(path.name+'-shm')]:
        target.unlink(missing_ok=True)
print('Active memory database removed. Backups and filesystem copies are retained.')
PY
fi
rm -rf -- "$app_dir/.venv"
echo 'Codex Recall removed. Source files and backups remain; memories are retained unless --delete-data was supplied. Start a new Codex session.'
echo 'Saved keyring credentials and dedicated browser session files are retained; see docs/credentials.md for removal.'

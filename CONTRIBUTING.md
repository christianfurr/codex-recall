# Contributing

Keep Codex Recall small, local and predictable. New features should solve a concrete memory problem without requiring a model, credentials, a network service or a daemon.

Use Python 3.10+ on Linux with SQLite FTS5. For development without changing Codex configuration:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and synthetic credentials. Do not include personal memories, configuration backups, access tokens or real chat transcripts in issues, fixtures or commits. Test schema migrations, filtering, FTS synchronization and security behavior when changing the database.

The dependency lock includes SDK transitive dependencies. Update the direct versions in `pyproject.toml`, regenerate `requirements.txt` in a clean Python 3.10 environment, and run the suite and `pip check`. Keep the lock compatible with supported Python versions. Changes to Codex registration must preserve unrelated settings and remain reversible.

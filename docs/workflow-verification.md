# Picking up where the last session left off

Codex Recall's practical benefit is keeping confirmed preferences and project
decisions available after a session ends. A new task does not always mention the
words in those decisions, so the startup brief loads a small amount of scoped
context before keyword search.

This local verification uses the actual Codex app-server MCP transport across
three independent processes. It does not start a model turn or call a cloud API.
The case and acceptance checks are declared in
[`verify_workflow.py`](../scripts/verify_workflow.py) before execution, and the
machine-readable result is in [`workflow.json`](../verification/workflow.json).

## The task

The next session receives the task **“Build settings page.”** The preceding
session saved these confirmed synthetic facts:

| Context | Saved fact |
| --- | --- |
| Global preference | Use Bun for JavaScript package commands. |
| Atlas authentication | Atlas uses Clerk for authentication. |
| Atlas deployment | Atlas deploys its API to Fly.io. |
| Another project | Beacon uses Auth0 for authentication. |

The task's words match none of these facts. Searching that task alone should
return no keyword matches. The two bounded startup calls should recover the
global preference and Atlas decisions while excluding Beacon:

```text
list_memories(scope="global", category="preferences", limit=3)
list_memories(scope="project", project="atlas", limit=5)
```

The verification then saves **“Atlas deploys its API to Railway,”** marks the
Fly.io decision superseded, and starts another independent process. Its startup
context must contain the replacement decision and exclude the previous one.

## What passed

The fresh database initially contained no startup context. After the first
process saved the four facts and exited, the second process recovered all three
relevant facts through the startup brief. Searching the task itself returned
no matches, and the other project's decision stayed excluded.

An independent official MCP-client initialization handshake also confirmed that
the installed server advertises the bounded startup brief and the instruction
to treat saved memories as potentially outdated reference data.

After changing the deployment decision and restarting again, the third process
recovered the Bun preference, Clerk decision, and current Railway decision.
The old Fly.io decision and Beacon fact were excluded. SQLite and search-index
integrity checks passed. All test processes stopped and the temporary database
was removed; the owner's configuration and normal database contents remained
unchanged.

This demonstrates that saved context is available to a later session even when
the task uses different words, and that a replaced decision does not return in
the default brief. It does not measure whether a model follows that context or
improves its coding, reasoning, speed, or token usage. Those outcomes require
separate model-driven evaluation.

## Run it yourself

Install Codex Recall and ensure the `codex` command is available, then run from
the repository root:

```bash
.venv/bin/python scripts/verify_workflow.py
```

To keep a separate report:

```bash
.venv/bin/python scripts/verify_workflow.py --output /tmp/recall-workflow.json
```

The script reads the existing Codex registration, disables unrelated MCP servers
and plugins only for its test processes, and overrides the memory database path
only for those processes. It uses synthetic facts in a temporary directory and
does not edit Codex settings or the user's regular memories. A failed acceptance
check exits with an error instead of writing a success report.

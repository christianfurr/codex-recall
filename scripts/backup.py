#!/usr/bin/env python3
"""Compatibility wrapper: run with the project's virtual-environment Python."""

import sys

from codex_memory.maintenance import main

raise SystemExit(main(["backup", *sys.argv[1:]]))

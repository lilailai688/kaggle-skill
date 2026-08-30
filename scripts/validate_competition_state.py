#!/usr/bin/env python3
"""Validate a Kaggle skill competition workspace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kaggle_core.workspace import validate_workspace as validate_v2_workspace


REQUIRED_DIRS = ["configs", "data", "flags", "notebooks", "reports", "runs", "submissions"]
REQUIRED_FILES = ["STATE.md", "ideas_backlog.md", "experiment_ledger.jsonl"]
STATE_MARKERS = [
    "Competition slug:",
    "Metric:",
    "Direction:",
    "Current stage:",
    "Next action:",
]


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return records, [f"missing file: {path}"]
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"experiment_ledger.jsonl:{line_no}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(item, dict):
            errors.append(f"experiment_ledger.jsonl:{line_no}: record must be an object")
            continue
        if "run_id" not in item:
            errors.append(f"experiment_ledger.jsonl:{line_no}: missing run_id")
        if "status" not in item:
            errors.append(f"experiment_ledger.jsonl:{line_no}: missing status")
        records.append(item)
    return records, errors


def validate_workspace(workspace: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not workspace.exists():
        return [f"workspace does not exist: {workspace}"], warnings
    if not workspace.is_dir():
        return [f"workspace is not a directory: {workspace}"], warnings

    for name in REQUIRED_DIRS:
        if not (workspace / name).is_dir():
            errors.append(f"missing directory: {name}")
    for name in REQUIRED_FILES:
        if not (workspace / name).is_file():
            errors.append(f"missing file: {name}")

    state_path = workspace / "STATE.md"
    if state_path.exists():
        state = state_path.read_text(encoding="utf-8")
        for marker in STATE_MARKERS:
            if marker not in state:
                errors.append(f"STATE.md missing marker: {marker}")

    ideas_path = workspace / "ideas_backlog.md"
    if ideas_path.exists():
        ideas = ideas_path.read_text(encoding="utf-8").lower()
        if "- [ ]" not in ideas and "status: todo" not in ideas:
            warnings.append("ideas_backlog.md has no todo ideas")

    _, ledger_errors = read_jsonl(workspace / "experiment_ledger.jsonl")
    errors.extend(ledger_errors)

    return errors, warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", help="Competition workspace directory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(
        "warning: validate_competition_state.py is deprecated; use kaggle_ops.py validate",
        file=sys.stderr,
    )
    workspace = Path(args.workspace)
    if (workspace / "competition.json").is_file():
        result = validate_v2_workspace(workspace)
        errors = result["errors"]
        warnings = result["warnings"]
    else:
        errors, warnings = validate_workspace(workspace)
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("OK" if result["ok"] else "FAILED")
        for warning in warnings:
            print(f"warning: {warning}")
        for error in errors:
            print(f"error: {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

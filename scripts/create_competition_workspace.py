#!/usr/bin/env python3
"""Create an isolated Kaggle competition workspace."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path


DIRS = [
    "configs",
    "data",
    "flags",
    "notebooks",
    "reports",
    "runs",
    "submissions",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_if_missing(path: Path, text: str) -> None:
    if not path.exists():
        path.write_text(text, encoding="utf-8", newline="\n")


def build_state(slug: str, metric: str, direction: str) -> str:
    return f"""# Competition State

- Competition slug: {slug}
- Metric: {metric}
- Direction: {direction}
- Current stage: bootstrap
- Best CV score:
- Best LB score:
- Active run:
- Next action: build competition profile
- Blockers:
- Updated at: {utc_now()}
"""


def build_ideas() -> str:
    return """# Ideas Backlog

- [ ] id: idea-001
  priority: high
  hypothesis: Build a simple trusted baseline before optimizing.
  validation: Baseline trains or infers end-to-end and produces a valid local score or submission-format artifact.
  failure_mode: The score is unusable because CV, metric, or submission format is wrong.
"""


def build_gate_report() -> str:
    return """{
  "candidate_id": "",
  "automated_gates": {
    "metric_gate": false,
    "format_gate": false,
    "provenance_gate": false,
    "compliance_gate": false,
    "diversity_gate": false
  },
  "human_approval": {
    "approved": false,
    "approver": "",
    "approved_at": "",
    "request_id": ""
  }
}
"""


def create_workspace(root: Path, slug: str, metric: str, direction: str) -> Path:
    workspace = root / slug
    workspace.mkdir(parents=True, exist_ok=True)
    for name in DIRS:
        directory = workspace / name
        directory.mkdir(exist_ok=True)
        write_if_missing(directory / ".gitkeep", "")

    write_if_missing(workspace / "STATE.md", build_state(slug, metric, direction))
    write_if_missing(workspace / "ideas_backlog.md", build_ideas())
    write_if_missing(workspace / "experiment_ledger.jsonl", "")
    write_if_missing(workspace / "reports" / "submission_gate.json", build_gate_report())
    return workspace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="workspaces", help="Parent directory for competition workspaces.")
    parser.add_argument("--slug", required=True, help="Kaggle competition slug, for example birdclef-2026.")
    parser.add_argument("--metric", default="primary_metric", help="Primary competition metric name.")
    parser.add_argument("--direction", choices=["higher", "lower"], default="higher", help="Whether larger metric values are better.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = create_workspace(Path(args.root), args.slug, args.metric, args.direction)
    print(f"created: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

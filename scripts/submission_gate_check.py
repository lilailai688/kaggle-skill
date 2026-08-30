#!/usr/bin/env python3
"""Check whether a Kaggle submission gate report is ready for submission."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kaggle_core.decisions import check_submission_gate as check_v2_submission_gate


REQUIRED_GATES = [
    "metric_gate",
    "format_gate",
    "provenance_gate",
    "compliance_gate",
    "diversity_gate",
]


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("gate report must be a JSON object")
    return data


def check_report(report: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    candidate_id = str(report.get("candidate_id", "")).strip()
    if not candidate_id:
        errors.append("missing candidate_id")

    gates = report.get("automated_gates")
    if not isinstance(gates, dict):
        errors.append("missing automated_gates object")
        gates = {}

    for gate in REQUIRED_GATES:
        if gates.get(gate) is not True:
            errors.append(f"automated gate not passed: {gate}")

    approval = report.get("human_approval")
    if not isinstance(approval, dict):
        errors.append("missing human_approval object")
        approval = {}

    if approval.get("approved") is not True:
        errors.append("human approval is missing or false")
    for field in ["approver", "approved_at", "request_id"]:
        if not str(approval.get(field, "")).strip():
            errors.append(f"human approval missing field: {field}")

    compliance_notes = report.get("compliance_notes")
    if compliance_notes is None:
        warnings.append("no compliance_notes field")

    return {
        "ready": not errors,
        "candidate_id": candidate_id,
        "errors": errors,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="Path to submission gate JSON report.")
    parser.add_argument("--workspace", help="V2 workspace root when checking a V2 report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("warning: submission_gate_check.py is deprecated; use kaggle_ops.py gate", file=sys.stderr)
    try:
        report_path = Path(args.report)
        report = load_report(report_path)
        if report.get("schema_version") == 2:
            if not args.workspace:
                raise ValueError("--workspace is required for a V2 gate report")
            result = check_v2_submission_gate(Path(args.workspace), report_path)
            result["ready"] = result["ready_for_human_submission"]
        else:
            result = check_report(report)
    except Exception as exc:  # noqa: BLE001 - CLI should print a clean failure.
        result = {"ready": False, "candidate_id": "", "errors": [str(exc)], "warnings": []}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Detect whether recent Kaggle experiments indicate a dry direction."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"completed", "valid", "accepted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_ledger(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            records.append(item)
    return records


def extract_score(record: dict[str, Any], metric: str) -> float | None:
    candidates = [
        record.get(metric),
        record.get("primary_metric"),
        record.get("cv_score"),
        record.get("metric_value"),
    ]
    metrics = record.get("metrics")
    if isinstance(metrics, dict):
        candidates.extend([metrics.get(metric), metrics.get("primary"), metrics.get("cv")])
    for value in candidates:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    return None


def has_todo_ideas(path: Path | None) -> bool | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8").lower()
    return "- [ ]" in text or "status: todo" in text


def compute_improvements(scores: list[float], direction: str) -> list[float]:
    improvements: list[float] = []
    best: float | None = None
    for score in scores:
        if best is None:
            improvements.append(float("inf"))
            best = score
            continue
        improvement = score - best if direction == "higher" else best - score
        improvements.append(improvement)
        if improvement > 0:
            best = score
    return improvements


def dryness_result(
    records: list[dict[str, Any]],
    metric: str,
    direction: str,
    patience: int,
    eps: float,
    ideas_path: Path | None,
) -> dict[str, Any]:
    scored: list[dict[str, Any]] = []
    for record in records:
        status = str(record.get("status", "")).lower()
        if status and status not in VALID_STATUSES:
            continue
        score = extract_score(record, metric)
        if score is None:
            continue
        scored.append({"run_id": record.get("run_id", ""), "score": score})

    scores = [item["score"] for item in scored]
    improvements = compute_improvements(scores, direction)
    finite_recent = [x for x in improvements[-patience:] if x != float("inf")]
    low_gain = len(finite_recent) >= patience and all(x < eps for x in finite_recent)
    todo = has_todo_ideas(ideas_path)
    backlog_exhausted = todo is False

    reasons: list[str] = []
    if low_gain:
        reasons.append(f"last {patience} valid experiments improved best score by < {eps}")
    if backlog_exhausted:
        reasons.append("ideas backlog has no todo ideas")

    return {
        "dry": bool(reasons),
        "reasons": reasons,
        "valid_experiments": len(scored),
        "recent_scores": scores[-patience:],
        "recent_improvements": finite_recent,
        "has_todo_ideas": todo,
        "checked_at": utc_now(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", help="Competition workspace directory.")
    parser.add_argument("--metric", default="primary_metric", help="Metric field or metric name to read.")
    parser.add_argument("--direction", choices=["higher", "lower"], default="higher")
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--eps", type=float, default=0.001)
    parser.add_argument("--write-flag", action="store_true", help="Write flags/EXPERIMENT_DRY.flag when dry.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace)
    result = dryness_result(
        load_ledger(workspace / "experiment_ledger.jsonl"),
        args.metric,
        args.direction,
        args.patience,
        args.eps,
        workspace / "ideas_backlog.md",
    )
    if args.write_flag and result["dry"]:
        flags = workspace / "flags"
        flags.mkdir(exist_ok=True)
        (flags / "EXPERIMENT_DRY.flag").write_text(json.dumps(result, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["dry"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

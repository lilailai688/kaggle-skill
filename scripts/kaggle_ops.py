#!/usr/bin/env python3
"""Platform-neutral competition operations for Kaggle Skill V2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kaggle_core.common import OpsError, print_json, read_json
from kaggle_core.decisions import check_dryness, check_submission_gate, promote_candidate, record_metric
from kaggle_core.release import build_postmortem, build_release, verify_release
from kaggle_core.runner import run_experiment
from kaggle_core.workspace import (
    bootstrap_workspace,
    create_or_update_idea,
    ingest_data,
    materialize_state,
    migrate_v1_workspace,
    validate_workspace,
)


def _workspace(value: str) -> Path:
    return Path(value).resolve()


def _invariant(value: str) -> tuple[Path, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("invariant must be OUTPUT=REFERENCE")
    output, reference = value.split("=", 1)
    if not output or not reference:
        raise argparse.ArgumentTypeError("invariant must be OUTPUT=REFERENCE")
    return Path(output), Path(reference)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    bootstrap = subparsers.add_parser("bootstrap", help="Create a V2 competition workspace.")
    bootstrap.add_argument("workspace")
    bootstrap.add_argument("--slug", required=True)
    bootstrap.add_argument("--platform", default="manual")
    bootstrap.add_argument("--problem-type", default="custom")
    bootstrap.add_argument("--track", default="primary")
    bootstrap.add_argument("--phase", default="active")
    bootstrap.add_argument("--metric", default="primary_metric")
    bootstrap.add_argument("--direction", choices=["higher", "lower"], default="higher")
    bootstrap.add_argument("--eps", type=float, default=0.001)
    bootstrap.add_argument("--submission-limit", type=int, default=5)
    bootstrap.add_argument("--deadline")
    bootstrap.add_argument("--submission-mode", default="file")

    migrate = subparsers.add_parser("migrate", help="Copy V1 metadata into a new V2 workspace.")
    migrate.add_argument("source")
    migrate.add_argument("destination")
    migrate.add_argument("--slug")
    migrate.add_argument("--platform", default="manual")
    migrate.add_argument("--mapping", type=Path)

    ingest = subparsers.add_parser("ingest", help="Validate and fingerprint a read-only data source.")
    ingest.add_argument("workspace")
    ingest.add_argument("source")

    idea = subparsers.add_parser("idea", help="Create or update an evidence-backed experiment idea.")
    idea.add_argument("workspace")
    idea.add_argument("--id", required=True)
    idea.add_argument("--track", required=True)
    idea.add_argument("--phase", required=True)
    idea.add_argument("--family", default="general")
    idea.add_argument("--priority", choices=["high", "medium", "low"], default="medium")
    idea.add_argument("--status", choices=["open", "done", "dropped"], default="open")
    idea.add_argument("--hypothesis", required=True)
    idea.add_argument("--validation", default="")
    idea.add_argument("--failure-mode", default="")

    run = subparsers.add_parser("run", help="Execute one isolated, auditable experiment unit.")
    run.add_argument("workspace")
    run.add_argument("--track", required=True)
    run.add_argument("--phase", required=True)
    run.add_argument("--data-snapshot", required=True)
    run.add_argument("--family", required=True)
    run.add_argument("--idea-id", required=True)
    run.add_argument("--command", required=True)
    run.add_argument("--run-id")
    run.add_argument("--source-root", type=Path)
    run.add_argument("--config", type=Path)
    run.add_argument("--container-image")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--parent-run-id")
    run.add_argument("--artifact", action="append", type=Path, default=[])
    run.add_argument("--invariant", action="append", type=_invariant, default=[])
    run.add_argument("--resume", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate V2 workspace structure and history.")
    validate.add_argument("workspace")
    validate.add_argument("--deep", action="store_true")

    status = subparsers.add_parser("status", help="Materialize and print the current state.")
    status.add_argument("workspace")

    dryness = subparsers.add_parser("dryness", help="Check one comparable experiment lane for dryness.")
    dryness.add_argument("workspace")
    dryness.add_argument("--track", required=True)
    dryness.add_argument("--phase", required=True)
    dryness.add_argument("--data-snapshot", required=True)
    dryness.add_argument("--family", required=True)
    dryness.add_argument("--metric", required=True)
    dryness.add_argument("--metric-source", required=True)
    dryness.add_argument("--comparable-group", required=True)
    dryness.add_argument("--patience", type=int)
    dryness.add_argument("--eps", type=float)
    dryness.add_argument("--failure-patience", type=int)
    dryness.add_argument("--no-write-flag", action="store_true")

    gate = subparsers.add_parser("gate", help="Validate a probe, candidate, or final submission report.")
    gate.add_argument("workspace")
    gate.add_argument("report")

    feedback = subparsers.add_parser("feedback", help="Record a typed local or leaderboard metric.")
    feedback.add_argument("workspace")
    feedback.add_argument("--run-id", required=True)
    feedback.add_argument("--candidate-id")
    feedback.add_argument("--baseline-candidate-id")
    feedback.add_argument("--metric-name", required=True)
    feedback.add_argument("--value", required=True, type=float)
    feedback.add_argument("--direction", choices=["higher", "lower"], required=True)
    feedback.add_argument(
        "--source",
        choices=["local_cv", "local_proxy", "public_lb", "private_lb", "final_result"],
        required=True,
    )
    feedback.add_argument("--split", required=True)
    feedback.add_argument("--scope", required=True)
    feedback.add_argument("--comparable-group", required=True)

    promote = subparsers.add_parser("promote", help="Explicitly promote a gated candidate.")
    promote.add_argument("workspace")
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--reason", required=True)

    release = subparsers.add_parser("release", help="Build or verify a deterministic release archive.")
    release_subparsers = release.add_subparsers(dest="release_action", required=True)
    release_build = release_subparsers.add_parser("build")
    release_build.add_argument("workspace")
    release_build.add_argument("--source", required=True, type=Path)
    release_build.add_argument("--output", type=Path)
    release_build.add_argument("--include", action="append")
    release_verify = release_subparsers.add_parser("verify")
    release_verify.add_argument("workspace")
    release_verify.add_argument("--archive", required=True, type=Path)
    release_verify.add_argument("--manifest", type=Path)

    postmortem = subparsers.add_parser("postmortem", help="Generate a generic competition postmortem.")
    postmortem.add_argument("workspace")
    postmortem.add_argument("--final-result", default="")
    postmortem.add_argument("--final-rank", default="")
    postmortem.add_argument("--reusable", action="append", default=[])
    return parser


def dispatch(args: argparse.Namespace) -> tuple[object, int]:
    command = args.command_name
    if command == "bootstrap":
        return (
            bootstrap_workspace(
                _workspace(args.workspace),
                slug=args.slug,
                platform=args.platform,
                problem_type=args.problem_type,
                track_id=args.track,
                phase_id=args.phase,
                metric=args.metric,
                direction=args.direction,
                eps=args.eps,
                submission_limit=args.submission_limit,
                deadline=args.deadline,
                submission_mode=args.submission_mode,
            ),
            0,
        )
    if command == "migrate":
        return (
            migrate_v1_workspace(
                Path(args.source),
                Path(args.destination),
                slug=args.slug,
                platform=args.platform,
                mapping_path=args.mapping,
            ),
            0,
        )
    if command == "ingest":
        return ingest_data(_workspace(args.workspace), Path(args.source)), 0
    if command == "idea":
        return (
            create_or_update_idea(
                _workspace(args.workspace),
                idea_id=args.id,
                track_id=args.track,
                phase_id=args.phase,
                experiment_family=args.family,
                priority=args.priority,
                status=args.status,
                hypothesis=args.hypothesis,
                validation=args.validation,
                failure_mode=args.failure_mode,
            ),
            0,
        )
    if command == "run":
        return (
            run_experiment(
                _workspace(args.workspace),
                track_id=args.track,
                phase_id=args.phase,
                data_snapshot_id=args.data_snapshot,
                experiment_family=args.family,
                idea_id=args.idea_id,
                command=args.command,
                run_id=args.run_id,
                source_root=args.source_root,
                config_path=args.config,
                container_image=args.container_image,
                seed=args.seed,
                parent_run_id=args.parent_run_id,
                artifact_paths=args.artifact,
                invariants=args.invariant,
                resume=args.resume,
            ),
            0,
        )
    if command == "validate":
        result = validate_workspace(_workspace(args.workspace), deep=args.deep)
        return result, 0 if result["ok"] else 1
    if command == "status":
        return materialize_state(_workspace(args.workspace)), 0
    if command == "dryness":
        result = check_dryness(
            _workspace(args.workspace),
            track_id=args.track,
            phase_id=args.phase,
            data_snapshot_id=args.data_snapshot,
            experiment_family=args.family,
            metric_name=args.metric,
            metric_source=args.metric_source,
            comparable_group=args.comparable_group,
            patience=args.patience,
            eps=args.eps,
            failure_patience=args.failure_patience,
            write_flag=not args.no_write_flag,
        )
        return result, 2 if result["dry"] else 0
    if command == "gate":
        result = check_submission_gate(_workspace(args.workspace), Path(args.report))
        return result, 0 if result["ready_for_human_submission"] else 2
    if command == "feedback":
        metric = {
            "name": args.metric_name,
            "value": args.value,
            "direction": args.direction,
            "source": args.source,
            "split": args.split,
            "scope": args.scope,
            "comparable_group": args.comparable_group,
        }
        return (
            record_metric(
                _workspace(args.workspace),
                run_id=args.run_id,
                metric=metric,
                candidate_id=args.candidate_id,
                baseline_candidate_id=args.baseline_candidate_id,
            ),
            0,
        )
    if command == "promote":
        return (
            promote_candidate(
                _workspace(args.workspace), candidate_id=args.candidate_id, reason=args.reason
            ),
            0,
        )
    if command == "release":
        if args.release_action == "build":
            return (
                build_release(
                    _workspace(args.workspace),
                    source=args.source,
                    output=args.output,
                    includes=args.include,
                ),
                0,
            )
        expected = None
        expected_archive_sha256 = None
        expected_member_set_sha256 = None
        if args.manifest:
            manifest = read_json(args.manifest)
            seal_path = args.manifest.with_suffix(args.manifest.suffix + ".seal.json")
            if not seal_path.is_file():
                raise OpsError(f"release manifest seal is missing: {seal_path}")
            seal = read_json(seal_path)
            from kaggle_core.common import sha256_file

            if seal.get("sha256") != sha256_file(args.manifest):
                raise OpsError("release manifest seal mismatch")
            expected = manifest.get("members")
            expected_archive_sha256 = manifest.get("archive_sha256")
            expected_member_set_sha256 = manifest.get("member_set_sha256")
        result = verify_release(
            _workspace(args.workspace),
            archive_path=args.archive,
            expected_members=expected,
            expected_archive_sha256=expected_archive_sha256,
            expected_member_set_sha256=expected_member_set_sha256,
        )
        return result, 0 if result["passed"] else 1
    if command == "postmortem":
        return (
            build_postmortem(
                _workspace(args.workspace),
                final_result=args.final_result,
                final_rank=args.final_rank,
                reusable_components=args.reusable,
            ),
            0,
        )
    raise OpsError(f"unknown command: {command}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result, exit_code = dispatch(args)
        print_json(result)
        return exit_code
    except OpsError as exc:
        print_json({"ok": False, "error": str(exc)})
        return 1
    except (OSError, ValueError) as exc:
        print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    except KeyboardInterrupt:
        print_json({"ok": False, "error": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

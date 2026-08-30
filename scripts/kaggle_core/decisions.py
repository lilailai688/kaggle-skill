from __future__ import annotations

import csv
import fnmatch
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    SCHEMA_VERSION,
    OpsError,
    ensure_identifier,
    exclusive_lock,
    event,
    parse_utc,
    read_json,
    sha256_file,
    utc_now,
    write_json_atomic,
)
from .schema import (
    METRIC_SOURCES,
    SUBMISSION_INTENTS,
    append_event_checked,
    get_phase,
    get_track,
    lane_key,
    load_and_validate_competition,
    load_events,
    validate_metric,
)
from .workspace import _run_validator_hook, materialize_state


def _validate_output_table(path: Path, check: dict[str, Any]) -> list[str]:
    pattern = str(check.get("glob", path.name))
    if not (fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(path.as_posix(), pattern)):
        return []
    errors: list[str] = []
    delimiter = "\t" if str(check.get("format", "csv")).lower() == "tsv" else str(check.get("delimiter", ","))
    encoding = str(check.get("encoding", "utf-8-sig"))
    required_columns = [str(item) for item in check.get("required_columns", [])]
    exact_columns = check.get("exact_columns")
    primary_key = [str(item) for item in check.get("primary_key", [])]
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            columns = reader.fieldnames or []
            missing = [column for column in required_columns if column not in columns]
            if missing:
                errors.append(f"submission table missing columns: {missing}")
            if isinstance(exact_columns, list) and columns != [str(item) for item in exact_columns]:
                errors.append(
                    f"submission table columns differ: expected {exact_columns}, observed {columns}"
                )
            seen: set[tuple[str, ...]] = set()
            duplicates = 0
            missing_keys = 0
            rows = 0
            for row in reader:
                rows += 1
                if primary_key:
                    key = tuple(str(row.get(column, "")) for column in primary_key)
                    missing_keys += any(not value for value in key)
                    duplicates += key in seen
                    seen.add(key)
            if rows < int(check.get("min_rows", 0)):
                errors.append(f"submission table row count {rows} is below {check.get('min_rows')}")
            max_rows = check.get("max_rows")
            if isinstance(max_rows, int) and rows > max_rows:
                errors.append(f"submission table row count {rows} exceeds {max_rows}")
            if duplicates:
                errors.append(f"submission table duplicate primary keys: {duplicates}")
            if missing_keys:
                errors.append(f"submission table missing primary keys: {missing_keys}")
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"submission table cannot be parsed: {exc}")
    return errors


def _safe_flag_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned[:50] or "unknown"


def _open_high_priority_idea(
    events: list[dict[str, Any]], track_id: str, phase_id: str, experiment_family: str
) -> bool:
    ideas: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("event_type") not in {"idea_created", "idea_updated"}:
            continue
        if item.get("track_id") != track_id or item.get("phase_id") != phase_id:
            continue
        if item.get("experiment_family") not in {experiment_family, "general", None}:
            continue
        ideas[str(item.get("idea_id"))] = item.get("payload", {})
    return any(item.get("priority") == "high" and item.get("status") == "open" for item in ideas.values())


def _run_statuses(events: list[dict[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for item in events:
        if item.get("event_type") in {"run_completed", "run_failed"} and item.get("run_id"):
            statuses[str(item["run_id"])] = str(item.get("payload", {}).get("status", ""))
    return statuses


def _metric_gain(scores: list[float], direction: str) -> float:
    if not scores:
        return math.inf
    first = scores[0]
    best = max(scores) if direction == "higher" else min(scores)
    return best - first if direction == "higher" else first - best


def check_dryness(
    workspace: Path,
    *,
    track_id: str,
    phase_id: str,
    data_snapshot_id: str,
    experiment_family: str,
    metric_name: str,
    metric_source: str,
    comparable_group: str,
    patience: int | None = None,
    eps: float | None = None,
    failure_patience: int | None = None,
    write_flag: bool = True,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    competition = load_and_validate_competition(workspace)
    track = get_track(competition, track_id)
    get_phase(competition, phase_id)
    if metric_source not in METRIC_SOURCES:
        raise OpsError(f"invalid metric source: {metric_source}")
    definition = next((item for item in track["metrics"] if item.get("name") == metric_name), None)
    if definition is None:
        raise OpsError(f"metric is not registered for track {track_id}: {metric_name}")
    direction = str(definition["direction"])
    eps = float(definition["eps"] if eps is None else eps)
    patience = int(patience or competition["dryness"]["patience"])
    failure_patience = int(failure_patience or competition["dryness"]["failure_patience"])
    events = load_events(workspace)
    statuses = _run_statuses(events)

    metrics_by_run: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("event_type") not in {"metric_recorded", "leaderboard_feedback_recorded"}:
            continue
        if (
            item.get("track_id") != track_id
            or item.get("phase_id") != phase_id
            or item.get("data_snapshot_id") != data_snapshot_id
            or item.get("experiment_family") != experiment_family
        ):
            continue
        metric = item.get("payload", {}).get("metric")
        if not isinstance(metric, dict):
            continue
        if (
            metric.get("name") == metric_name
            and metric.get("source") == metric_source
            and metric.get("comparable_group") == comparable_group
            and metric.get("direction") == direction
            and statuses.get(str(item.get("run_id"))) == "completed"
        ):
            run_id = str(item.get("run_id"))
            metrics_by_run.pop(run_id, None)
            metrics_by_run[run_id] = {
                "run_id": run_id,
                "metric": metric,
                "occurred_at": item.get("occurred_at"),
            }
    metrics = list(metrics_by_run.values())
    recent = metrics[-patience:]
    scores = [float(item["metric"]["value"]) for item in recent]
    gain = _metric_gain(scores, direction)
    low_gain = len(recent) >= patience and gain <= eps + 1e-12
    has_high_priority_idea = _open_high_priority_idea(events, track_id, phase_id, experiment_family)

    terminal_outcomes = [
        item
        for item in events
        if item.get("event_type") in {"run_completed", "run_failed"}
        and item.get("track_id") == track_id
        and item.get("phase_id") == phase_id
        and item.get("data_snapshot_id") == data_snapshot_id
        and item.get("experiment_family") == experiment_family
    ]
    recent_outcomes = terminal_outcomes[-failure_patience:]
    recent_failures = (
        recent_outcomes
        if len(recent_outcomes) >= failure_patience
        and all(item.get("event_type") == "run_failed" for item in recent_outcomes)
        else []
    )
    failure_reasons = [str(item.get("payload", {}).get("failure_reason", "")).strip() for item in recent_failures]
    repeated_failure = (
        len(recent_failures) >= failure_patience
        and bool(failure_reasons[0])
        and len(set(failure_reasons)) == 1
    )
    reasons: list[str] = []
    if low_gain and not has_high_priority_idea:
        reasons.append(
            f"last {patience} comparable completed runs improved the rolling best by {gain:.12g} <= {eps:.12g}"
        )
        reasons.append("no open high-priority idea exists for this track and experiment family")
    if repeated_failure:
        reasons.append(f"same failure reason repeated {failure_patience} times: {failure_reasons[0]}")
    dry = bool(reasons)
    result = {
        "schema_version": SCHEMA_VERSION,
        "dry": dry,
        "track_id": track_id,
        "phase_id": phase_id,
        "data_snapshot_id": data_snapshot_id,
        "experiment_family": experiment_family,
        "metric_name": metric_name,
        "metric_source": metric_source,
        "comparable_group": comparable_group,
        "direction": direction,
        "patience": patience,
        "eps": eps,
        "comparable_runs": len(metrics),
        "recent_run_ids": [item["run_id"] for item in recent],
        "recent_scores": scores,
        "rolling_best_gain": gain if math.isfinite(gain) else None,
        "has_open_high_priority_idea": has_high_priority_idea,
        "repeated_failure": repeated_failure,
        "reasons": reasons,
        "checked_at": utc_now(),
    }
    flag_name = "DRY_" + "_".join(
        _safe_flag_part(item)
        for item in [track_id, phase_id, data_snapshot_id, experiment_family, metric_source, comparable_group]
    ) + ".flag"
    flag_path = workspace / "flags" / flag_name
    if write_flag:
        if dry:
            write_json_atomic(flag_path, result)
        else:
            flag_path.unlink(missing_ok=True)
    materialize_state(workspace)
    return result


def _zip_members(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    members: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or "" in member.parts
                    or (member.parts and member.parts[0].endswith(":"))
                ):
                    errors.append(f"unsafe ZIP member: {info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) == 0o120000:
                    errors.append(f"ZIP symlink is not allowed: {info.filename}")
                if not info.is_dir():
                    members.append(info.filename)
            duplicates = sorted(name for name, count in Counter(members).items() if count > 1)
            errors.extend(f"duplicate ZIP member: {name}" for name in duplicates)
            bad = archive.testzip()
            if bad:
                errors.append(f"ZIP CRC failure: {bad}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"invalid ZIP: {exc}")
    return sorted(members), errors


def _approval_errors(
    approval: Any,
    *,
    candidate_id: str,
    artifact_sha256: str,
    intent: str,
) -> list[str]:
    if not isinstance(approval, dict):
        return ["human_approval must be an object"]
    required = {
        "approved",
        "candidate_id",
        "artifact_sha256",
        "intent",
        "approver",
        "request_id",
        "approved_at",
        "expires_at",
    }
    errors = [f"human_approval missing field: {key}" for key in sorted(required - set(approval))]
    if approval.get("approved") is not True:
        errors.append("human approval is missing or false")
    if approval.get("candidate_id") != candidate_id:
        errors.append("human approval candidate_id mismatch")
    if approval.get("artifact_sha256") != artifact_sha256:
        errors.append("human approval artifact_sha256 mismatch")
    if approval.get("intent") != intent:
        errors.append("human approval intent mismatch")
    approver = str(approval.get("approver", "")).strip()
    if not approver:
        errors.append("human approval approver is empty")
    if approver.lower() in {"agent", "codex", "system", "assistant", "ai"}:
        errors.append("the system cannot self-approve")
    if not str(approval.get("request_id", "")).strip():
        errors.append("human approval request_id is empty")
    try:
        approved_at = parse_utc(str(approval.get("approved_at", "")))
        expires_at = parse_utc(str(approval.get("expires_at", "")))
        now = datetime.now(timezone.utc)
        if expires_at <= approved_at:
            errors.append("human approval expires_at must be after approved_at")
        if expires_at <= now:
            errors.append("human approval is expired")
        if approved_at > now:
            errors.append("human approval approved_at is in the future")
    except (TypeError, ValueError):
        errors.append("human approval timestamps must be ISO-8601 values")
    return errors


def _gate_value(report: dict[str, Any], name: str) -> tuple[bool, str]:
    gates = report.get("automated_gates")
    if not isinstance(gates, dict):
        return False, "automated_gates must be an object"
    value = gates.get(name)
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, dict):
        return value.get("passed") is True, str(value.get("evidence", ""))
    return False, f"missing automated gate: {name}"


def _candidate_event(events: list[dict[str, Any]], candidate_id: str) -> dict[str, Any] | None:
    for item in events:
        if item.get("event_type") == "candidate_prepared" and item.get("payload", {}).get("candidate_id") == candidate_id:
            return item
    return None


def check_submission_gate(workspace: Path, report_path: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    competition = load_and_validate_competition(workspace)
    report = read_json(report_path.resolve())
    required = {
        "schema_version",
        "candidate_id",
        "track_id",
        "phase_id",
        "data_snapshot_id",
        "experiment_family",
        "run_id",
        "intent",
        "artifact_path",
        "artifact_sha256",
        "ensemble",
        "automated_gates",
        "human_approval",
    }
    errors = [f"gate report missing field: {key}" for key in sorted(required - set(report))]
    allowed = required | {
        "probe",
        "controlled_difference",
        "release_manifest",
        "notes",
        "extensions",
    }
    errors.extend(f"gate report unknown field: {key}" for key in sorted(set(report) - allowed))
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"gate report schema_version must be {SCHEMA_VERSION}")
    if not str(report.get("candidate_id", "")).strip():
        errors.append("candidate_id must be a non-empty string")
    if not str(report.get("experiment_family", "")).strip():
        errors.append("experiment_family must be a non-empty string")
    if not isinstance(report.get("ensemble"), bool):
        errors.append("ensemble must be boolean")
    intent = str(report.get("intent", ""))
    if intent not in SUBMISSION_INTENTS:
        errors.append(f"invalid submission intent: {intent}")
    track_id = str(report.get("track_id", ""))
    phase_id = str(report.get("phase_id", ""))
    try:
        get_track(competition, track_id)
        phase = get_phase(competition, phase_id)
    except OpsError as exc:
        errors.append(str(exc))
        phase = {"submission_limit": 0}
    snapshot_id = str(report.get("data_snapshot_id", ""))
    try:
        ensure_identifier(snapshot_id, "data_snapshot_id")
    except OpsError as exc:
        errors.append(str(exc))
    if not (workspace / "data" / "manifests" / f"{snapshot_id}.json").is_file():
        errors.append(f"unknown data_snapshot_id: {snapshot_id}")

    artifact_path = Path(str(report.get("artifact_path", "")))
    if not artifact_path.is_absolute():
        artifact_path = (workspace / artifact_path).resolve()
    observed_hash = sha256_file(artifact_path) if artifact_path.is_file() else ""
    if not artifact_path.is_file():
        errors.append(f"candidate artifact does not exist: {artifact_path}")
    if observed_hash != report.get("artifact_sha256"):
        errors.append("candidate artifact SHA256 does not match the gate report")

    events = load_events(workspace)
    run_id = str(report.get("run_id", ""))
    try:
        ensure_identifier(run_id, "run_id")
    except OpsError as exc:
        errors.append(str(exc))
    run_manifest_path = workspace / "runs" / run_id / "run-manifest.json"
    provenance_passed = False
    run_manifest: dict[str, Any] = {}
    if run_manifest_path.is_file():
        run_manifest = read_json(run_manifest_path)
        context_matches = True
        expected_context = {
            "track_id": track_id,
            "phase_id": phase_id,
            "data_snapshot_id": snapshot_id,
            "experiment_family": str(report.get("experiment_family", "")),
            "run_id": run_id,
        }
        for field, expected in expected_context.items():
            if run_manifest.get(field) != expected:
                errors.append(f"run manifest {field} mismatch: expected {expected}")
                context_matches = False
        identity = run_manifest.get("identity", {})
        current_identity_files = {
            "competition_sha256": workspace / "competition.json",
            "rules_snapshot_sha256": workspace / "rules" / "rules.json",
            "data_manifest_sha256": workspace / "data" / "manifests" / f"{snapshot_id}.json",
        }
        for field, path in current_identity_files.items():
            observed_identity_hash = sha256_file(path) if path.is_file() else None
            if identity.get(field) != observed_identity_hash:
                errors.append(f"run identity is stale for {field}")
                context_matches = False
        seal_path = run_manifest_path.with_name("run-manifest.sha256.json")
        manifest_sealed = False
        if not seal_path.is_file():
            errors.append("run manifest seal is missing")
        else:
            seal = read_json(seal_path)
            manifest_sealed = seal.get("sha256") == sha256_file(run_manifest_path)
            if not manifest_sealed:
                errors.append("run manifest seal mismatch")
        artifact_entry = next(
            (item for item in run_manifest.get("artifacts", []) if item.get("sha256") == observed_hash),
            None,
        )
        stored_artifact_valid = False
        if artifact_entry is not None:
            stored_artifact = Path(str(artifact_entry.get("store_path", "")))
            stored_artifact_valid = (
                stored_artifact.is_file() and sha256_file(stored_artifact) == observed_hash
            )
        if not stored_artifact_valid:
            errors.append("content-addressed artifact is missing or has a different SHA256")
        planned = next(
            (
                item
                for item in events
                if item.get("event_type") == "run_planned" and item.get("run_id") == run_id
            ),
            None,
        )
        completed_event = next(
            (
                item
                for item in reversed(events)
                if item.get("event_type") == "run_completed"
                and item.get("run_id") == run_id
                and item.get("payload", {}).get("status") == "completed"
            ),
            None,
        )
        ledger_context_valid = planned is not None and completed_event is not None
        for item in [planned, completed_event]:
            if item is None:
                continue
            for field, expected in expected_context.items():
                if field == "run_id":
                    continue
                if item.get(field) != expected:
                    ledger_context_valid = False
                    errors.append(f"ledger {field} differs from candidate report")
        ledger_artifact_valid = bool(
            completed_event
            and any(
                item.get("sha256") == observed_hash
                for item in completed_event.get("payload", {}).get("artifacts", [])
            )
        )
        if not ledger_artifact_valid:
            errors.append("completed run event does not bind the candidate artifact SHA256")
        provenance_passed = (
            run_manifest.get("status") == "completed"
            and context_matches
            and manifest_sealed
            and stored_artifact_valid
            and ledger_context_valid
            and ledger_artifact_valid
        )
    if not provenance_passed:
        errors.append("provenance gate failed: artifact is not recorded by a completed run")

    output_contract_error_start = len(errors)
    output_contract = competition["output_contract"]
    run_artifact_names: list[str] = []
    run_dir = workspace / "runs" / run_id
    for item in run_manifest.get("artifacts", []):
        source_path = Path(str(item.get("source_path", "")))
        try:
            relative = source_path.resolve().relative_to(run_dir.resolve()).as_posix()
        except ValueError:
            relative = source_path.name
        run_artifact_names.extend([relative, source_path.name])
    for pattern in output_contract.get("required_artifacts", []):
        if not any(fnmatch.fnmatch(name, str(pattern)) for name in run_artifact_names):
            errors.append(f"required run artifact is missing: {pattern}")
    for check in output_contract.get("tabular_checks", []):
        if not isinstance(check, dict):
            errors.append("output tabular check must be an object")
            continue
        pattern = str(check.get("glob", artifact_path.name))
        matched = fnmatch.fnmatch(artifact_path.name, pattern) or fnmatch.fnmatch(
            artifact_path.as_posix(), pattern
        )
        if not matched and check.get("required", True):
            errors.append(f"output tabular check does not match candidate artifact: {pattern}")
        elif matched and artifact_path.is_file():
            errors.extend(_validate_output_table(artifact_path, check))
    for hook in output_contract.get("validator_hooks", []):
        if not isinstance(hook, dict):
            errors.append("output validator hook must be an object")
            continue
        try:
            hook_result = _run_validator_hook(workspace, artifact_path.parent, hook, output_contract)
            errors.extend(str(item) for item in hook_result.get("errors", []))
        except Exception as exc:  # noqa: BLE001 - hook failures are gate failures.
            errors.append(f"output validator hook failed: {exc}")

    if artifact_path.suffix.lower() == ".zip" and artifact_path.is_file():
        observed_members, zip_errors = _zip_members(artifact_path)
        errors.extend(zip_errors)
        expected_members = sorted(competition["output_contract"].get("submission_zip_members", []))
        if expected_members and observed_members != expected_members:
            errors.append(
                f"submission ZIP members mismatch: expected {expected_members}, observed {observed_members}"
            )

    common_gates = [
        "format",
        "compliance",
        "artifact_integrity",
        "leakage",
        "temporal_integrity",
        "training_inference_boundary",
        "prohibited_shortcuts",
        "artifact_freshness",
    ]
    for name in common_gates:
        passed, note = _gate_value(report, name)
        if not passed:
            errors.append(note or f"automated gate not passed: {name}")
    unresolved_rule_fields = [
        field
        for field in ["external_data_policy", "network_policy", "model_license_policy"]
        if str(competition.get("rules", {}).get(field, "unknown")).strip().lower()
        in {"", "unknown", "unreviewed", "unset"}
    ]
    if unresolved_rule_fields:
        errors.append(
            "compliance checklist has unresolved rules: " + ", ".join(unresolved_rule_fields)
        )
    metric_passed, metric_note = _gate_value(report, "metric")
    if intent in {"candidate", "final"} and not metric_passed:
        errors.append(metric_note or "metric gate is required for candidate and final submissions")
    if report.get("ensemble") is True:
        diversity_passed, diversity_note = _gate_value(report, "diversity")
        if not diversity_passed:
            errors.append(diversity_note or "diversity gate is required for ensemble candidates")

    submitted_candidates = {
        str(item.get("payload", {}).get("candidate_id"))
        for item in events
        if item.get("event_type") == "leaderboard_feedback_recorded"
        and item.get("phase_id") == phase_id
        and item.get("payload", {}).get("candidate_id")
    }
    submissions_used = len(submitted_candidates)
    remaining_submissions = max(int(phase.get("submission_limit", 0)) - submissions_used, 0)
    if remaining_submissions <= 0:
        errors.append("submission gate rejected because no submission budget remains")
    if intent == "probe":
        probe = report.get("probe")
        if not isinstance(probe, dict):
            errors.append("probe intent requires a probe object")
        else:
            for field in ["information_hypothesis", "controlled_difference", "rollback_candidate_id"]:
                if not str(probe.get(field, "")).strip():
                    errors.append(f"probe is missing {field}")
    if intent == "final":
        if not isinstance(run_manifest.get("container"), dict) or not run_manifest.get(
            "container", {}
        ).get("image_id"):
            errors.append(
                "final intent requires a containerized run with a resolved image ID"
            )
        for name in ["cold_start", "release", "final_deliverable"]:
            passed, note = _gate_value(report, name)
            if not passed:
                errors.append(note or f"automated gate not passed: {name}")
        release_manifest_value = report.get("release_manifest")
        if not isinstance(release_manifest_value, str) or not release_manifest_value.strip():
            errors.append("final intent requires release_manifest")
        else:
            release_manifest_path = Path(release_manifest_value)
            if not release_manifest_path.is_absolute():
                release_manifest_path = (workspace / release_manifest_path).resolve()
            try:
                release_manifest_path.relative_to((workspace / "release").resolve())
            except ValueError:
                errors.append("release_manifest must be inside workspace/release")
            if not release_manifest_path.is_file():
                errors.append(f"release manifest does not exist: {release_manifest_path}")
            else:
                release_seal_path = release_manifest_path.with_suffix(
                    release_manifest_path.suffix + ".seal.json"
                )
                if (
                    not release_seal_path.is_file()
                    or read_json(release_seal_path).get("sha256")
                    != sha256_file(release_manifest_path)
                ):
                    errors.append("release manifest seal is missing or invalid")
                else:
                    release_manifest = read_json(release_manifest_path)
                    release_run_ids = {
                        str(item.get("run_id"))
                        for item in release_manifest.get("runtime_provenance", [])
                    }
                    if run_id not in release_run_ids:
                        errors.append(
                            "release runtime provenance does not include the final candidate run"
                        )
                    release_archive = Path(str(release_manifest.get("archive", "")))
                    if (
                        not release_archive.is_file()
                        or sha256_file(release_archive)
                        != release_manifest.get("archive_sha256")
                    ):
                        errors.append("release archive is missing or has a different SHA256")

    candidate_id = str(report.get("candidate_id", ""))
    approval_errors = _approval_errors(
        report.get("human_approval"),
        candidate_id=candidate_id,
        artifact_sha256=observed_hash,
        intent=intent,
    )
    errors.extend(approval_errors)
    ready = not errors
    result = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "ready_for_human_submission": ready,
        "intent": intent,
        "artifact_path": artifact_path.as_posix(),
        "artifact_sha256": observed_hash,
        "provenance_computed": provenance_passed,
        "output_contract_passed": artifact_path.is_file()
        and len(errors) == output_contract_error_start,
        "metric_gate_passed": metric_passed,
        "remaining_submissions": remaining_submissions,
        "errors": errors,
        "checked_at": utc_now(),
    }
    existing_candidate = _candidate_event(events, candidate_id)
    candidate_binding_valid = True
    if existing_candidate is not None:
        existing = existing_candidate.get("payload", {})
        immutable_checks = {
            "intent": intent,
            "artifact_sha256": observed_hash,
        }
        for field, observed in immutable_checks.items():
            if existing.get(field) != observed:
                errors.append(f"candidate_id is already bound to a different {field}")
                candidate_binding_valid = False
        event_checks = {
            "track_id": track_id,
            "phase_id": phase_id,
            "data_snapshot_id": snapshot_id,
            "run_id": run_id,
            "experiment_family": str(report.get("experiment_family", "general")),
        }
        for field, observed in event_checks.items():
            if existing_candidate.get(field) != observed:
                errors.append(f"candidate_id is already bound to a different {field}")
                candidate_binding_valid = False
        ready = not errors
        result["ready_for_human_submission"] = ready
        result["errors"] = errors

    output_path = workspace / "reports" / "current" / f"gate_{_safe_flag_part(candidate_id)}.json"
    write_json_atomic(output_path, {"report": report, "result": result})
    write_json_atomic(
        output_path.with_suffix(output_path.suffix + ".seal.json"),
        {"sha256": sha256_file(output_path), "sealed_at": result["checked_at"]},
    )

    linkable = (
        bool(candidate_id and track_id and phase_id and snapshot_id and run_id)
        and artifact_path.is_file()
        and observed_hash == report.get("artifact_sha256")
        and provenance_passed
        and any(
            item.get("event_type") == "run_planned"
            and item.get("run_id") == run_id
            and item.get("track_id") == track_id
            and item.get("phase_id") == phase_id
            and item.get("data_snapshot_id") == snapshot_id
            for item in events
        )
    )
    if existing_candidate is None and linkable:
        append_event_checked(
            workspace,
            event(
                "candidate_prepared",
                {
                    "candidate_id": candidate_id,
                    "intent": intent,
                    "artifact_path": artifact_path.as_posix(),
                    "artifact_sha256": observed_hash,
                    "decision": "pending",
                    "ready_for_human_submission": ready,
                    "gate_report": output_path.relative_to(workspace).as_posix(),
                    "probe": report.get("probe"),
                    "controlled_difference": report.get("controlled_difference"),
                    "metric_gate_passed": metric_passed,
                },
                track_id=track_id,
                phase_id=phase_id,
                data_snapshot_id=snapshot_id,
                run_id=run_id,
                experiment_family=str(report.get("experiment_family", "general")),
            ),
        )
        existing_candidate = _candidate_event(load_events(workspace), candidate_id)
    approval_payload = (
        {"candidate_id": candidate_id, **report["human_approval"]}
        if isinstance(report.get("human_approval"), dict)
        else None
    )
    if (
        existing_candidate is not None
        and candidate_binding_valid
        and not approval_errors
        and approval_payload is not None
        and not any(
            item.get("event_type") == "approval_recorded"
            and item.get("payload") == approval_payload
            for item in load_events(workspace)
        )
    ):
        append_event_checked(
            workspace,
            event(
                "approval_recorded",
                approval_payload,
                track_id=track_id,
                phase_id=phase_id,
                data_snapshot_id=snapshot_id,
                run_id=run_id,
                experiment_family=str(report.get("experiment_family", "general")),
            ),
        )
    materialize_state(workspace)
    return result


def record_metric(
    workspace: Path,
    *,
    run_id: str,
    metric: dict[str, Any],
    candidate_id: str | None = None,
    baseline_candidate_id: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    competition = load_and_validate_competition(workspace)
    errors = validate_metric(metric)
    if errors:
        raise OpsError("invalid metric:\n- " + "\n- ".join(errors))
    events = load_events(workspace)
    planned = next(
        (item for item in events if item.get("event_type") == "run_planned" and item.get("run_id") == run_id),
        None,
    )
    if planned is None:
        raise OpsError(f"run_id was not planned: {run_id}")
    track = get_track(competition, str(planned.get("track_id")))
    metric_definition = next(
        (item for item in track["metrics"] if item.get("name") == metric.get("name")),
        None,
    )
    if metric_definition is None:
        raise OpsError(f"metric is not registered for track {planned.get('track_id')}: {metric.get('name')}")
    if metric_definition.get("direction") != metric.get("direction"):
        raise OpsError(
            f"metric direction differs from competition.json: expected {metric_definition.get('direction')}"
        )
    terminal = next(
        (
            item
            for item in reversed(events)
            if item.get("event_type") in {"run_completed", "run_failed"} and item.get("run_id") == run_id
        ),
        None,
    )
    if terminal is None or terminal.get("payload", {}).get("status") != "completed":
        raise OpsError(f"metrics can only be recorded for completed valid runs: {run_id}")

    is_leaderboard = metric["source"] in {"public_lb", "private_lb", "final_result"}
    candidate = _candidate_event(events, candidate_id) if candidate_id else None
    if candidate_id and candidate is None:
        raise OpsError(f"candidate_id does not exist: {candidate_id}")
    if candidate is not None and candidate.get("run_id") != run_id:
        raise OpsError(f"candidate {candidate_id} is bound to a different run_id")
    candidate_artifact_sha256 = None
    if candidate is not None:
        candidate_payload = candidate.get("payload", {})
        candidate_artifact = Path(str(candidate_payload.get("artifact_path", "")))
        if not candidate_artifact.is_file():
            raise OpsError(f"candidate artifact is missing: {candidate_artifact}")
        candidate_artifact_sha256 = sha256_file(candidate_artifact)
        if candidate_artifact_sha256 != candidate_payload.get("artifact_sha256"):
            raise OpsError("candidate artifact SHA256 no longer matches its prepared binding")
    if is_leaderboard and candidate is None:
        raise OpsError("leaderboard and final metrics must bind to a prepared candidate_id")
    baseline_metric = None
    if baseline_candidate_id:
        if candidate is None:
            raise OpsError("baseline_candidate_id requires a current candidate_id")
        baseline_candidate = _candidate_event(events, baseline_candidate_id)
        if baseline_candidate is None:
            raise OpsError(f"baseline candidate does not exist: {baseline_candidate_id}")
        for field in ["track_id", "phase_id", "data_snapshot_id"]:
            if baseline_candidate.get(field) != planned.get(field):
                raise OpsError(
                    f"baseline candidate is not comparable: {field} differs "
                    f"({baseline_candidate.get(field)} != {planned.get(field)})"
                )
        comparable_metric_fields = [
            "name",
            "direction",
            "source",
            "split",
            "scope",
            "comparable_group",
        ]
        for item in reversed(events):
            payload = item.get("payload", {})
            prior_metric = payload.get("metric")
            if (
                item.get("event_type") in {"leaderboard_feedback_recorded", "metric_recorded"}
                and payload.get("candidate_id") == baseline_candidate_id
                and isinstance(prior_metric, dict)
                and all(prior_metric.get(field) == metric[field] for field in comparable_metric_fields)
                and all(
                    item.get(field) == planned.get(field)
                    for field in ["track_id", "phase_id", "data_snapshot_id"]
                )
            ):
                baseline_metric = prior_metric
                break
        if baseline_metric is None:
            raise OpsError(f"no comparable baseline metric found for candidate: {baseline_candidate_id}")
    delta = None
    if baseline_metric:
        raw = float(metric["value"]) - float(baseline_metric["value"])
        delta = raw if metric["direction"] == "higher" else -raw

    event_type = "leaderboard_feedback_recorded" if is_leaderboard else "metric_recorded"
    payload = {
        "metric": metric,
        "candidate_id": candidate_id,
        "candidate_artifact_sha256": candidate_artifact_sha256,
        "baseline_candidate_id": baseline_candidate_id,
        "improvement_delta": delta,
        "attribution_scope": (
            candidate.get("payload", {}).get("controlled_difference")
            or (
                candidate.get("payload", {}).get("probe", {}).get("controlled_difference")
                if candidate and isinstance(candidate.get("payload", {}).get("probe"), dict)
                else None
            )
            if candidate
            else None
        ),
        "promotion_performed": False,
    }
    item = event(
        event_type,
        payload,
        track_id=str(planned.get("track_id")),
        phase_id=str(planned.get("phase_id")),
        data_snapshot_id=str(planned.get("data_snapshot_id")),
        run_id=run_id,
        idea_id=str(planned.get("idea_id")),
        experiment_family=str(planned.get("experiment_family")),
    )
    append_event_checked(workspace, item)
    materialize_state(workspace)
    return item


def promote_candidate(workspace: Path, *, candidate_id: str, reason: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    with exclusive_lock(workspace / "ledger" / "promotion.lock"):
        return _promote_candidate_locked(workspace, candidate_id=candidate_id, reason=reason)


def _promote_candidate_locked(
    workspace: Path, *, candidate_id: str, reason: str
) -> dict[str, Any]:
    workspace = workspace.resolve()
    events = load_events(workspace)
    candidate = _candidate_event(events, candidate_id)
    if candidate is None:
        raise OpsError(f"candidate_id does not exist: {candidate_id}")
    terminal_decision = next(
        (
            item
            for item in reversed(events)
            if item.get("event_type") in {"candidate_promoted", "candidate_rejected"}
            and item.get("payload", {}).get("candidate_id") == candidate_id
        ),
        None,
    )
    if terminal_decision is not None:
        raise OpsError(
            f"candidate already has terminal decision: "
            f"{terminal_decision.get('payload', {}).get('decision')}"
        )
    payload = candidate["payload"]
    gate_path = workspace / str(payload.get("gate_report", ""))
    if not gate_path.is_file():
        raise OpsError(f"candidate gate report is missing: {gate_path}")
    gate = read_json(gate_path)
    gate_seal_path = gate_path.with_suffix(gate_path.suffix + ".seal.json")
    if not gate_seal_path.is_file():
        raise OpsError("candidate gate report seal is missing")
    gate_seal = read_json(gate_seal_path)
    if gate_seal.get("sha256") != sha256_file(gate_path):
        raise OpsError("candidate gate report was modified after it was sealed")
    if gate.get("result", {}).get("ready_for_human_submission") is not True:
        raise OpsError("candidate cannot be promoted because its gate is not ready")
    stored_report = gate.get("report")
    if not isinstance(stored_report, dict):
        raise OpsError("candidate gate report is malformed")
    artifact_path = Path(str(payload.get("artifact_path", "")))
    if not artifact_path.is_file():
        raise OpsError(f"candidate artifact is missing: {artifact_path}")
    current_hash = sha256_file(artifact_path)
    if current_hash != payload.get("artifact_sha256"):
        raise OpsError("candidate artifact no longer matches its immutable SHA256 binding")
    approval_errors = _approval_errors(
        stored_report.get("human_approval"),
        candidate_id=candidate_id,
        artifact_sha256=current_hash,
        intent=str(payload.get("intent", "")),
    )
    if approval_errors:
        raise OpsError("candidate approval is no longer valid:\n- " + "\n- ".join(approval_errors))
    approval = stored_report["human_approval"]
    if not any(
        item.get("event_type") == "approval_recorded"
        and item.get("payload", {}).get("candidate_id") == candidate_id
        and all(item.get("payload", {}).get(field) == approval.get(field) for field in approval)
        for item in events
    ):
        raise OpsError("candidate approval is not present in the append-only ledger")
    feedback = next(
        (
            item
            for item in reversed(events)
            if item.get("event_type") in {"leaderboard_feedback_recorded", "metric_recorded"}
            and item.get("payload", {}).get("candidate_id") == candidate_id
        ),
        None,
    )
    metric = feedback.get("payload", {}).get("metric") if feedback else None
    if not isinstance(metric, dict):
        raise OpsError("candidate cannot be promoted before a bound metric is recorded")
    champions_path = workspace / "champions.json"
    champions = read_json(champions_path)
    key = lane_key(
        str(candidate.get("track_id")),
        str(candidate.get("phase_id")),
        str(candidate.get("data_snapshot_id")),
    )
    previous = champions["lanes"].get(key, {})
    if previous.get("champion_candidate_id") == candidate_id:
        raise OpsError("candidate is already the champion for this lane")
    rejected = {
        str(item.get("payload", {}).get("candidate_id"))
        for item in events
        if item.get("event_type") == "candidate_rejected"
        and item.get("payload", {}).get("decision") in {"rejected", "superseded"}
    }
    challengers = sorted(
        {
            str(item.get("payload", {}).get("candidate_id"))
            for item in events
            if item.get("event_type") == "candidate_prepared"
            and item.get("track_id") == candidate.get("track_id")
            and item.get("phase_id") == candidate.get("phase_id")
            and item.get("data_snapshot_id") == candidate.get("data_snapshot_id")
            and item.get("payload", {}).get("candidate_id")
        }
        - rejected
        - {candidate_id}
    )
    champions["lanes"][key] = {
        "track_id": candidate.get("track_id"),
        "phase_id": candidate.get("phase_id"),
        "data_snapshot_id": candidate.get("data_snapshot_id"),
        "champion_candidate_id": candidate_id,
        "champion_run_id": candidate.get("run_id"),
        "rollback_candidate_id": previous.get("champion_candidate_id"),
        "online_anchor_candidate_id": (
            candidate_id
            if metric.get("source") in {"public_lb", "private_lb", "final_result"}
            else previous.get("online_anchor_candidate_id")
        ),
        "challenger_candidate_ids": challengers,
        "updated_at": utc_now(),
    }
    champions["lanes"][key]["metric"] = metric
    champions["updated_at"] = utc_now()
    promotion = event(
        "candidate_promoted",
        {
            "candidate_id": candidate_id,
            "decision": "promoted",
            "reason": reason,
            "previous_candidate_id": previous.get("champion_candidate_id"),
            "metric": metric,
        },
        track_id=str(candidate.get("track_id")),
        phase_id=str(candidate.get("phase_id")),
        data_snapshot_id=str(candidate.get("data_snapshot_id")),
        run_id=str(candidate.get("run_id")),
        experiment_family=str(candidate.get("experiment_family")),
    )
    append_event_checked(workspace, promotion)
    write_json_atomic(champions_path, champions)
    materialize_state(workspace)
    return champions["lanes"][key]

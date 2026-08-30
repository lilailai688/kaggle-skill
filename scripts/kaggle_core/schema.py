from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .common import (
    SCHEMA_VERSION,
    OpsError,
    append_jsonl_unlocked,
    exclusive_lock,
    iter_jsonl,
    parse_utc,
    read_json,
    require_keys,
    unknown_keys,
)


EVENT_TYPES = {
    "workspace_bootstrapped",
    "data_ingested",
    "legacy_record_imported",
    "idea_created",
    "idea_updated",
    "run_planned",
    "run_started",
    "run_completed",
    "run_failed",
    "validation_recorded",
    "metric_recorded",
    "candidate_prepared",
    "approval_recorded",
    "leaderboard_feedback_recorded",
    "candidate_promoted",
    "candidate_rejected",
    "release_built",
    "postmortem_created",
}
RUN_STATUSES = {"planned", "running", "completed", "failed", "invalid"}
CANDIDATE_DECISIONS = {"pending", "promoted", "rejected", "superseded"}
METRIC_SOURCES = {"local_cv", "local_proxy", "public_lb", "private_lb", "final_result"}
SUBMISSION_INTENTS = {"probe", "candidate", "final"}
IDEA_PRIORITIES = {"high", "medium", "low"}
IDEA_STATUSES = {"open", "done", "dropped"}

EVENT_BASE_FIELDS = {
    "schema_version",
    "event_id",
    "event_type",
    "occurred_at",
    "payload",
    "track_id",
    "phase_id",
    "data_snapshot_id",
    "run_id",
    "idea_id",
    "experiment_family",
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_metric_definition(value: Any, context: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{context}: must be an object"]
    errors = require_keys(value, ["name", "direction", "primary", "eps"], context)
    errors.extend(
        unknown_keys(value, {"name", "direction", "primary", "eps", "weight", "metadata"}, context)
    )
    if not _nonempty_string(value.get("name")):
        errors.append(f"{context}: name must be a non-empty string")
    if value.get("direction") not in {"higher", "lower"}:
        errors.append(f"{context}: direction must be higher or lower")
    if not isinstance(value.get("primary"), bool):
        errors.append(f"{context}: primary must be boolean")
    eps = value.get("eps")
    if (
        isinstance(eps, bool)
        or not isinstance(eps, (int, float))
        or not math.isfinite(float(eps))
        or eps < 0
    ):
        errors.append(f"{context}: eps must be a non-negative number")
    if "weight" in value and (
        isinstance(value["weight"], bool)
        or not isinstance(value["weight"], (int, float))
        or not math.isfinite(float(value["weight"]))
    ):
        errors.append(f"{context}: weight must be numeric")
    return errors


def validate_competition(value: dict[str, Any]) -> list[str]:
    context = "competition.json"
    allowed = {
        "schema_version",
        "competition",
        "phases",
        "tracks",
        "input_contract",
        "output_contract",
        "dryness",
        "rules",
        "created_at",
        "extensions",
    }
    errors = require_keys(
        value,
        [
            "schema_version",
            "competition",
            "phases",
            "tracks",
            "input_contract",
            "output_contract",
            "dryness",
            "rules",
            "created_at",
        ],
        context,
    )
    errors.extend(unknown_keys(value, allowed, context))
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{context}: schema_version must be {SCHEMA_VERSION}")

    competition = value.get("competition")
    if not isinstance(competition, dict):
        errors.append(f"{context}: competition must be an object")
    else:
        errors.extend(require_keys(competition, ["slug", "platform", "problem_type"], "competition"))
        errors.extend(
            unknown_keys(
                competition,
                {"slug", "name", "platform", "problem_type", "submission_mode", "metadata"},
                "competition",
            )
        )
        for field in ["slug", "platform", "problem_type"]:
            if not _nonempty_string(competition.get(field)):
                errors.append(f"competition: {field} must be a non-empty string")

    phases = value.get("phases")
    phase_ids: list[str] = []
    if not isinstance(phases, list) or not phases:
        errors.append(f"{context}: phases must be a non-empty list")
    else:
        for index, phase in enumerate(phases):
            item_context = f"phases[{index}]"
            if not isinstance(phase, dict):
                errors.append(f"{item_context}: must be an object")
                continue
            errors.extend(require_keys(phase, ["id", "submission_limit"], item_context))
            errors.extend(
                unknown_keys(
                    phase,
                    {"id", "name", "deadline", "submission_limit", "score_scope", "metadata"},
                    item_context,
                )
            )
            phase_id = phase.get("id")
            if not _nonempty_string(phase_id):
                errors.append(f"{item_context}: id must be a non-empty string")
            else:
                phase_ids.append(phase_id)
            if (
                isinstance(phase.get("submission_limit"), bool)
                or not isinstance(phase.get("submission_limit"), int)
                or phase.get("submission_limit", -1) < 0
            ):
                errors.append(f"{item_context}: submission_limit must be a non-negative integer")
            if phase.get("deadline"):
                try:
                    parse_utc(str(phase["deadline"]))
                except (TypeError, ValueError):
                    errors.append(f"{item_context}: deadline must be an ISO-8601 timestamp")
    duplicates = [item for item, count in Counter(phase_ids).items() if count > 1]
    errors.extend(f"phases: duplicate id: {item}" for item in duplicates)

    tracks = value.get("tracks")
    track_ids: list[str] = []
    if not isinstance(tracks, list) or not tracks:
        errors.append(f"{context}: tracks must be a non-empty list")
    else:
        for index, track in enumerate(tracks):
            item_context = f"tracks[{index}]"
            if not isinstance(track, dict):
                errors.append(f"{item_context}: must be an object")
                continue
            errors.extend(require_keys(track, ["id", "problem_type", "metrics"], item_context))
            errors.extend(
                unknown_keys(track, {"id", "name", "problem_type", "metrics", "metadata"}, item_context)
            )
            track_id = track.get("id")
            if not _nonempty_string(track_id):
                errors.append(f"{item_context}: id must be a non-empty string")
            else:
                track_ids.append(track_id)
            if not _nonempty_string(track.get("problem_type")):
                errors.append(f"{item_context}: problem_type must be a non-empty string")
            metrics = track.get("metrics")
            if not isinstance(metrics, list) or not metrics:
                errors.append(f"{item_context}: metrics must be a non-empty list")
            else:
                primary_count = 0
                names: list[str] = []
                for metric_index, metric in enumerate(metrics):
                    errors.extend(_validate_metric_definition(metric, f"{item_context}.metrics[{metric_index}]"))
                    if isinstance(metric, dict):
                        names.append(str(metric.get("name", "")))
                        primary_count += metric.get("primary") is True
                if primary_count != 1:
                    errors.append(f"{item_context}: exactly one metric must be primary")
                metric_duplicates = [item for item, count in Counter(names).items() if item and count > 1]
                errors.extend(f"{item_context}: duplicate metric name: {item}" for item in metric_duplicates)
    track_duplicates = [item for item, count in Counter(track_ids).items() if count > 1]
    errors.extend(f"tracks: duplicate id: {item}" for item in track_duplicates)

    input_contract = value.get("input_contract")
    if not isinstance(input_contract, dict):
        errors.append(f"{context}: input_contract must be an object")
    else:
        errors.extend(
            unknown_keys(
                input_contract,
                {
                    "required_globs",
                    "forbidden_globs",
                    "min_files",
                    "max_files",
                    "tabular_checks",
                    "temporal_checks",
                    "validator_hooks",
                    "metadata",
                },
                "input_contract",
            )
        )
        for field in [
            "required_globs",
            "forbidden_globs",
            "tabular_checks",
            "temporal_checks",
            "validator_hooks",
        ]:
            if field in input_contract and not isinstance(input_contract[field], list):
                errors.append(f"input_contract: {field} must be a list")

    output_contract = value.get("output_contract")
    if not isinstance(output_contract, dict):
        errors.append(f"{context}: output_contract must be an object")
    else:
        errors.extend(
            unknown_keys(
                output_contract,
                {
                    "required_artifacts",
                    "submission_zip_members",
                    "tabular_checks",
                    "release_includes",
                    "release_excludes",
                    "validator_hooks",
                    "metadata",
                },
                "output_contract",
            )
        )
        for field in [
            "required_artifacts",
            "submission_zip_members",
            "tabular_checks",
            "release_includes",
            "release_excludes",
            "validator_hooks",
        ]:
            if field in output_contract and not isinstance(output_contract[field], list):
                errors.append(f"output_contract: {field} must be a list")

    dryness = value.get("dryness")
    if not isinstance(dryness, dict):
        errors.append(f"{context}: dryness must be an object")
    else:
        errors.extend(require_keys(dryness, ["patience", "failure_patience"], "dryness"))
        errors.extend(unknown_keys(dryness, {"patience", "failure_patience"}, "dryness"))
        for field in ["patience", "failure_patience"]:
            if not isinstance(dryness.get(field), int) or dryness.get(field, 0) < 1:
                errors.append(f"dryness: {field} must be a positive integer")

    rules = value.get("rules")
    if not isinstance(rules, dict):
        errors.append(f"{context}: rules must be an object")
    else:
        required_rules = [
            "external_data_policy",
            "network_policy",
            "model_license_policy",
            "max_business_date",
            "external_assets",
            "model_licenses",
            "prohibited_identity_shortcuts",
            "notes",
        ]
        allowed_rules = set(required_rules)
        errors.extend(require_keys(rules, required_rules, "rules"))
        errors.extend(unknown_keys(rules, allowed_rules, "rules"))
        for field in ["external_assets", "model_licenses", "prohibited_identity_shortcuts", "notes"]:
            if field in rules and not isinstance(rules[field], list):
                errors.append(f"rules: {field} must be a list")
        if rules.get("max_business_date") is not None and not _nonempty_string(rules.get("max_business_date")):
            errors.append("rules: max_business_date must be null or a non-empty string")
    try:
        parse_utc(str(value.get("created_at", "")))
    except (TypeError, ValueError):
        errors.append(f"{context}: created_at must be an ISO-8601 timestamp")
    return errors


def validate_metric(value: Any, context: str = "metric") -> list[str]:
    if not isinstance(value, dict):
        return [f"{context}: must be an object"]
    allowed = {"name", "value", "direction", "source", "split", "scope", "comparable_group"}
    errors = require_keys(value, sorted(allowed), context)
    errors.extend(unknown_keys(value, allowed, context))
    if not _nonempty_string(value.get("name")):
        errors.append(f"{context}: name must be a non-empty string")
    metric_value = value.get("value")
    if (
        isinstance(metric_value, bool)
        or not isinstance(metric_value, (int, float))
        or not math.isfinite(float(metric_value))
    ):
        errors.append(f"{context}: value must be numeric")
    if value.get("direction") not in {"higher", "lower"}:
        errors.append(f"{context}: direction must be higher or lower")
    if value.get("source") not in METRIC_SOURCES:
        errors.append(f"{context}: invalid source: {value.get('source')}")
    for field in ["split", "scope", "comparable_group"]:
        if not _nonempty_string(value.get(field)):
            errors.append(f"{context}: {field} must be a non-empty string")
    return errors


def validate_event(value: dict[str, Any], context: str = "event") -> list[str]:
    errors = require_keys(
        value,
        ["schema_version", "event_id", "event_type", "occurred_at", "payload"],
        context,
    )
    errors.extend(unknown_keys(value, EVENT_BASE_FIELDS, context))
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{context}: schema_version must be {SCHEMA_VERSION}")
    if not _nonempty_string(value.get("event_id")):
        errors.append(f"{context}: event_id must be a non-empty string")
    event_type = value.get("event_type")
    if event_type not in EVENT_TYPES:
        errors.append(f"{context}: invalid event_type: {event_type}")
    try:
        parse_utc(str(value.get("occurred_at", "")))
    except (TypeError, ValueError):
        errors.append(f"{context}: occurred_at must be an ISO-8601 timestamp")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        errors.append(f"{context}: payload must be an object")
        payload = {}

    run_events = {
        "run_planned",
        "run_started",
        "run_completed",
        "run_failed",
        "validation_recorded",
        "metric_recorded",
    }
    associated_candidate_events = {
        "candidate_prepared",
        "approval_recorded",
        "leaderboard_feedback_recorded",
        "candidate_promoted",
        "candidate_rejected",
    }
    run_status_events = {
        "run_planned",
        "run_started",
        "run_completed",
        "run_failed",
        "validation_recorded",
    }
    lane_events = run_events | {
        "idea_created",
        "idea_updated",
        "candidate_prepared",
        "approval_recorded",
        "leaderboard_feedback_recorded",
        "candidate_promoted",
        "candidate_rejected",
    }
    if event_type in lane_events:
        for field in ["track_id", "phase_id"]:
            if not _nonempty_string(value.get(field)):
                errors.append(f"{context}: {field} is required for {event_type}")
    if event_type in run_events:
        for field in ["run_id", "data_snapshot_id", "idea_id", "experiment_family"]:
            if not _nonempty_string(value.get(field)):
                errors.append(f"{context}: {field} is required for {event_type}")
    if event_type in associated_candidate_events:
        for field in ["run_id", "data_snapshot_id", "experiment_family"]:
            if not _nonempty_string(value.get(field)):
                errors.append(f"{context}: {field} is required for {event_type}")
    if event_type in {"idea_created", "idea_updated"}:
        if not _nonempty_string(value.get("idea_id")):
            errors.append(f"{context}: idea_id is required for {event_type}")
        if payload.get("priority") not in IDEA_PRIORITIES:
            errors.append(f"{context}: invalid idea priority: {payload.get('priority')}")
        if payload.get("status") not in IDEA_STATUSES:
            errors.append(f"{context}: invalid idea status: {payload.get('status')}")
    if event_type in {"candidate_prepared", "candidate_promoted", "candidate_rejected"}:
        if payload.get("decision") not in CANDIDATE_DECISIONS:
            errors.append(f"{context}: invalid candidate decision: {payload.get('decision')}")
    if event_type in run_status_events and payload.get("status") not in RUN_STATUSES:
        errors.append(f"{context}: invalid run status: {payload.get('status')}")
    if "metric" in payload:
        errors.extend(validate_metric(payload["metric"], f"{context}.payload.metric"))
    if "metrics" in payload:
        if not isinstance(payload["metrics"], list):
            errors.append(f"{context}.payload.metrics: must be a list")
        else:
            for index, metric in enumerate(payload["metrics"]):
                errors.extend(validate_metric(metric, f"{context}.payload.metrics[{index}]"))
    return errors


def load_events(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "ledger" / "events.jsonl"
    return [item for _, item in iter_jsonl(path)] if path.exists() else []


def validate_event_history(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    event_ids: set[str] = set()
    planned_runs: set[str] = set()
    ideas: set[str] = set()
    candidates: dict[str, dict[str, Any]] = {}
    candidate_decisions: dict[str, str] = {}
    for index, item in enumerate(events):
        context = f"events[{index}]"
        errors.extend(validate_event(item, context))
        event_id = item.get("event_id")
        if event_id in event_ids:
            errors.append(f"{context}: duplicate event_id: {event_id}")
        event_ids.add(str(event_id))
        event_type = item.get("event_type")
        run_id = item.get("run_id")
        idea_id = item.get("idea_id")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if event_type == "idea_created":
            if idea_id in ideas:
                errors.append(f"{context}: duplicate idea_id: {idea_id}")
            ideas.add(str(idea_id))
        elif event_type == "idea_updated" and idea_id not in ideas:
            errors.append(f"{context}: idea_id does not exist: {idea_id}")
        if event_type == "run_planned":
            if run_id in planned_runs:
                errors.append(f"{context}: duplicate run_id: {run_id}")
            planned_runs.add(str(run_id))
            if idea_id not in ideas:
                errors.append(f"{context}: idea_id does not exist: {idea_id}")
        elif event_type in {
            "run_started",
            "run_completed",
            "run_failed",
            "validation_recorded",
            "metric_recorded",
            "leaderboard_feedback_recorded",
        }:
            if run_id not in planned_runs:
                errors.append(f"{context}: run_id was not planned: {run_id}")
        if event_type == "candidate_prepared":
            candidate_id = payload.get("candidate_id")
            if not _nonempty_string(candidate_id):
                errors.append(f"{context}: candidate_id is required")
            elif candidate_id in candidates:
                errors.append(f"{context}: duplicate candidate_id: {candidate_id}")
            else:
                candidates[candidate_id] = item
                candidate_decisions[candidate_id] = "pending"
            if run_id not in planned_runs:
                errors.append(f"{context}: candidate run_id was not planned: {run_id}")
        if event_type == "metric_recorded" and payload.get("candidate_id") is not None:
            if payload.get("candidate_id") not in candidates:
                errors.append(
                    f"{context}: candidate_id does not exist: {payload.get('candidate_id')}"
                )
        if event_type in {
            "approval_recorded",
            "leaderboard_feedback_recorded",
            "candidate_promoted",
            "candidate_rejected",
        }:
            candidate_id = payload.get("candidate_id")
            if candidate_id not in candidates:
                errors.append(f"{context}: candidate_id does not exist: {candidate_id}")
            else:
                prepared = candidates[str(candidate_id)]
                for field in [
                    "track_id",
                    "phase_id",
                    "data_snapshot_id",
                    "run_id",
                    "experiment_family",
                ]:
                    if item.get(field) != prepared.get(field):
                        errors.append(
                            f"{context}: {field} differs from prepared candidate {candidate_id}"
                        )
        if event_type in {"candidate_promoted", "candidate_rejected"}:
            candidate_id = str(payload.get("candidate_id", ""))
            current_decision = candidate_decisions.get(candidate_id)
            if current_decision != "pending":
                errors.append(
                    f"{context}: candidate decision cannot transition from "
                    f"{current_decision} to {payload.get('decision')}"
                )
            elif payload.get("decision") in {"promoted", "rejected", "superseded"}:
                candidate_decisions[candidate_id] = str(payload["decision"])
    return errors


def append_event_checked(workspace: Path, value: dict[str, Any]) -> None:
    ledger_path = workspace / "ledger" / "events.jsonl"
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with exclusive_lock(lock_path):
        current = load_events(workspace)
        proposed = [*current, value]
        errors = validate_event_history(proposed)
        if errors:
            raise OpsError("event rejected:\n- " + "\n- ".join(errors))
        append_jsonl_unlocked(ledger_path, value)


def validate_champions(value: dict[str, Any]) -> list[str]:
    errors = require_keys(value, ["schema_version", "lanes", "updated_at"], "champions.json")
    errors.extend(unknown_keys(value, {"schema_version", "lanes", "updated_at"}, "champions.json"))
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"champions.json: schema_version must be {SCHEMA_VERSION}")
    lanes = value.get("lanes")
    if not isinstance(lanes, dict):
        errors.append("champions.json: lanes must be an object")
        return errors
    lane_allowed = {
        "track_id",
        "phase_id",
        "data_snapshot_id",
        "champion_candidate_id",
        "champion_run_id",
        "rollback_candidate_id",
        "online_anchor_candidate_id",
        "challenger_candidate_ids",
        "metric",
        "updated_at",
    }
    for key, lane in lanes.items():
        context = f"champions.json.lanes[{key}]"
        if not isinstance(lane, dict):
            errors.append(f"{context}: must be an object")
            continue
        errors.extend(
            require_keys(
                lane,
                ["track_id", "phase_id", "data_snapshot_id", "champion_candidate_id", "champion_run_id", "updated_at"],
                context,
            )
        )
        errors.extend(unknown_keys(lane, lane_allowed, context))
        expected_key = lane_key(
            str(lane.get("track_id", "")),
            str(lane.get("phase_id", "")),
            str(lane.get("data_snapshot_id", "")),
        )
        if key != expected_key:
            errors.append(f"{context}: lane key must be {expected_key}")
        if "metric" in lane:
            errors.extend(validate_metric(lane["metric"], f"{context}.metric"))
        if "challenger_candidate_ids" in lane and not isinstance(lane["challenger_candidate_ids"], list):
            errors.append(f"{context}: challenger_candidate_ids must be a list")
    return errors


def lane_key(track_id: str, phase_id: str, data_snapshot_id: str) -> str:
    return "|".join([track_id, phase_id, data_snapshot_id])


def get_track(competition: dict[str, Any], track_id: str) -> dict[str, Any]:
    for track in competition.get("tracks", []):
        if isinstance(track, dict) and track.get("id") == track_id:
            return track
    raise OpsError(f"unknown track_id: {track_id}")


def get_phase(competition: dict[str, Any], phase_id: str) -> dict[str, Any]:
    for phase in competition.get("phases", []):
        if isinstance(phase, dict) and phase.get("id") == phase_id:
            return phase
    raise OpsError(f"unknown phase_id: {phase_id}")


def load_and_validate_competition(workspace: Path) -> dict[str, Any]:
    value = read_json(workspace / "competition.json")
    errors = validate_competition(value)
    if errors:
        raise OpsError("invalid competition.json:\n- " + "\n- ".join(errors))
    rules_snapshot = read_json(workspace / "rules" / "rules.json")
    if rules_snapshot != value.get("rules"):
        raise OpsError(
            "rules/rules.json differs from competition.json.rules; refresh the explicit rule snapshot"
        )
    return value

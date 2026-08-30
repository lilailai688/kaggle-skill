from __future__ import annotations

import csv
import fnmatch
import importlib.util
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import (
    SCHEMA_VERSION,
    OpsError,
    append_jsonl,
    event,
    ensure_identifier,
    file_record,
    hash_json,
    iter_jsonl,
    read_json,
    sha256_file,
    utc_now,
    write_json_atomic,
)
from .schema import (
    append_event_checked,
    get_phase,
    get_track,
    lane_key,
    load_and_validate_competition,
    load_events,
    validate_champions,
    validate_competition,
    validate_event,
    validate_event_history,
)


WORKSPACE_DIRS = [
    "ledger",
    "data/manifests",
    "runs",
    "artifacts/sha256",
    "reports/current",
    "reports/archive",
    "reports/archive/state",
    "submissions",
    "rules",
    "release",
    "flags",
    "validators",
]


def build_competition(
    slug: str,
    platform: str,
    problem_type: str,
    track_id: str,
    phase_id: str,
    metric: str,
    direction: str,
    eps: float,
    submission_limit: int,
    deadline: str | None,
    submission_mode: str,
) -> dict[str, Any]:
    phase: dict[str, Any] = {
        "id": phase_id,
        "name": phase_id,
        "submission_limit": submission_limit,
        "score_scope": "competition-defined",
        "metadata": {},
    }
    if deadline:
        phase["deadline"] = deadline
    return {
        "schema_version": SCHEMA_VERSION,
        "competition": {
            "slug": slug,
            "name": slug,
            "platform": platform,
            "problem_type": problem_type,
            "submission_mode": submission_mode,
            "metadata": {},
        },
        "phases": [phase],
        "tracks": [
            {
                "id": track_id,
                "name": track_id,
                "problem_type": problem_type,
                "metrics": [
                    {
                        "name": metric,
                        "direction": direction,
                        "primary": True,
                        "eps": eps,
                        "weight": 1.0,
                        "metadata": {},
                    }
                ],
                "metadata": {},
            }
        ],
        "input_contract": {
            "required_globs": [],
            "forbidden_globs": [],
            "min_files": 1,
            "tabular_checks": [],
            "temporal_checks": [],
            "validator_hooks": [],
            "metadata": {},
        },
        "output_contract": {
            "required_artifacts": [],
            "submission_zip_members": [],
            "tabular_checks": [],
            "release_includes": [],
            "release_excludes": [],
            "validator_hooks": [],
            "metadata": {},
        },
        "dryness": {"patience": 5, "failure_patience": 3},
        "rules": {
            "external_data_policy": "unknown",
            "network_policy": "unknown",
            "model_license_policy": "unknown",
            "max_business_date": None,
            "external_assets": [],
            "model_licenses": [],
            "prohibited_identity_shortcuts": [],
            "notes": [],
        },
        "extensions": {},
        "created_at": utc_now(),
    }


def bootstrap_workspace(
    workspace: Path,
    *,
    slug: str,
    platform: str = "manual",
    problem_type: str = "custom",
    track_id: str = "primary",
    phase_id: str = "active",
    metric: str = "primary_metric",
    direction: str = "higher",
    eps: float = 0.001,
    submission_limit: int = 5,
    deadline: str | None = None,
    submission_mode: str = "file",
) -> dict[str, Any]:
    workspace = workspace.resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise OpsError(f"workspace is not empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in WORKSPACE_DIRS:
        (workspace / directory).mkdir(parents=True, exist_ok=True)

    competition = build_competition(
        slug,
        platform,
        problem_type,
        track_id,
        phase_id,
        metric,
        direction,
        eps,
        submission_limit,
        deadline,
        submission_mode,
    )
    errors = validate_competition(competition)
    if errors:
        raise OpsError("bootstrap produced invalid competition.json:\n- " + "\n- ".join(errors))
    write_json_atomic(workspace / "competition.json", competition)
    champions = {"schema_version": SCHEMA_VERSION, "lanes": {}, "updated_at": utc_now()}
    write_json_atomic(workspace / "champions.json", champions)
    write_json_atomic(workspace / "rules" / "rules.json", competition["rules"])
    (workspace / "ledger" / "events.jsonl").touch()
    append_event_checked(
        workspace,
        event(
            "workspace_bootstrapped",
            {
                "workspace": workspace.as_posix(),
                "slug": slug,
                "platform": platform,
                "problem_type": problem_type,
            },
        ),
    )
    append_event_checked(
        workspace,
        event(
            "idea_created",
            {
                "hypothesis": "Build a trusted end-to-end baseline before optimization.",
                "priority": "high",
                "status": "open",
                "validation": "Complete the competition-defined input, evaluation, and output contracts.",
                "failure_mode": "The baseline is not comparable because the data split, metric, or output contract is wrong.",
            },
            track_id=track_id,
            phase_id=phase_id,
            idea_id="idea-baseline",
            experiment_family="baseline",
        ),
    )
    status = materialize_state(workspace)
    return {"workspace": workspace.as_posix(), "competition": competition, "status": status}


def _extract_legacy_marker(text: str, marker: str, fallback: str) -> str:
    match = re.search(rf"(?mi)^-?\s*{re.escape(marker)}\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else fallback


def _legacy_track(record: dict[str, Any], mapping: dict[str, Any] | None) -> str | None:
    explicit = record.get("track_id") or record.get("task") or record.get("track")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if not mapping:
        return None
    searchable = json.dumps(record, ensure_ascii=False, sort_keys=True)
    for rule in mapping.get("track_rules", []):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        track_id = rule.get("track_id")
        if isinstance(pattern, str) and isinstance(track_id, str) and re.search(pattern, searchable):
            return track_id
    return mapping.get("default_track_id") if isinstance(mapping.get("default_track_id"), str) else None


def migrate_v1_workspace(
    source: Path,
    destination: Path,
    *,
    slug: str | None = None,
    platform: str = "manual",
    mapping_path: Path | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    required = [source / "STATE.md", source / "ideas_backlog.md", source / "experiment_ledger.jsonl"]
    missing = [path.as_posix() for path in required if not path.is_file()]
    if missing:
        raise OpsError("V1 workspace is missing required files: " + ", ".join(missing))
    before = {path.name: sha256_file(path) for path in required}
    state_text = (source / "STATE.md").read_text(encoding="utf-8", errors="replace")
    inferred_slug = slug or _extract_legacy_marker(state_text, "Competition slug:", source.name)
    metric = _extract_legacy_marker(state_text, "Metric:", "primary_metric")
    direction = _extract_legacy_marker(state_text, "Direction:", "higher").lower()
    if direction not in {"higher", "lower"}:
        direction = "higher"
    bootstrap_workspace(
        destination,
        slug=inferred_slug,
        platform=platform,
        problem_type="legacy-import",
        track_id="legacy",
        phase_id="legacy",
        metric=metric,
        direction=direction,
    )
    archive = destination / "reports" / "archive" / "v1"
    archive.mkdir(parents=True, exist_ok=True)
    for path in required:
        shutil.copy2(path, archive / path.name)

    mapping = read_json(mapping_path) if mapping_path else None
    imported: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    invalid_lines = 0
    blank_lines = 0
    json_records = 0
    with (source / "experiment_ledger.jsonl").open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                blank_lines += 1
                imported.append(
                    event(
                        "legacy_record_imported",
                        {
                            "legacy_status": "__blank_line__",
                            "legacy_raw_line": line.rstrip("\r\n"),
                            "legacy_line_no": line_no,
                            "import_status": "imported",
                        },
                    )
                )
                continue
            try:
                legacy = json.loads(line)
                if not isinstance(legacy, dict):
                    raise ValueError("record is not an object")
                legacy_status = str(legacy.get("status", ""))
                payload = {
                    "legacy_status": legacy_status,
                    "legacy_payload": legacy,
                    "legacy_line_no": line_no,
                    "import_status": "imported",
                }
                track_id = _legacy_track(legacy, mapping)
                statuses[legacy_status] += 1
                json_records += 1
            except (json.JSONDecodeError, ValueError) as exc:
                invalid_lines += 1
                payload = {
                    "legacy_status": "__invalid_json__",
                    "legacy_raw_line": line.rstrip("\n"),
                    "legacy_line_no": line_no,
                    "import_status": "imported",
                    "parse_error": str(exc),
                }
                track_id = None
                statuses["__invalid_json__"] += 1
            imported.append(event("legacy_record_imported", payload, track_id=track_id))

    existing = load_events(destination)
    errors = validate_event_history([*existing, *imported])
    if errors:
        raise OpsError("migration events failed validation:\n- " + "\n- ".join(errors))
    for item in imported:
        append_jsonl(destination / "ledger" / "events.jsonl", item)

    after = {path.name: sha256_file(path) for path in required}
    if before != after:
        raise OpsError("source workspace changed during migration")
    report = {
        "schema_version": SCHEMA_VERSION,
        "source": source.as_posix(),
        "destination": destination.as_posix(),
        "source_hashes_before": before,
        "source_hashes_after": after,
        "source_unchanged": True,
        "records_imported": len(imported),
        "json_records_imported": json_records,
        "blank_lines_imported": blank_lines,
        "legacy_state_bytes": (source / "STATE.md").stat().st_size,
        "legacy_state_level2_headings": sum(
            1 for line in state_text.splitlines() if line.startswith("## ")
        ),
        "legacy_state_sha256": before["STATE.md"],
        "invalid_lines": invalid_lines,
        "unique_legacy_statuses": len(statuses),
        "legacy_status_counts": dict(statuses),
        "mapping_path": mapping_path.resolve().as_posix() if mapping_path else None,
        "champion_inference_performed": False,
        "created_at": utc_now(),
    }
    write_json_atomic(destination / "reports" / "archive" / "v1" / "migration_report.json", report)
    materialize_state(destination)
    return report


def _glob_files(source: Path, pattern: str) -> list[Path]:
    matches: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if fnmatch.fnmatch(relative, pattern) or ("/" not in pattern and fnmatch.fnmatch(path.name, pattern)):
            matches.append(path)
    return sorted(matches, key=lambda item: item.as_posix())


def _validate_tabular(source: Path, check: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    warnings: list[str] = []
    details: list[dict[str, Any]] = []
    pattern = str(check.get("glob", ""))
    if not pattern:
        return ["tabular check is missing glob"], warnings, details
    matched = _glob_files(source, pattern)
    if not matched and check.get("required", True):
        errors.append(f"tabular check matched no files: {pattern}")
        return errors, warnings, details
    format_name = str(check.get("format", "csv")).lower()
    delimiter = "\t" if format_name == "tsv" else str(check.get("delimiter", ","))
    encoding = str(check.get("encoding", "utf-8-sig"))
    required_columns = [str(item) for item in check.get("required_columns", [])]
    primary_key = [str(item) for item in check.get("primary_key", [])]
    min_rows = int(check.get("min_rows", 0))
    max_rows = check.get("max_rows")
    missing_policy = str(check.get("missing_policy", "allow"))
    if missing_policy not in {"allow", "forbid"}:
        return [f"tabular check has invalid missing_policy: {missing_policy}"], warnings, details
    allowed_missing_columns = {str(item) for item in check.get("allowed_missing_columns", [])}
    max_missing_fraction = {
        str(key): float(value) for key, value in check.get("max_missing_fraction", {}).items()
    }
    for path in matched:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                columns = reader.fieldnames or []
                missing = [column for column in required_columns if column not in columns]
                if missing:
                    errors.append(f"{path}: missing columns: {', '.join(missing)}")
                seen: set[tuple[str, ...]] = set()
                duplicate_keys = 0
                missing_primary_keys = 0
                missing_counts = {column: 0 for column in columns}
                row_count = 0
                for row in reader:
                    row_count += 1
                    for column in columns:
                        if row.get(column) in {None, ""}:
                            missing_counts[column] += 1
                    if primary_key and all(column in row for column in primary_key):
                        key = tuple(str(row.get(column, "")) for column in primary_key)
                        if any(not value for value in key):
                            missing_primary_keys += 1
                        if key in seen:
                            duplicate_keys += 1
                        seen.add(key)
                if row_count < min_rows:
                    errors.append(f"{path}: row count {row_count} is below {min_rows}")
                if isinstance(max_rows, int) and row_count > max_rows:
                    errors.append(f"{path}: row count {row_count} exceeds {max_rows}")
                if duplicate_keys:
                    errors.append(f"{path}: duplicate primary keys: {duplicate_keys}")
                if missing_primary_keys:
                    errors.append(f"{path}: rows with missing primary keys: {missing_primary_keys}")
                if missing_policy == "forbid":
                    forbidden_missing = {
                        column: count
                        for column, count in missing_counts.items()
                        if count and column not in allowed_missing_columns
                    }
                    if forbidden_missing:
                        errors.append(f"{path}: forbidden missing values: {forbidden_missing}")
                for column, limit in max_missing_fraction.items():
                    if column not in columns:
                        errors.append(f"{path}: max_missing_fraction references unknown column: {column}")
                        continue
                    observed = missing_counts[column] / row_count if row_count else 0.0
                    if observed > limit + 1e-12:
                        errors.append(
                            f"{path}: missing fraction for {column} is {observed:.12g}, above {limit:.12g}"
                        )
                details.append(
                    {
                        "path": path.relative_to(source).as_posix(),
                        "rows": row_count,
                        "columns": columns,
                        "duplicate_primary_keys": duplicate_keys,
                        "missing_primary_keys": missing_primary_keys,
                        "missing_counts": missing_counts,
                    }
                )
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f"{path}: failed tabular validation: {exc}")
    return errors, warnings, details


def _parse_contract_time(value: str, format_string: str | None) -> datetime:
    if format_string:
        parsed = datetime.strptime(value, format_string)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_temporal(
    source: Path, check: dict[str, Any], default_max: str | None
) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    details: list[dict[str, Any]] = []
    pattern = str(check.get("glob", ""))
    column = str(check.get("column", ""))
    maximum = check.get("max_value", default_max)
    if not pattern or not column or not maximum:
        return ["temporal check requires glob, column, and max_value or rules.max_business_date"], details
    matched = _glob_files(source, pattern)
    if not matched and check.get("required", True):
        return [f"temporal check matched no files: {pattern}"], details
    delimiter = "\t" if str(check.get("format", "csv")).lower() == "tsv" else str(check.get("delimiter", ","))
    encoding = str(check.get("encoding", "utf-8-sig"))
    format_string = check.get("datetime_format")
    try:
        maximum_time = _parse_contract_time(str(maximum), str(format_string) if format_string else None)
    except ValueError as exc:
        return [f"temporal check max value cannot be parsed: {maximum}: {exc}"], details
    for path in matched:
        observed_max: datetime | None = None
        violations = 0
        parse_failures = 0
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if column not in (reader.fieldnames or []):
                errors.append(f"{path}: temporal column is missing: {column}")
                continue
            for row in reader:
                raw = str(row.get(column, "")).strip()
                if not raw:
                    continue
                try:
                    observed = _parse_contract_time(raw, str(format_string) if format_string else None)
                except ValueError:
                    parse_failures += 1
                    continue
                observed_max = observed if observed_max is None or observed > observed_max else observed_max
                violations += observed > maximum_time
        if parse_failures:
            errors.append(f"{path}: unparseable temporal values in {column}: {parse_failures}")
        if violations:
            errors.append(f"{path}: {violations} values in {column} exceed {maximum}")
        details.append(
            {
                "path": path.relative_to(source).as_posix(),
                "column": column,
                "maximum_allowed": maximum,
                "maximum_observed": observed_max.isoformat() if observed_max else None,
                "violations": violations,
                "parse_failures": parse_failures,
            }
        )
    return errors, details


def _run_validator_hook(
    workspace: Path,
    source: Path,
    hook: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    relative_path = hook.get("path")
    function_name = hook.get("function", "validate")
    if not isinstance(relative_path, str) or not relative_path:
        raise OpsError("validator hook path must be a non-empty string")
    hook_path = (workspace / relative_path).resolve()
    try:
        hook_path.relative_to(workspace.resolve())
    except ValueError as exc:
        raise OpsError(f"validator hook escapes workspace: {relative_path}") from exc
    if not hook_path.is_file():
        raise OpsError(f"validator hook does not exist: {hook_path}")
    module_name = f"kaggle_validator_{hash_json({'path': hook_path.as_posix()})[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, hook_path)
    if spec is None or spec.loader is None:
        raise OpsError(f"cannot load validator hook: {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise OpsError(f"validator hook function is not callable: {function_name}")
    result = function(source=source, workspace=workspace, contract=contract)
    if not isinstance(result, dict):
        raise OpsError(f"validator hook must return an object: {hook_path}")
    result.setdefault("errors", [])
    result.setdefault("warnings", [])
    result.setdefault("details", {})
    return result


def ingest_data(workspace: Path, source: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    source = source.resolve()
    if not source.is_dir():
        raise OpsError(f"data source is not a directory: {source}")
    competition = load_and_validate_competition(workspace)
    contract = competition["input_contract"]
    all_files = sorted((path for path in source.rglob("*") if path.is_file()), key=lambda item: item.as_posix())
    records_before = [file_record(path, source) for path in all_files]
    errors: list[str] = []
    warnings: list[str] = []
    for pattern in contract.get("required_globs", []):
        if not _glob_files(source, str(pattern)):
            errors.append(f"required glob matched no files: {pattern}")
    for pattern in contract.get("forbidden_globs", []):
        matches = _glob_files(source, str(pattern))
        if matches:
            errors.append(f"forbidden glob matched {len(matches)} files: {pattern}")
    min_files = int(contract.get("min_files", 0))
    max_files = contract.get("max_files")
    if len(all_files) < min_files:
        errors.append(f"file count {len(all_files)} is below {min_files}")
    if isinstance(max_files, int) and len(all_files) > max_files:
        errors.append(f"file count {len(all_files)} exceeds {max_files}")

    tabular_details: list[dict[str, Any]] = []
    for check in contract.get("tabular_checks", []):
        if not isinstance(check, dict):
            errors.append("tabular check must be an object")
            continue
        item_errors, item_warnings, details = _validate_tabular(source, check)
        errors.extend(item_errors)
        warnings.extend(item_warnings)
        tabular_details.extend(details)

    temporal_details: list[dict[str, Any]] = []
    for check in contract.get("temporal_checks", []):
        if not isinstance(check, dict):
            errors.append("temporal check must be an object")
            continue
        item_errors, details = _validate_temporal(
            source, check, competition.get("rules", {}).get("max_business_date")
        )
        errors.extend(item_errors)
        temporal_details.extend(details)

    hook_results: list[dict[str, Any]] = []
    for hook in contract.get("validator_hooks", []):
        if not isinstance(hook, dict):
            errors.append("validator hook must be an object")
            continue
        try:
            result = _run_validator_hook(workspace, source, hook, contract)
            errors.extend(str(item) for item in result.get("errors", []))
            warnings.extend(str(item) for item in result.get("warnings", []))
            hook_results.append(result)
        except Exception as exc:  # noqa: BLE001 - hook failures are validation failures.
            errors.append(f"validator hook failed: {exc}")

    files_after = sorted((path for path in source.rglob("*") if path.is_file()), key=lambda item: item.as_posix())
    records = [file_record(path, source) for path in files_after]
    source_mutation_detected = records != records_before
    if source_mutation_detected:
        errors.append("data source was modified while validation hooks were running")
    hook_files: list[dict[str, Any]] = []
    for hook in contract.get("validator_hooks", []):
        if not isinstance(hook, dict) or not isinstance(hook.get("path"), str):
            continue
        hook_path = (workspace / hook["path"]).resolve()
        if hook_path.is_file():
            hook_files.append(file_record(hook_path, workspace))
    snapshot_material = {
        "source": source.as_posix(),
        "contract_sha256": hash_json(contract),
        "files": records,
        "tabular_details": tabular_details,
        "temporal_details": temporal_details,
        "validator_hook_files": hook_files,
        "validator_hook_results": hook_results,
        "errors": errors,
        "warnings": warnings,
    }
    snapshot_id = hash_json(snapshot_material)[:20]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_snapshot_id": snapshot_id,
        "source": source.as_posix(),
        "source_is_read_only_input": not source_mutation_detected,
        "source_mutation_detected": source_mutation_detected,
        "files_before_validation": records_before,
        "contract_sha256": snapshot_material["contract_sha256"],
        "file_count": len(records),
        "files": records,
        "tabular_details": tabular_details,
        "temporal_details": temporal_details,
        "validator_hook_results": hook_results,
        "validator_hook_files": hook_files,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "created_at": utc_now(),
    }
    manifest_path = workspace / "data" / "manifests" / f"{snapshot_id}.json"
    if manifest_path.is_file():
        existing = read_json(manifest_path)
        existing_stable = {key: value for key, value in existing.items() if key != "created_at"}
        proposed_stable = {key: value for key, value in manifest.items() if key != "created_at"}
        if existing_stable != proposed_stable:
            raise OpsError(f"data snapshot identity collision: {snapshot_id}")
        if existing.get("valid") is not True:
            raise OpsError(
                "data ingest failed:\n- " + "\n- ".join(str(item) for item in existing.get("errors", []))
            )
        return existing
    write_json_atomic(manifest_path, manifest)
    append_event_checked(
        workspace,
        event(
            "data_ingested",
            {
                "manifest": manifest_path.relative_to(workspace).as_posix(),
                "valid": not errors,
                "file_count": len(records),
                "errors": errors,
            },
            data_snapshot_id=snapshot_id,
        ),
    )
    materialize_state(workspace)
    if errors:
        raise OpsError("data ingest failed:\n- " + "\n- ".join(errors))
    return manifest


def _idea_state(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ideas: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("event_type") not in {"idea_created", "idea_updated"}:
            continue
        idea_id = str(item.get("idea_id"))
        current = ideas.get(idea_id, {})
        current.update(item.get("payload", {}))
        current.update(
            {
                "idea_id": idea_id,
                "track_id": item.get("track_id"),
                "phase_id": item.get("phase_id"),
                "experiment_family": item.get("experiment_family"),
                "updated_at": item.get("occurred_at"),
            }
        )
        ideas[idea_id] = current
    return ideas


def materialize_state(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    competition = load_and_validate_competition(workspace)
    events = load_events(workspace)
    champions = read_json(workspace / "champions.json")
    champion_errors = validate_champions(champions)
    if champion_errors:
        raise OpsError("invalid champions.json:\n- " + "\n- ".join(champion_errors))
    ideas = _idea_state(events)
    open_ideas = [item for item in ideas.values() if item.get("status") == "open"]

    run_latest: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("run_id") and item.get("event_type", "").startswith("run_"):
            run_latest[str(item["run_id"])] = item
    active_runs = [
        item for item in run_latest.values() if item.get("payload", {}).get("status") in {"planned", "running"}
    ]
    flags = sorted(path.name for path in (workspace / "flags").glob("*.flag"))
    generated_at = utc_now()

    best_metrics: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for item in events:
        metric = item.get("payload", {}).get("metric")
        if not isinstance(metric, dict):
            continue
        key = (
            str(item.get("track_id", "")),
            str(item.get("phase_id", "")),
            str(item.get("data_snapshot_id", "")),
            str(metric.get("source", "")),
            str(metric.get("comparable_group", "")),
            str(metric.get("name", "")),
        )
        current = best_metrics.get(key)
        if current is None:
            best_metrics[key] = {"metric": metric, "run_id": item.get("run_id")}
            continue
        observed = float(metric["value"])
        incumbent = float(current["metric"]["value"])
        if (metric["direction"] == "higher" and observed > incumbent) or (
            metric["direction"] == "lower" and observed < incumbent
        ):
            best_metrics[key] = {"metric": metric, "run_id": item.get("run_id")}

    lines = [
        "# Competition State",
        "",
        f"- Competition: {competition['competition']['slug']}",
        f"- Platform: {competition['competition']['platform']}",
        f"- Problem type: {competition['competition']['problem_type']}",
        f"- Updated at: {generated_at}",
        f"- Events: {len(events)}",
        f"- Open ideas: {len(open_ideas)}",
        f"- Active runs: {len(active_runs)}",
        f"- Flags: {', '.join(flags) if flags else 'none'}",
        "",
        "## Current Champions",
        "",
    ]
    if champions["lanes"]:
        for key, lane in sorted(champions["lanes"].items()):
            metric = lane.get("metric", {})
            metric_text = f"{metric.get('name')}={metric.get('value')} ({metric.get('source')})" if metric else "unscored"
            source_text = "unknown"
            run_manifest_path = workspace / "runs" / str(lane.get("champion_run_id")) / "run-manifest.json"
            if run_manifest_path.is_file():
                run_manifest = read_json(run_manifest_path)
                source_origin = run_manifest.get("source_root") or "command-only"
                source_digest = str(run_manifest.get("source_sha256") or "unhashed")[:12]
                source_text = f"{source_origin}@{source_digest}"
            lines.append(
                f"- {key}: candidate={lane.get('champion_candidate_id')} run={lane.get('champion_run_id')} "
                f"metric={metric_text} code={source_text} "
                f"rollback={lane.get('rollback_candidate_id') or 'none'} "
                f"challengers={','.join(lane.get('challenger_candidate_ids', [])) or 'none'}"
            )
    else:
        lines.append("- No champion has been explicitly promoted.")

    lines.extend(["", "## Active Runs", ""])
    if active_runs:
        for item in active_runs[:20]:
            lines.append(
                f"- {item.get('run_id')}: {item.get('payload', {}).get('status')} "
                f"track={item.get('track_id')} family={item.get('experiment_family')}"
            )
    else:
        lines.append("- None.")

    lines.extend(["", "## Best Comparable Metrics", ""])
    if best_metrics:
        for key, value in sorted(best_metrics.items())[:20]:
            track_id, phase_id, snapshot_id, source, group, metric_name = key
            lines.append(
                f"- {track_id}/{phase_id} data={snapshot_id} source={source} group={group}: "
                f"{metric_name}={value['metric']['value']} run={value['run_id']}"
            )
    else:
        lines.append("- No typed metric has been recorded.")

    lines.extend(["", "## Next Actions", ""])
    if flags:
        lines.append(f"- Resolve flags: {', '.join(flags)}")
    for item in sorted(open_ideas, key=lambda value: (value.get("priority") != "high", value.get("updated_at", "")))[:10]:
        lines.append(
            f"- {item['idea_id']} [{item.get('priority')}]: {item.get('hypothesis', '')} "
            f"(track={item.get('track_id')}, family={item.get('experiment_family') or 'general'})"
        )
    if not flags and not open_ideas:
        lines.append("- No queued action. Review the competition profile or create a new evidence-backed idea.")

    lines.extend(["", "## Recent Events", ""])
    for item in events[-20:]:
        label = item.get("run_id") or item.get("idea_id") or item.get("payload", {}).get("candidate_id") or ""
        lines.append(f"- {item.get('occurred_at')} {item.get('event_type')} {label}".rstrip())
    if len(lines) > 200:
        lines = lines[:199] + ["- State output truncated; query ledger/events.jsonl for full history."]
    state_text = "\n".join(lines) + "\n"
    (workspace / "STATE.md").write_text(state_text, encoding="utf-8", newline="\n")
    archive_path = workspace / "reports" / "archive" / "state" / f"{generated_at[:10]}.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(state_text, encoding="utf-8", newline="\n")

    backlog_lines = ["# Ideas Backlog", ""]
    for item in sorted(ideas.values(), key=lambda value: value.get("updated_at", "")):
        checked = " " if item.get("status") == "open" else "x"
        backlog_lines.extend(
            [
                f"- [{checked}] id: {item['idea_id']}",
                f"  track: {item.get('track_id')}",
                f"  phase: {item.get('phase_id')}",
                f"  family: {item.get('experiment_family') or 'general'}",
                f"  priority: {item.get('priority')}",
                f"  status: {item.get('status')}",
                f"  hypothesis: {item.get('hypothesis', '')}",
            ]
        )
    (workspace / "ideas_backlog.md").write_text(
        "\n".join(backlog_lines) + "\n", encoding="utf-8", newline="\n"
    )

    status = {
        "schema_version": SCHEMA_VERSION,
        "competition": competition["competition"],
        "events": len(events),
        "open_ideas": len(open_ideas),
        "active_runs": [item.get("run_id") for item in active_runs],
        "flags": flags,
        "champion_lanes": len(champions["lanes"]),
        "best_metric_groups": len(best_metrics),
        "state_lines": len(lines),
        "updated_at": generated_at,
    }
    write_json_atomic(workspace / "reports" / "current" / "status.json", status)
    return status


def validate_workspace(workspace: Path, *, deep: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not workspace.is_dir():
        return {"ok": False, "errors": [f"workspace does not exist: {workspace}"], "warnings": []}
    missing_dirs = [directory for directory in WORKSPACE_DIRS if not (workspace / directory).is_dir()]
    errors.extend(f"missing directory: {item}" for item in missing_dirs)
    if not (workspace / "rules" / "rules.json").is_file():
        errors.append("missing rules snapshot: rules/rules.json")
    try:
        competition = read_json(workspace / "competition.json")
        errors.extend(validate_competition(competition))
        rules_path = workspace / "rules" / "rules.json"
        if rules_path.is_file() and read_json(rules_path) != competition.get("rules"):
            errors.append("rules/rules.json differs from competition.json.rules")
    except OpsError as exc:
        errors.append(str(exc))
    try:
        champions = read_json(workspace / "champions.json")
        errors.extend(validate_champions(champions))
    except OpsError as exc:
        errors.append(str(exc))
    try:
        events = load_events(workspace)
        errors.extend(validate_event_history(events))
    except OpsError as exc:
        events = []
        errors.append(str(exc))

    run_manifests = list((workspace / "runs").glob("*/run-manifest.json")) if (workspace / "runs").exists() else []
    run_ids: set[str] = set()
    for path in run_manifests:
        try:
            manifest = read_json(path)
            run_id = manifest.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                errors.append(f"{path}: missing run_id")
            elif run_id in run_ids:
                errors.append(f"duplicate run_id in manifests: {run_id}")
            else:
                run_ids.add(run_id)
            if manifest.get("status") not in {"planned", "running", "completed", "failed", "invalid"}:
                errors.append(f"{path}: invalid status: {manifest.get('status')}")
            if manifest.get("status") in {"completed", "failed", "invalid"}:
                seal_path = path.with_name("run-manifest.sha256.json")
                if not seal_path.is_file():
                    errors.append(f"{path}: terminal run manifest seal is missing")
                else:
                    seal = read_json(seal_path)
                    if seal.get("sha256") != sha256_file(path):
                        errors.append(f"{path}: terminal run manifest seal mismatch")
            if deep:
                for artifact in manifest.get("artifacts", []):
                    artifact_path = Path(str(artifact.get("store_path", "")))
                    if not artifact_path.is_file():
                        errors.append(f"{path}: missing artifact: {artifact_path}")
                    elif sha256_file(artifact_path) != artifact.get("sha256"):
                        errors.append(f"{path}: artifact hash mismatch: {artifact_path}")
        except OpsError as exc:
            errors.append(str(exc))

    if deep:
        for path in (workspace / "reports" / "current").glob("gate_*.json"):
            if path.name.endswith(".seal.json"):
                continue
            seal_path = path.with_suffix(path.suffix + ".seal.json")
            if not seal_path.is_file():
                errors.append(f"{path}: gate report seal is missing")
                continue
            try:
                seal = read_json(seal_path)
                if seal.get("sha256") != sha256_file(path):
                    errors.append(f"{path}: gate report seal mismatch")
            except OpsError as exc:
                errors.append(str(exc))

    if "champions" in locals() and isinstance(champions, dict):
        promotions: dict[str, list[dict[str, Any]]] = {}
        for item in events:
            if item.get("event_type") != "candidate_promoted":
                continue
            key = lane_key(
                str(item.get("track_id", "")),
                str(item.get("phase_id", "")),
                str(item.get("data_snapshot_id", "")),
            )
            promotions.setdefault(key, []).append(item)
        champion_lanes = champions.get("lanes", {})
        for key in sorted(set(champion_lanes) | set(promotions)):
            lane = champion_lanes.get(key)
            lane_promotions = promotions.get(key, [])
            context = f"champions.json.lanes[{key}]"
            if not lane_promotions:
                errors.append(f"{context}: no candidate_promoted event exists")
                continue
            if not isinstance(lane, dict):
                errors.append(f"{context}: latest promotion is not materialized")
                continue
            latest = lane_promotions[-1]
            payload = latest.get("payload", {})
            expected = {
                "track_id": latest.get("track_id"),
                "phase_id": latest.get("phase_id"),
                "data_snapshot_id": latest.get("data_snapshot_id"),
                "champion_candidate_id": payload.get("candidate_id"),
                "champion_run_id": latest.get("run_id"),
                "rollback_candidate_id": payload.get("previous_candidate_id"),
                "metric": payload.get("metric"),
            }
            for field, expected_value in expected.items():
                if lane.get(field) != expected_value:
                    errors.append(
                        f"{context}: {field} differs from latest promotion event"
                    )
            if lane.get("rollback_candidate_id") == lane.get("champion_candidate_id"):
                errors.append(f"{context}: rollback candidate cannot equal champion")

    if (workspace / "STATE.md").exists():
        state_lines = len((workspace / "STATE.md").read_text(encoding="utf-8").splitlines())
        if state_lines > 200:
            errors.append(f"STATE.md exceeds 200 lines: {state_lines}")
    else:
        warnings.append("STATE.md has not been materialized")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "events": len(events),
        "run_manifests": len(run_manifests),
    }


def create_or_update_idea(
    workspace: Path,
    *,
    idea_id: str,
    track_id: str,
    phase_id: str,
    experiment_family: str,
    priority: str,
    status: str,
    hypothesis: str,
    validation: str = "",
    failure_mode: str = "",
) -> dict[str, Any]:
    ensure_identifier(idea_id, "idea_id")
    ensure_identifier(experiment_family, "experiment_family")
    competition = load_and_validate_competition(workspace)
    get_track(competition, track_id)
    get_phase(competition, phase_id)
    events = load_events(workspace)
    exists = any(item.get("event_type") == "idea_created" and item.get("idea_id") == idea_id for item in events)
    item = event(
        "idea_updated" if exists else "idea_created",
        {
            "hypothesis": hypothesis,
            "priority": priority,
            "status": status,
            "validation": validation,
            "failure_mode": failure_mode,
        },
        track_id=track_id,
        phase_id=phase_id,
        idea_id=idea_id,
        experiment_family=experiment_family,
    )
    append_event_checked(workspace, item)
    materialize_state(workspace)
    return item

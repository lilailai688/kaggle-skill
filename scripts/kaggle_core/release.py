from __future__ import annotations

import fnmatch
import math
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from .common import SCHEMA_VERSION, OpsError, event, hash_json, read_json, sha256_file, utc_now, write_json_atomic
from .schema import append_event_checked, load_and_validate_competition, load_events
from .workspace import materialize_state


def _safe_member(name: str) -> bool:
    member = PurePosixPath(name)
    return (
        not member.is_absolute()
        and ".." not in member.parts
        and "" not in member.parts
        and not (member.parts and member.parts[0].endswith(":"))
    )


def _selected_files(source: Path, patterns: list[str], excludes: list[str]) -> tuple[list[Path], list[str]]:
    files = sorted((path for path in source.rglob("*") if path.is_file() and not path.is_symlink()), key=lambda p: p.as_posix())
    selected: list[Path] = []
    excluded: list[str] = []
    for path in files:
        relative = path.relative_to(source).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
            excluded.append(relative)
            continue
        if not patterns or any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            selected.append(path)
    return selected, excluded


def build_release(
    workspace: Path,
    *,
    source: Path,
    output: Path | None = None,
    includes: list[str] | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    source = source.resolve()
    if not source.is_dir():
        raise OpsError(f"release source is not a directory: {source}")
    competition = load_and_validate_competition(workspace)
    patterns = includes if includes is not None else competition["output_contract"].get("release_includes", [])
    excludes = [str(item) for item in competition["output_contract"].get("release_excludes", [])]
    files, excluded = _selected_files(source, [str(item) for item in patterns], excludes)
    if not files:
        raise OpsError("release selection contains no files")
    if output is None:
        output = workspace / "release" / f"{competition['competition']['slug']}-release.zip"
    elif not output.is_absolute():
        output = workspace / output
    output = output.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise OpsError("release output must be outside the release source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    members: list[dict[str, Any]] = []
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            if not _safe_member(relative):
                raise OpsError(f"unsafe release path: {relative}")
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            data = path.read_bytes()
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            members.append({"path": relative, "size": len(data), "sha256": sha256_file(path)})
    temporary.replace(output)
    verification = verify_release(workspace, archive_path=output, expected_members=members, record_event=False)
    if not verification["passed"]:
        raise OpsError("release verification failed:\n- " + "\n- ".join(verification["errors"]))
    champions = read_json(workspace / "champions.json")
    champion_run_ids = sorted(
        {
            str(lane.get("champion_run_id"))
            for lane in champions.get("lanes", {}).values()
            if lane.get("champion_run_id")
        }
    )
    runtime_provenance: list[dict[str, Any]] = []
    for run_id in champion_run_ids:
        run_manifest_path = workspace / "runs" / run_id / "run-manifest.json"
        if not run_manifest_path.is_file():
            raise OpsError(f"champion run manifest is missing: {run_id}")
        run_manifest = read_json(run_manifest_path)
        run_seal_path = run_manifest_path.with_name("run-manifest.sha256.json")
        if (
            not run_seal_path.is_file()
            or read_json(run_seal_path).get("sha256") != sha256_file(run_manifest_path)
        ):
            raise OpsError(f"champion run manifest seal is invalid: {run_id}")
        runtime_provenance.append(
            {
                "run_id": run_id,
                "identity_sha256": run_manifest.get("identity_sha256"),
                "source_sha256": run_manifest.get("source_sha256"),
                "environment_sha256": run_manifest.get("identity", {}).get(
                    "environment_sha256"
                ),
                "container": run_manifest.get("container"),
                "packages": run_manifest.get("environment", {}).get("packages", []),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "archive": output.as_posix(),
        "archive_sha256": sha256_file(output),
        "source": source.as_posix(),
        "include_patterns": patterns,
        "exclude_patterns": excludes,
        "excluded_paths": excluded,
        "members": members,
        "member_set_sha256": hash_json(members),
        "runtime_provenance": runtime_provenance,
        "created_at": utc_now(),
        "verification": verification,
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(
        manifest_path.with_suffix(manifest_path.suffix + ".seal.json"),
        {"sha256": sha256_file(manifest_path), "sealed_at": manifest["created_at"]},
    )
    append_event_checked(
        workspace,
        event(
            "release_built",
            {
                "archive": output.as_posix(),
                "archive_sha256": manifest["archive_sha256"],
                "manifest": manifest_path.as_posix(),
                "member_count": len(members),
            },
        ),
    )
    materialize_state(workspace)
    return manifest


def verify_release(
    workspace: Path,
    *,
    archive_path: Path,
    expected_members: list[dict[str, Any]] | None = None,
    expected_archive_sha256: str | None = None,
    expected_member_set_sha256: str | None = None,
    record_event: bool = False,
) -> dict[str, Any]:
    del record_event  # Verification is intentionally read-only.
    workspace = workspace.resolve()
    load_and_validate_competition(workspace)
    archive_path = archive_path.resolve()
    errors: list[str] = []
    observed: list[dict[str, Any]] = []
    if not archive_path.is_file():
        return {"passed": False, "errors": [f"release archive does not exist: {archive_path}"], "members": []}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad = archive.testzip()
            if bad:
                errors.append(f"ZIP CRC failure: {bad}")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if not _safe_member(info.filename):
                    errors.append(f"unsafe ZIP member: {info.filename}")
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) == 0o120000:
                    errors.append(f"ZIP symlink is not allowed: {info.filename}")
                    continue
                data = archive.read(info.filename)
                import hashlib

                observed.append(
                    {"path": info.filename, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                )
            names = [item["path"] for item in observed]
            duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
            errors.extend(f"duplicate ZIP member: {name}" for name in duplicates)
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"invalid ZIP: {exc}")
    observed.sort(key=lambda item: item["path"])
    if expected_members is not None:
        expected = sorted(expected_members, key=lambda item: item["path"])
        if observed != expected:
            errors.append("release member set, size, or SHA256 differs from the manifest")
    observed_archive_sha256 = sha256_file(archive_path) if archive_path.is_file() else None
    if expected_archive_sha256 is not None and observed_archive_sha256 != expected_archive_sha256:
        errors.append("release archive SHA256 differs from the manifest")
    observed_member_set_sha256 = hash_json(observed)
    if (
        expected_member_set_sha256 is not None
        and observed_member_set_sha256 != expected_member_set_sha256
    ):
        errors.append("release member-set SHA256 differs from the manifest")
    return {
        "passed": not errors,
        "archive": archive_path.as_posix(),
        "archive_sha256": observed_archive_sha256,
        "member_set_sha256": observed_member_set_sha256,
        "members": observed,
        "errors": errors,
        "checked_at": utc_now(),
    }


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys))
    return numerator / denominator if denominator else None


def build_postmortem(
    workspace: Path,
    *,
    final_result: str = "",
    final_rank: str = "",
    reusable_components: list[str] | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    competition = load_and_validate_competition(workspace)
    events = load_events(workspace)
    champions = read_json(workspace / "champions.json")
    event_counts = Counter(str(item.get("event_type")) for item in events)
    run_statuses = Counter(
        str(item.get("payload", {}).get("status"))
        for item in events
        if item.get("event_type") in {"run_completed", "run_failed"}
    )
    family_statuses: dict[str, Counter[str]] = defaultdict(Counter)
    for item in events:
        if item.get("event_type") in {"run_completed", "run_failed"}:
            family_statuses[str(item.get("experiment_family", "unknown"))][
                str(item.get("payload", {}).get("status"))
            ] += 1

    candidate_metrics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        metric = item.get("payload", {}).get("metric")
        candidate_id = item.get("payload", {}).get("candidate_id")
        if isinstance(metric, dict) and candidate_id:
            candidate_metrics[str(candidate_id)].append(
                {
                    "metric": metric,
                    "track_id": item.get("track_id"),
                    "phase_id": item.get("phase_id"),
                    "data_snapshot_id": item.get("data_snapshot_id"),
                }
            )
    reliability: list[dict[str, Any]] = []
    groups: dict[tuple[Any, ...], tuple[list[float], list[float]]] = {}
    for observations in candidate_metrics.values():
        locals_by_group = {
            (
                item["track_id"],
                item["phase_id"],
                item["data_snapshot_id"],
                item["metric"]["name"],
                item["metric"]["direction"],
                item["metric"]["scope"],
                item["metric"]["comparable_group"],
            ): float(item["metric"]["value"])
            for item in observations
            if item["metric"].get("source") == "local_proxy"
        }
        lb_by_group = {
            (
                item["track_id"],
                item["phase_id"],
                item["data_snapshot_id"],
                item["metric"]["name"],
                item["metric"]["direction"],
                item["metric"]["scope"],
                item["metric"]["comparable_group"],
            ): float(item["metric"]["value"])
            for item in observations
            if item["metric"].get("source") in {"public_lb", "private_lb", "final_result"}
        }
        for key in set(locals_by_group) & set(lb_by_group):
            xs, ys = groups.setdefault(key, ([], []))
            xs.append(locals_by_group[key])
            ys.append(lb_by_group[key])
    for key, (xs, ys) in sorted(groups.items()):
        track_id, phase_id, snapshot_id, name, direction, scope, comparable_group = key
        reliability.append(
            {
                "track_id": track_id,
                "phase_id": phase_id,
                "data_snapshot_id": snapshot_id,
                "metric_name": name,
                "direction": direction,
                "scope": scope,
                "comparable_group": comparable_group,
                "paired_candidates": len(xs),
                "local_proxy_lb_pearson": _pearson(xs, ys),
            }
        )

    phase_usage: dict[str, dict[str, Any]] = {}
    for phase in competition["phases"]:
        phase_id = str(phase["id"])
        used = len({
            str(item.get("payload", {}).get("candidate_id"))
            for item in events
            if item.get("event_type") == "leaderboard_feedback_recorded" and item.get("phase_id") == phase_id
            and item.get("payload", {}).get("candidate_id")
        })
        phase_usage[phase_id] = {
            "used": used,
            "limit": phase["submission_limit"],
            "remaining": max(int(phase["submission_limit"]) - used, 0),
        }

    run_manifests: dict[str, dict[str, Any]] = {}
    reproducibility_errors: list[str] = []
    for path in sorted((workspace / "runs").glob("*/run-manifest.json")):
        manifest = read_json(path)
        run_id = str(manifest.get("run_id", path.parent.name))
        run_manifests[run_id] = manifest
        if manifest.get("status") != "completed":
            continue
        seal_path = path.with_name("run-manifest.sha256.json")
        if not seal_path.is_file() or read_json(seal_path).get("sha256") != sha256_file(path):
            reproducibility_errors.append(f"completed run has invalid manifest seal: {run_id}")
        for artifact in manifest.get("artifacts", []):
            stored = Path(str(artifact.get("store_path", "")))
            if not stored.is_file() or sha256_file(stored) != artifact.get("sha256"):
                reproducibility_errors.append(f"completed run has invalid stored artifact: {run_id}")

    champion_routes: dict[str, list[str]] = {}
    for lane, champion in champions["lanes"].items():
        route: list[str] = []
        run_id = str(champion.get("champion_run_id", ""))
        seen: set[str] = set()
        while run_id and run_id not in seen:
            seen.add(run_id)
            route.append(run_id)
            run_id = str(run_manifests.get(run_id, {}).get("parent_run_id") or "")
        champion_routes[lane] = list(reversed(route))

    failed_routes = [
        {
            "run_id": item.get("run_id"),
            "experiment_family": item.get("experiment_family"),
            "status": item.get("payload", {}).get("status"),
            "reason": item.get("payload", {}).get("failure_reason"),
        }
        for item in events
        if item.get("event_type") in {"run_completed", "run_failed"}
        and item.get("payload", {}).get("status") in {"failed", "invalid"}
    ]
    release_events = [item for item in events if item.get("event_type") == "release_built"]
    for item in release_events:
        payload = item.get("payload", {})
        archive = Path(str(payload.get("archive", "")))
        manifest_path = Path(str(payload.get("manifest", "")))
        seal_path = manifest_path.with_suffix(manifest_path.suffix + ".seal.json")
        if not manifest_path.is_file() or not seal_path.is_file():
            reproducibility_errors.append(f"release manifest or seal is missing: {archive}")
            continue
        release_manifest = read_json(manifest_path)
        if read_json(seal_path).get("sha256") != sha256_file(manifest_path):
            reproducibility_errors.append(f"release manifest seal mismatch: {archive}")
        if not archive.is_file() or sha256_file(archive) != release_manifest.get("archive_sha256"):
            reproducibility_errors.append(f"release archive SHA256 mismatch: {archive}")
    report = {
        "schema_version": SCHEMA_VERSION,
        "competition": competition["competition"],
        "final_result": final_result,
        "final_rank": final_rank,
        "event_counts": dict(event_counts),
        "run_statuses": dict(run_statuses),
        "experiment_family_statuses": {key: dict(value) for key, value in family_statuses.items()},
        "submission_usage": phase_usage,
        "champions": champions["lanes"],
        "champion_routes": champion_routes,
        "failed_routes": failed_routes,
        "proxy_reliability": reliability,
        "reproducibility": {
            "completed_run_manifests": sum(
                manifest.get("status") == "completed" for manifest in run_manifests.values()
            ),
            "release_packages": len(release_events),
            "passed": not reproducibility_errors,
            "errors": reproducibility_errors,
        },
        "reusable_components": reusable_components or [],
        "created_at": utc_now(),
    }
    output_json = workspace / "reports" / "archive" / "postmortem.json"
    output_md = workspace / "reports" / "archive" / "postmortem.md"
    write_json_atomic(output_json, report)
    lines = [
        "# Competition Postmortem",
        "",
        f"- Competition: {competition['competition']['slug']}",
        f"- Platform: {competition['competition']['platform']}",
        f"- Problem type: {competition['competition']['problem_type']}",
        f"- Final result: {final_result or 'not recorded'}",
        f"- Final rank: {final_rank or 'not recorded'}",
        f"- Created at: {report['created_at']}",
        "",
        "## Run Outcomes",
        "",
    ]
    for status, count in sorted(run_statuses.items()):
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Champion Lanes", ""])
    if champions["lanes"]:
        for key, lane in sorted(champions["lanes"].items()):
            lines.append(
                f"- {key}: {lane.get('champion_candidate_id')} "
                f"rollback={lane.get('rollback_candidate_id') or 'none'}"
            )
    else:
        lines.append("- No candidate was explicitly promoted.")
    lines.extend(["", "## Submission Usage", ""])
    for phase_id, usage in phase_usage.items():
        lines.append(f"- {phase_id}: {usage['used']}/{usage['limit']} used, {usage['remaining']} remaining")
    lines.extend(["", "## Reproducibility", ""])
    lines.append(
        f"- Passed: {report['reproducibility']['passed']}; "
        f"completed runs={report['reproducibility']['completed_run_manifests']}; "
        f"releases={report['reproducibility']['release_packages']}"
    )
    lines.extend(f"- Error: {item}" for item in reproducibility_errors)
    lines.extend(["", "## Failed Routes", ""])
    if failed_routes:
        for item in failed_routes:
            lines.append(
                f"- {item['run_id']} family={item['experiment_family']} "
                f"status={item['status']} reason={item['reason']}"
            )
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Proxy Reliability", ""])
    if reliability:
        for item in reliability:
            lines.append(
                f"- {item['metric_name']} / {item['comparable_group']}: "
                f"pairs={item['paired_candidates']} pearson={item['local_proxy_lb_pearson']}"
            )
    else:
        lines.append("- Insufficient paired local-proxy and leaderboard observations.")
    lines.extend(["", "## Reusable Components", ""])
    lines.extend(f"- {item}" for item in (reusable_components or []))
    if not reusable_components:
        lines.append("- None recorded.")
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    append_event_checked(
        workspace,
        event(
            "postmortem_created",
            {"report_json": output_json.as_posix(), "report_md": output_md.as_posix()},
        ),
    )
    materialize_state(workspace)
    return report

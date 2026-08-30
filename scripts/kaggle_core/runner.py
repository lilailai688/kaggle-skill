from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import time
import uuid
import hashlib
import importlib.metadata
from pathlib import Path
from typing import Any

from .common import (
    SCHEMA_VERSION,
    OpsError,
    event,
    ensure_identifier,
    file_record,
    exclusive_lock,
    hash_json,
    hash_tree,
    read_json,
    sha256_file,
    store_artifact,
    utc_now,
    write_json_atomic,
)
from .schema import append_event_checked, get_phase, get_track, load_and_validate_competition, load_events
from .workspace import materialize_state


def _environment_fingerprint() -> dict[str, Any]:
    packages = sorted(
        {
            f"{distribution.metadata.get('Name', 'unknown')}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
        }
    )
    return {
        "python": sys.version,
        "executable": Path(sys.executable).resolve().as_posix(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "packages": packages,
    }


def _resolve_run_path(path: Path, run_dir: Path) -> Path:
    return path.resolve() if path.is_absolute() else (run_dir / path).resolve()


def _resolve_workspace_path(path: Path, workspace: Path) -> Path:
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _verify_data_source(snapshot: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    source = Path(str(snapshot.get("source", "")))
    if not source.is_dir():
        return [f"data source is missing: {source}"], []
    observed = [
        file_record(path, source)
        for path in sorted(
            (item for item in source.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        )
    ]
    expected = snapshot.get("files", [])
    if observed != expected:
        return ["data source files, sizes, or SHA256 differ from the ingested snapshot"], observed
    return [], observed


def _verify_completed_manifest(
    manifest: dict[str, Any], identity_sha256: str, manifest_path: Path
) -> list[str]:
    errors: list[str] = []
    if manifest.get("identity_sha256") != identity_sha256:
        errors.append("resume identity differs from the completed run")
    if manifest.get("status") != "completed":
        errors.append(f"run is not completed: {manifest.get('status')}")
    seal_path = manifest_path.with_name("run-manifest.sha256.json")
    if not seal_path.is_file():
        errors.append("completed run manifest seal is missing")
    else:
        seal = read_json(seal_path)
        if seal.get("sha256") != sha256_file(manifest_path):
            errors.append("completed run manifest was modified after it was sealed")
    for artifact in manifest.get("artifacts", []):
        store_path = Path(str(artifact.get("store_path", "")))
        if not store_path.is_file():
            errors.append(f"missing stored artifact: {store_path}")
        elif sha256_file(store_path) != artifact.get("sha256"):
            errors.append(f"stored artifact hash mismatch: {store_path}")
    return errors


def _git_provenance(source_root: Path | None) -> tuple[dict[str, Any] | None, bytes]:
    if source_root is None:
        return None, b""
    try:
        commit = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if commit.returncode != 0:
            return None, b""
        patch = subprocess.run(
            ["git", "-C", str(source_root), "diff", "HEAD", "--binary", "--no-ext-diff"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        patch_bytes = patch.stdout if patch.returncode == 0 else b""
        return (
            {
                "commit": commit.stdout.decode("ascii", errors="replace").strip(),
                "dirty": bool(patch_bytes),
                "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
            },
            patch_bytes,
        )
    except OSError:
        return None, b""


def _container_identity(image: str | None) -> dict[str, str] | None:
    if image is None:
        return None
    if not image.strip():
        raise OpsError("container_image must be non-empty")
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise OpsError(f"Docker is unavailable: {exc}") from exc
    image_id = completed.stdout.strip()
    if completed.returncode != 0 or not image_id:
        message = completed.stderr.strip() or "image inspection failed"
        raise OpsError(f"cannot resolve container image {image}: {message}")
    return {"image": image, "image_id": image_id}


def _docker_command(
    *,
    container: dict[str, str],
    command: str,
    run_dir: Path,
    data_root: Path,
    snapshot_path: Path,
    source_root: Path | None,
    config_path: Path | None,
    run_id: str,
    seed: int,
) -> list[str]:
    arguments = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=1g",
        "--mount",
        f"type=bind,source={run_dir},target=/workspace/run",
        "--mount",
        f"type=bind,source={data_root},target=/workspace/input,readonly",
        "--mount",
        f"type=bind,source={snapshot_path},target=/workspace/data-manifest.json,readonly",
        "--workdir",
        "/workspace/run",
    ]
    if source_root:
        arguments.extend(
            ["--mount", f"type=bind,source={source_root},target=/workspace/source,readonly"]
        )
    if config_path:
        arguments.extend(
            ["--mount", f"type=bind,source={config_path},target=/workspace/config.json,readonly"]
        )
    environment = {
        "KAGGLE_SKILL_RUN_ID": run_id,
        "KAGGLE_SKILL_RUN_DIR": "/workspace/run",
        "KAGGLE_SKILL_DATA_ROOT": "/workspace/input",
        "KAGGLE_SKILL_DATA_MANIFEST": "/workspace/data-manifest.json",
        "KAGGLE_SKILL_SOURCE_ROOT": "/workspace/source" if source_root else "",
        "KAGGLE_SKILL_CONFIG": "/workspace/config.json" if config_path else "",
        "KAGGLE_SKILL_SEED": str(seed),
        "PYTHONHASHSEED": str(seed),
    }
    for key, value in environment.items():
        arguments.extend(["--env", f"{key}={value}"])
    arguments.extend([container["image"], "/bin/sh", "-lc", command])
    return arguments


def make_run_id(experiment_family: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe_family = "".join(character if character.isalnum() or character in "-_" else "-" for character in experiment_family)
    return f"{timestamp}-{safe_family[:40]}-{uuid.uuid4().hex[:8]}"


def run_experiment(
    workspace: Path,
    *,
    track_id: str,
    phase_id: str,
    data_snapshot_id: str,
    experiment_family: str,
    idea_id: str,
    command: str,
    run_id: str | None = None,
    source_root: Path | None = None,
    config_path: Path | None = None,
    container_image: str | None = None,
    seed: int = 42,
    parent_run_id: str | None = None,
    artifact_paths: list[Path] | None = None,
    invariants: list[tuple[Path, Path]] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    ensure_identifier(data_snapshot_id, "data_snapshot_id")
    ensure_identifier(experiment_family, "experiment_family")
    ensure_identifier(idea_id, "idea_id")
    if run_id is not None:
        ensure_identifier(run_id, "run_id")
    if not command.strip():
        raise OpsError("command must be non-empty")
    competition = load_and_validate_competition(workspace)
    get_track(competition, track_id)
    get_phase(competition, phase_id)
    snapshot_path = workspace / "data" / "manifests" / f"{data_snapshot_id}.json"
    snapshot = read_json(snapshot_path)
    if snapshot.get("valid") is not True:
        raise OpsError(f"data snapshot is not valid: {data_snapshot_id}")
    data_errors_before, data_records_before = _verify_data_source(snapshot)
    if data_errors_before:
        raise OpsError("data snapshot integrity failed before run:\n- " + "\n- ".join(data_errors_before))
    events = load_events(workspace)
    if not any(item.get("event_type") == "idea_created" and item.get("idea_id") == idea_id for item in events):
        raise OpsError(f"idea_id does not exist: {idea_id}")

    source_hash = None
    source_files: list[dict[str, Any]] = []
    if source_root:
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise OpsError(f"source_root is not a directory: {source_root}")
        try:
            workspace.relative_to(source_root)
        except ValueError:
            pass
        else:
            raise OpsError("source_root must not contain the mutable competition workspace")
        source_hash, source_files = hash_tree(source_root)
    git_provenance, patch_bytes = _git_provenance(source_root)
    config_record = None
    if config_path:
        config_path = config_path.resolve()
        if not config_path.is_file():
            raise OpsError(f"config does not exist: {config_path}")
        config_record = {
            "path": config_path.as_posix(),
            "size": config_path.stat().st_size,
            "sha256": sha256_file(config_path),
        }
    parent_identity_sha256 = None
    if parent_run_id:
        parent_manifest_path = workspace / "runs" / parent_run_id / "run-manifest.json"
        parent_manifest = read_json(parent_manifest_path)
        parent_identity_sha256 = parent_manifest.get("identity_sha256")
        parent_errors = _verify_completed_manifest(
            parent_manifest, str(parent_identity_sha256), parent_manifest_path
        )
        if parent_errors:
            raise OpsError(
                f"parent run is not a valid sealed checkpoint: {parent_run_id}\n- "
                + "\n- ".join(parent_errors)
            )
    invariant_identity: list[dict[str, Any]] = []
    for output, reference in invariants or []:
        reference_path = _resolve_workspace_path(reference, workspace)
        if not reference_path.is_file():
            raise OpsError(f"controlled reference does not exist: {reference_path}")
        invariant_identity.append(
            {
                "output": output.as_posix(),
                "reference": reference_path.as_posix(),
                "reference_sha256": sha256_file(reference_path),
                "reference_size": reference_path.stat().st_size,
            }
        )
    container = _container_identity(container_image)
    environment = _environment_fingerprint()
    competition_sha256 = sha256_file(workspace / "competition.json")
    rules_snapshot_path = workspace / "rules" / "rules.json"
    rules_snapshot_sha256 = sha256_file(rules_snapshot_path) if rules_snapshot_path.is_file() else None
    identity = {
        "command": command,
        "track_id": track_id,
        "phase_id": phase_id,
        "data_snapshot_id": data_snapshot_id,
        "data_manifest_sha256": sha256_file(snapshot_path),
        "competition_sha256": competition_sha256,
        "rules_snapshot_sha256": rules_snapshot_sha256,
        "experiment_family": experiment_family,
        "idea_id": idea_id,
        "source_sha256": source_hash,
        "git_provenance": git_provenance,
        "config_sha256": config_record["sha256"] if config_record else None,
        "environment_sha256": hash_json(environment),
        "container": container,
        "seed": seed,
        "parent_run_id": parent_run_id,
        "parent_identity_sha256": parent_identity_sha256,
        "execution_workdir": "run_dir",
        "invariants": invariant_identity,
    }
    identity_sha256 = hash_json(identity)
    run_id = run_id or make_run_id(experiment_family)
    run_dir = workspace / "runs" / run_id
    manifest_path = run_dir / "run-manifest.json"
    if run_dir.exists():
        if not resume:
            raise OpsError(f"run directory already exists: {run_dir}")
        manifest = read_json(manifest_path)
        errors = _verify_completed_manifest(manifest, identity_sha256, manifest_path)
        if errors:
            raise OpsError("cannot resume run:\n- " + "\n- ".join(errors))
        return {**manifest, "resume_skipped": True}

    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "checkpoints").mkdir()
    if patch_bytes:
        (run_dir / "code.patch").write_bytes(patch_bytes)
    planned_at = utc_now()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "planned",
        "track_id": track_id,
        "phase_id": phase_id,
        "data_snapshot_id": data_snapshot_id,
        "experiment_family": experiment_family,
        "idea_id": idea_id,
        "parent_run_id": parent_run_id,
        "identity": identity,
        "identity_sha256": identity_sha256,
        "command": command,
        "command_tokens": shlex.split(command, posix=os.name != "nt"),
        "source_root": source_root.as_posix() if source_root else None,
        "execution_workdir": run_dir.as_posix(),
        "source_sha256": source_hash,
        "source_file_count": len(source_files),
        "git_provenance": git_provenance,
        "code_patch_path": (run_dir / "code.patch").as_posix() if patch_bytes else None,
        "config": config_record,
        "environment": environment,
        "container": container,
        "competition_sha256": competition_sha256,
        "rules_snapshot_sha256": rules_snapshot_sha256,
        "seed": seed,
        "artifacts": [],
        "controlled_differences": [],
        "planned_at": planned_at,
        "data_integrity_before": {
            "passed": True,
            "file_count": len(data_records_before),
        },
    }
    write_json_atomic(manifest_path, manifest)
    append_event_checked(
        workspace,
        event(
            "run_planned",
            {"status": "planned", "identity_sha256": identity_sha256, "command": command},
            track_id=track_id,
            phase_id=phase_id,
            data_snapshot_id=data_snapshot_id,
            run_id=run_id,
            idea_id=idea_id,
            experiment_family=experiment_family,
        ),
    )

    manifest["status"] = "running"
    manifest["started_at"] = utc_now()
    write_json_atomic(manifest_path, manifest)
    append_event_checked(
        workspace,
        event(
            "run_started",
            {"status": "running", "identity_sha256": identity_sha256},
            track_id=track_id,
            phase_id=phase_id,
            data_snapshot_id=data_snapshot_id,
            run_id=run_id,
            idea_id=idea_id,
            experiment_family=experiment_family,
        ),
    )

    execution_cwd = run_dir.resolve()
    environment_vars = os.environ.copy()
    environment_vars.update(
        {
            "KAGGLE_SKILL_WORKSPACE": workspace.as_posix(),
            "KAGGLE_SKILL_RUN_ID": run_id,
            "KAGGLE_SKILL_RUN_DIR": run_dir.as_posix(),
            "KAGGLE_SKILL_DATA_MANIFEST": snapshot_path.as_posix(),
            "KAGGLE_SKILL_DATA_ROOT": str(snapshot.get("source", "")),
            "KAGGLE_SKILL_SOURCE_ROOT": source_root.as_posix() if source_root else "",
            "KAGGLE_SKILL_CONFIG": config_path.as_posix() if config_path else "",
            "KAGGLE_SKILL_SEED": str(seed),
            "PYTHONHASHSEED": str(seed),
        }
    )
    started_wall = time.monotonic()
    started_cpu = time.process_time()
    log_path = run_dir / "run.log"
    process_command: str | list[str]
    use_shell = container is None
    if container:
        process_command = _docker_command(
            container=container,
            command=command,
            run_dir=run_dir,
            data_root=Path(str(snapshot["source"])).resolve(),
            snapshot_path=snapshot_path,
            source_root=source_root,
            config_path=config_path,
            run_id=run_id,
            seed=seed,
        )
    else:
        process_command = command
    with exclusive_lock(run_dir / "run.lock", timeout_seconds=0.1):
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            log.write(
                f"run_id={run_id}\nstarted_at={manifest['started_at']}\n"
                f"cwd={execution_cwd}\ncommand={command}\ncontainer={container}\n\n"
            )
            log.flush()
            try:
                completed = subprocess.run(
                    process_command,
                    shell=use_shell,
                    cwd=execution_cwd,
                    env=environment_vars,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                log.write(f"execution failed: {exc}\n")
                completed = subprocess.CompletedProcess(process_command, 127)
    wall_seconds = time.monotonic() - started_wall
    cpu_seconds = time.process_time() - started_cpu
    validation_errors: list[str] = []
    data_errors_after, data_records_after = _verify_data_source(snapshot)
    validation_errors.extend(data_errors_after)
    source_sha256_after = None
    if source_root:
        source_sha256_after, _ = hash_tree(source_root)
        if source_sha256_after != source_hash:
            validation_errors.append("source code changed during the run")
    controlled_differences: list[dict[str, Any]] = []
    for output, reference in invariants or []:
        output_path = _resolve_run_path(output, run_dir)
        try:
            output_path.relative_to(run_dir)
        except ValueError:
            validation_errors.append(f"controlled output escapes isolated run directory: {output_path}")
            continue
        reference_path = _resolve_workspace_path(reference, workspace)
        if not output_path.is_file():
            validation_errors.append(f"controlled output missing: {output_path}")
            continue
        if not reference_path.is_file():
            validation_errors.append(f"controlled reference missing: {reference_path}")
            continue
        output_hash = sha256_file(output_path)
        reference_hash = sha256_file(reference_path)
        passed = output_hash == reference_hash
        controlled_differences.append(
            {
                "output": output_path.as_posix(),
                "reference": reference_path.as_posix(),
                "output_sha256": output_hash,
                "reference_sha256": reference_hash,
                "passed": passed,
            }
        )
        if not passed:
            validation_errors.append(f"controlled difference invariant failed: {output_path}")

    artifacts: list[dict[str, Any]] = []
    for artifact in artifact_paths or []:
        artifact_path = _resolve_run_path(artifact, run_dir)
        try:
            artifact_path.relative_to(run_dir)
        except ValueError:
            validation_errors.append(f"declared artifact escapes isolated run directory: {artifact_path}")
            continue
        if not artifact_path.is_file():
            validation_errors.append(f"declared artifact is missing: {artifact_path}")
            continue
        artifacts.append(store_artifact(artifact_path, workspace / "artifacts"))

    status = "completed"
    if completed.returncode != 0:
        status = "failed"
        validation_errors.append(f"command exited with code {completed.returncode}")
    elif validation_errors:
        status = "invalid"
    manifest.update(
        {
            "status": status,
            "return_code": completed.returncode,
            "completed_at": utc_now(),
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "log_path": log_path.as_posix(),
            "log_sha256": sha256_file(log_path),
            "artifacts": artifacts,
            "controlled_differences": controlled_differences,
            "validation_errors": validation_errors,
            "data_integrity_after": {
                "passed": not data_errors_after,
                "file_count": len(data_records_after),
                "errors": data_errors_after,
            },
            "source_sha256_after": source_sha256_after,
        }
    )
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "stage": "command",
        "identity_sha256": identity_sha256,
        "status": status,
        "artifacts": [{"sha256": item["sha256"], "store_path": item["store_path"]} for item in artifacts],
        "created_at": manifest["completed_at"],
    }
    write_json_atomic(run_dir / "checkpoints" / "command.json", checkpoint)
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(
        run_dir / "run-manifest.sha256.json",
        {"sha256": sha256_file(manifest_path), "sealed_at": manifest["completed_at"]},
    )
    terminal_event = "run_completed" if status in {"completed", "invalid"} else "run_failed"
    append_event_checked(
        workspace,
        event(
            terminal_event,
            {
                "status": status,
                "return_code": completed.returncode,
                "wall_seconds": wall_seconds,
                "validation_errors": validation_errors,
                "artifacts": artifacts,
                "failure_reason": validation_errors[0] if validation_errors else None,
            },
            track_id=track_id,
            phase_id=phase_id,
            data_snapshot_id=data_snapshot_id,
            run_id=run_id,
            idea_id=idea_id,
            experiment_family=experiment_family,
        ),
    )
    append_event_checked(
        workspace,
        event(
            "validation_recorded",
            {
                "status": "completed" if status == "completed" else "invalid",
                "passed": status == "completed",
                "errors": validation_errors,
                "controlled_differences": controlled_differences,
            },
            track_id=track_id,
            phase_id=phase_id,
            data_snapshot_id=data_snapshot_id,
            run_id=run_id,
            idea_id=idea_id,
            experiment_family=experiment_family,
        ),
    )
    materialize_state(workspace)
    if status != "completed":
        raise OpsError(f"run {run_id} ended with status {status}; see {log_path}")
    return manifest

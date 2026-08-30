from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 2


class OpsError(RuntimeError):
    """User-facing validation or workflow error."""


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def ensure_identifier(value: str, field: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise OpsError(
            f"{field} must use 1-128 ASCII letters, numbers, dot, underscore, or hyphen"
        )
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    record = {
        "path": resolved.as_posix() if root is None else resolved.relative_to(root.resolve()).as_posix(),
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }
    return record


def hash_tree(root: Path, *, exclude_names: set[str] | None = None) -> tuple[str, list[dict[str, Any]]]:
    root = root.resolve()
    excluded = exclude_names or {"__pycache__", ".pytest_cache", ".git"}
    records: list[dict[str, Any]] = []
    if not root.exists():
        raise OpsError(f"path does not exist: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        records.append(file_record(path, root))
    return hash_json(records), records


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpsError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OpsError(f"invalid JSON in {path}: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(value, dict):
        raise OpsError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OpsError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise OpsError(f"{path}:{line_no}: event must be an object")
            yield line_no, value


@contextmanager
def exclusive_lock(path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OpsError(f"timed out waiting for lock: {path}")
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()} created_at={utc_now()}\n".encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
        append_jsonl_unlocked(path, value)


def append_jsonl_unlocked(path: Path, value: dict[str, Any]) -> None:
    """Append after the caller has acquired the JSONL transaction lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def event(
    event_type: str,
    payload: dict[str, Any],
    *,
    track_id: str | None = None,
    phase_id: str | None = None,
    data_snapshot_id: str | None = None,
    run_id: str | None = None,
    idea_id: str | None = None,
    experiment_family: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": utc_now(),
        "payload": payload,
    }
    optional = {
        "track_id": track_id,
        "phase_id": phase_id,
        "data_snapshot_id": data_snapshot_id,
        "run_id": run_id,
        "idea_id": idea_id,
        "experiment_family": experiment_family,
    }
    value.update({key: item for key, item in optional.items() if item not in (None, "")})
    return value


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise OpsError(f"path escapes root {root}: {path}") from exc
    return resolved


def copy_readonly(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def store_artifact(source: Path, artifact_root: Path) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file():
        raise OpsError(f"artifact is not a file: {source}")
    digest = sha256_file(source)
    destination = artifact_root / "sha256" / digest[:2] / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != digest:
            temporary.unlink(missing_ok=True)
            raise OpsError(f"artifact copy hash mismatch: {source}")
        os.replace(temporary, destination)
    elif sha256_file(destination) != digest:
        raise OpsError(f"content-addressed artifact is corrupt: {destination}")

    # Keep the run output and immutable CAS object on separate inodes. A hardlink
    # here would let later edits to a run file silently corrupt the shared store.
    return {
        "source_path": source.as_posix(),
        "store_path": destination.as_posix(),
        "sha256": digest,
        "size": destination.stat().st_size,
        "hardlinked": False,
    }


def require_keys(value: dict[str, Any], required: Iterable[str], context: str) -> list[str]:
    return [f"{context}: missing field: {key}" for key in required if key not in value]


def unknown_keys(value: dict[str, Any], allowed: set[str], context: str) -> list[str]:
    return [f"{context}: unknown field: {key}" for key in sorted(set(value) - allowed)]


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")

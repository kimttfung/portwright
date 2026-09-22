"""Validate migration approval and exchange a generic Engineer task/result."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .discovery import discover
from .workflow import read_json, run_path, utc_now


def _write_json(path: Path, value: dict, expected: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and expected is None:
        if read_json(path) == value:
            return
        raise ValueError(f"Refusing to replace an existing artifact: {path}")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-migration-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        json.dump(value, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if expected is not None and path.read_bytes() != expected:
            raise ValueError("The run changed concurrently.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git_changed_paths(repository: Path) -> list[str]:
    if not (repository / ".git").exists():
        return []
    executable = shutil.which("git")
    if not executable or Path(executable).resolve().is_relative_to(repository):
        raise ValueError("A trusted Git executable is required.")
    result = subprocess.run(
        [
            str(Path(executable).resolve()),
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise ValueError("Could not validate the target working tree.")
    values = result.stdout.decode("utf-8", errors="strict").split("\0")
    paths = []
    index = 0
    while index < len(values):
        value = values[index]
        if not value:
            index += 1
            continue
        code, name = value[:2], value[3:]
        paths.append(name.replace("/", "\\"))
        index += 2 if "R" in code or "C" in code else 1
    return sorted(set(paths))


def _approval(record: dict) -> dict:
    plan = record.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("Migration requires a recorded migration brief.")
    approval = next(
        (item for item in record.get("approvals", []) if item.get("id") == plan.get("approval_id")),
        None,
    )
    if not approval or approval.get("state") != "approved-not-executed":
        raise ValueError("The named probe/migration scope is not approved.")
    if approval.get("source_edits_authorized") is not True:
        raise ValueError("Source-changing migration needs explicit edit approval.")
    if approval.get("execution_authorized") is not True:
        raise ValueError("Migration execution needs explicit command approval.")
    if approval.get("execution_started"):
        raise ValueError("The approved migration is already marked as started.")
    return approval


def _relative_files(repository: Path, values: list[str]) -> list[str]:
    paths = []
    for value in values:
        candidate = (repository / value).resolve()
        if not candidate.is_relative_to(repository):
            raise ValueError(f"Authorized file leaves the target: {value}")
        paths.append(str(candidate.relative_to(repository)).replace("/", "\\"))
    return paths


def _validate_baseline(workspace: Path, repository: Path, record: dict) -> dict:
    report = discover(repository)
    plan = record["plan"]
    if report["repository"]["revision"] != plan["source_revision"]:
        raise ValueError("The target revision changed after planning.")
    if report["source_fingerprint"] != plan["source_fingerprint"]:
        raise ValueError("Relevant declarations changed after planning.")
    if report["repository"]["working_tree_fingerprint"] != plan["working_tree_fingerprint"]:
        raise ValueError("Tracked source changed after planning.")
    if Path(record["workspace"]).resolve() != workspace:
        raise ValueError("The run belongs to another workspace.")
    return report


def engineer_result(record: dict, directory: Path, repository: Path) -> dict | None:
    migration = record.get("migration")
    if not migration:
        return None
    result_path = directory / migration["result_path"]
    if not result_path.exists():
        return None
    result = read_json(result_path)
    expected = {
        "migration_id": migration["id"],
        "run_id": record["run_id"],
        "role": "Engineer",
        "execution_mode": "agent-guided",
        "plan_id": record["plan"]["id"],
        "approval_id": migration["approval_id"],
        "automatic_cli_invocation": False,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise ValueError(f"Engineer result has a stale or invalid {key}.")
    changed = result.get("changed_files")
    if not isinstance(changed, list):
        raise ValueError("Engineer result needs changed_files.")
    allowed = set(migration["authorized_files"])
    returned = {item.get("path") for item in changed}
    if not returned.issubset(allowed):
        raise ValueError("Engineer result leaves the approved source scope.")
    for item in changed:
        path = repository / item["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("after_sha256"):
            raise ValueError(f"Current source does not match Engineer result: {item.get('path')}")
    dirty = set(_git_changed_paths(repository))
    output_root = migration.get("output_root")
    if isinstance(output_root, str) and output_root:
        normalized_root = output_root.replace("/", "\\").rstrip("\\")
        dirty = {
            value
            for value in dirty
            if value != normalized_root and not value.startswith(normalized_root + "\\")
        }
    if dirty and not dirty.issubset(allowed):
        outside = sorted(dirty - allowed)
        raise ValueError(
            f"The checkout contains changes outside the approved source scope: {outside}"
        )
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Engineer result needs explicit command/check outcomes.")
    for check in checks:
        if check.get("status") not in {"passed", "failed", "blocked", "not-run"}:
            raise ValueError("Engineer result has an invalid check status.")
    if not isinstance(result.get("artifacts", []), list):
        raise ValueError("Engineer result artifacts must be a list.")
    return result


def migrate(workspace: Path, run_id: str) -> dict:
    started = utc_now()
    workspace = workspace.resolve(strict=True)
    path = run_path(workspace, run_id)
    previous = path.read_bytes()
    record = read_json(path)
    if record.get("execution_mode") != "agent-guided":
        raise ValueError("Migration requires an agent-guided run.")
    if record.get("migration", {}).get("result_status") == "imported-agent-guided":
        return record
    approval = _approval(record)
    repository = (workspace / record["target"]["checkout"]).resolve(strict=True)
    if record.get("migration") is None:
        report = _validate_baseline(workspace, repository, record)
        probe = record["plan"]["proposal"].get("first_probe") or {}
        authorized = _relative_files(repository, probe.get("authorized_files", []))
        before = {
            name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
            for name in authorized
            if (repository / name).is_file()
        }
        binding = json.dumps(
            {
                "run_id": run_id,
                "plan_id": record["plan"]["id"],
                "approval": approval,
                "source": report["repository"],
                "authorized_files": authorized,
                "before": before,
                "probe": probe,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        migration_id = hashlib.sha256(binding.encode()).hexdigest()
        task_path = str(Path("logs") / f"engineer-{migration_id[:16]}.task.json")
        result_path = str(Path("logs") / f"engineer-{migration_id[:16]}.result.json")
        task = {
            "migration_id": migration_id,
            "run_id": run_id,
            "role": "Engineer",
            "execution_mode": "agent-guided",
            "automatic_cli_invocation": False,
            "plan_id": record["plan"]["id"],
            "plan_path": str(path.parent / record["plan"]["path"]),
            "approval_id": approval["id"],
            "repository": report["repository"],
            "objective": probe.get("objective"),
            "command": probe.get("command"),
            "working_directory": probe.get("working_directory"),
            "environment": probe.get("environment", {}),
            "output_root": probe.get("output_root"),
            "time_limit_seconds": probe.get("time_limit_seconds"),
            "expected_result": probe.get("expected_result"),
            "authorized_files": authorized,
            "source_before_sha256": before,
            "permissions": {
                "source_write": authorized,
                "generated_output_root": probe.get("output_root"),
                "result_path": str(path.parent / result_path),
                "delegation": False,
            },
            "return_fields": [
                "migration_id",
                "run_id",
                "role",
                "execution_mode",
                "automatic_cli_invocation",
                "plan_id",
                "approval_id",
                "changed_files",
                "commands",
                "checks",
                "artifacts",
                "remaining_unknowns",
            ],
            "handoff": "The approved host performs this task. Portwright did not invoke it automatically.",
        }
        _write_json(path.parent / task_path, task)
        record.update(
            stage="engineering",
            stop_after_stage="engineering",
            active_role=None,
            status="engineer-task-ready",
        )
        record["migration"] = {
            "id": migration_id,
            "task_path": task_path,
            "result_path": result_path,
            "approval_id": approval["id"],
            "authorized_files": authorized,
            "output_root": probe.get("output_root"),
            "agent_invoked": False,
            "result_status": "pending",
            "created_at_utc": started,
        }
        record.setdefault("capabilities", {})["migrate"] = "implemented-agent-guided-task-import"
        record["next_action"] = "Relay the Engineer task and return the named result file."
    result = engineer_result(record, path.parent, repository)
    if result:
        record["migration"]["result_status"] = "imported-agent-guided"
        record["migration"]["result"] = result
        record["status"] = "engineering-result-imported"
        approval["state"] = "approved-executed"
        approval["execution_started"] = True
        approval["execution_completed"] = True
        record["plan"]["execution_started"] = True
        record["next_action"] = "Review the actual result and approve staging or a bounded repair."
    ended = utc_now()
    intervals = record.setdefault("timing", {}).setdefault("intervals", [])
    elapsed = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    intervals.append(
        {"stage": "engineering", "activity": "migrate", "started_at_utc": started, "ended_at_utc": ended, "elapsed_seconds": elapsed}
    )
    _write_json(path, record, previous)
    return record


def render_migration(record: dict, directory: Path) -> str:
    migration = record.get("migration")
    if not migration:
        raise ValueError("No engineering interaction has been recorded.")
    result = migration.get("result")
    lines = [
        f"PORTWRIGHT MIGRATE {record['run_id']}",
        "Mode: agent-guided; approved host execution, not automatic CLI invocation",
        f"Plan: {record['plan']['id']} | Approval: {migration['approval_id']}",
        f"Task packet: {migration['task_path']}",
    ]
    if result is None:
        lines.append("Execution: pending approved host handoff")
    else:
        lines.append(f"Changed files: {len(result.get('changed_files', []))}")
        for check in result.get("checks", []):
            lines.append(f"- {check.get('name', 'check')}: {check.get('status')}")
        lines.append(f"Artifacts: {len(result.get('artifacts', []))}")
        if result.get("remaining_unknowns"):
            lines.append("Remaining unknowns: " + "; ".join(result["remaining_unknowns"]))
    return "\n".join(lines) + "\n"

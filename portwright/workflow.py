"""Deterministic run selection, discovery state, and agent-guided handoff."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .discovery import discover


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Artifact path leaves its run: {value}")
    return path


def run_path(workspace: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run_id):
        raise ValueError("Run IDs must be simple names, not paths.")
    return _inside(workspace, str(Path("runs") / run_id / "run.json"))


def _write_json(path: Path, value: dict, expected: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and expected is None:
        if read_json(path) == value:
            return
        raise ValueError(f"Refusing to replace an existing artifact: {path}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".portwright-", suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        json.dump(value, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if expected is not None and (not path.exists() or path.read_bytes() != expected):
            raise ValueError("The run changed concurrently; no update was written.")
        if expected is None and path.exists():
            raise ValueError("The artifact appeared concurrently; no update was written.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _target_path(workspace: Path, record: dict) -> Path | None:
    value = record.get("target", {}).get("checkout")
    return (workspace / value).resolve() if isinstance(value, str) else None


def select_run(workspace: Path, repository: Path, requested: str | None) -> Path:
    if requested:
        path = run_path(workspace, requested)
        if path.exists() and _target_path(workspace, read_json(path)) != repository:
            raise ValueError("That run belongs to a different checkout.")
        return path
    matches = []
    for path in sorted((workspace / "runs").glob("*\\run.json")):
        if _target_path(workspace, read_json(path)) == repository:
            matches.append(path)
    if len(matches) > 1:
        raise ValueError("Multiple runs match this checkout; pass --run-id.")
    if matches:
        return matches[0]
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", repository.name).strip(".-")[:45] or "repository"
    suffix = hashlib.sha256(str(repository).encode()).hexdigest()[:10]
    path = run_path(workspace, f"{name}-{suffix}")
    if path.exists():
        raise ValueError("Generated run ID is occupied; pass --run-id.")
    return path


def _probe(record: dict, directory: Path, report: dict) -> dict | None:
    result = record.get("qualification", {}).get("build_result") or {}
    relative = result.get("result")
    if not isinstance(relative, str):
        return None
    path = _inside(directory, relative)
    if not path.is_file():
        return {"status": "missing", "path": relative}
    probe = read_json(path)
    return {
        "status": "recorded", "outcome": probe.get("outcome", "unknown"), "path": relative,
        "same_revision": probe.get("repository_revision") == report["repository"]["revision"],
        "working_tree_clean": report["repository"]["working_tree"] == "clean",
        "observed_failure": probe.get("observed_failure"),
        "generated_outputs": probe.get("generated_outputs"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "limits": "Historical probe evidence; not rerun or reclassified by analysis.",
    }


def investigator_result(record: dict, directory: Path) -> dict | None:
    analysis = record.get("analysis")
    if not analysis:
        return None
    path = _inside(directory, analysis["result_path"])
    if not path.exists():
        return None
    result = read_json(path)
    for key, expected in (
        ("analysis_id", analysis["id"]), ("run_id", record["run_id"]),
        ("role", "Investigator"), ("execution_mode", "agent-guided"),
        ("source_fingerprint", analysis["report"]["source_fingerprint"]),
    ):
        if result.get(key) != expected:
            raise ValueError(f"Investigator result has a stale or invalid {key}.")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise ValueError("Investigator result needs a substantive summary.")
    return result


def analyze(workspace: Path, repository: Path, requested: str | None = None) -> dict:
    started = utc_now()
    workspace = workspace.resolve(strict=True)
    repository = repository.resolve(strict=True)
    if workspace.is_relative_to(repository):
        raise ValueError("Use a Portwright workspace outside the target repository.")
    path = select_run(workspace, repository, requested)
    if path.resolve().is_relative_to(repository):
        raise ValueError("Run artifacts must not be written inside the target repository.")
    previous = path.read_bytes() if path.exists() else None
    record = read_json(path) if previous is not None else {
        "schema_version": 1, "run_id": path.parent.name, "workspace": str(workspace),
        "target": {"checkout": str(repository), "source_edits_authorized": False},
        "approvals": [], "patterns": [], "timing": {"intervals": []},
    }
    if record.get("run_id") != path.parent.name:
        raise ValueError("The saved run ID does not match its directory.")
    report = discover(repository)
    expected_revision = record.get("target", {}).get("requested_revision")
    if expected_revision and expected_revision != report["repository"]["revision"]:
        raise ValueError("The checkout no longer matches this run's pinned baseline.")
    instructions = [
        ".github\\skills\\portwright\\SKILL.md", "instructions\\investigate.md",
    ]
    instruction_hashes = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in instructions
    }
    binding = json.dumps({
        "source": report["source_fingerprint"], "repository": report["repository"],
        "run_id": record["run_id"], "instructions": instruction_hashes,
    }, sort_keys=True)
    analysis_id = hashlib.sha256(binding.encode()).hexdigest()
    task_path = str(Path("logs") / f"investigator-{analysis_id[:16]}.task.json")
    result_path = str(Path("logs") / f"investigator-{analysis_id[:16]}.result.json")
    probe = _probe(record, path.parent, report)
    task = {
        "analysis_id": analysis_id, "run_id": record["run_id"], "role": "Investigator",
        "execution_mode": "agent-guided", "automatic_invocation": False,
        "repository": report["repository"], "source_fingerprint": report["source_fingerprint"],
        "instruction_files": instructions, "instruction_sha256": instruction_hashes,
        "objective": (
            "Reconcile source facts and prior evidence, recommend the least-invasive route, "
            "and describe the cheapest next approved probe. Stop before migration planning."
        ),
        "permissions": {
            "target": "read-only", "target_execution": False, "source_changes": False,
            "installs": False, "delegation": False,
            "result_path": str(path.parent / result_path),
        },
        "facts": report, "historical_probe": probe,
        "return_fields": ["analysis_id", "run_id", "role", "execution_mode",
                          "source_fingerprint", "summary", "recommendation",
                          "migration_brief", "limits"],
        "handoff": "Relay this packet to the approved existing agent host. The CLI did not launch a specialist. Save its result only at result_path.",
    }
    if record.get("stage") != "discovery":
        record.setdefault("stage_history", []).append({
            "stage": record.get("stage"), "status": record.get("status"),
            "execution_mode": record.get("execution_mode"), "preserved_at_utc": started,
        })
    record.update(
        stage="discovery",
        stop_after_stage="discovery",
        execution_mode="agent-guided",
        cli_exists=True,
        active_role=None,
        status="discovery-recorded",
    )
    record["analysis"] = {
        "id": analysis_id, "started_at_utc": started, "recorded_at_utc": utc_now(),
        "report": report, "historical_probe": probe,
        "task_path": task_path, "result_path": result_path,
        "agent_invoked": False, "result_status": "pending",
    }
    _write_json(_inside(path.parent, task_path), task)
    if investigator_result(record, path.parent):
        record["analysis"]["result_status"] = "imported-agent-guided"
        record["status"] = "discovery-complete-agent-guided"
    else:
        record["status"] = "discovery-awaiting-investigator"
    record.setdefault("capabilities", {}).update({
        "analyze": "implemented-static-discovery-with-agent-guided-investigator",
        "status": "implemented-offline", "python_module_entry": "implemented",
        "coordinator_code": "implemented-for-analysis-state-and-handoff-only",
    })
    record["next_action"] = (
        "Complete the Investigator handoff if pending, then create a migration brief. "
        "Target edits and target execution require separate approval."
    )
    ended = utc_now()
    record["analysis"]["ended_at_utc"] = ended
    timing = record.setdefault("timing", {"intervals": []})
    intervals = timing.setdefault("intervals", [])
    covered = any(item.get("ended_at_utc") is None for item in intervals)
    record["analysis"]["timing_covered_by_open_interval"] = covered
    if not covered:
        elapsed = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
        intervals.append({
            "stage": "discovery", "activity": "analysis", "started_at_utc": started,
            "ended_at_utc": ended, "elapsed_seconds": elapsed,
        })
        timing["elapsed_seconds_at_last_observation"] = (
            timing.get("elapsed_seconds_at_last_observation", 0) + elapsed
        )
        timing["last_observed_at_utc"] = ended
        if "planning_budget_seconds" in timing:
            timing["planning_remaining_seconds"] = (
                timing["planning_budget_seconds"] - timing["elapsed_seconds_at_last_observation"]
            )
    _write_json(path, record, expected=previous)
    return record


def _duration(seconds: float) -> str:
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m {int(seconds % 60)}s"


def render(record: dict, directory: Path, include_findings: bool) -> str:
    analysis = record.get("analysis")
    if not analysis:
        return f"PORTWRIGHT {record['run_id']}\nStage: {record.get('stage', 'unknown')}\nAnalysis: not recorded\n"
    report = analysis["report"]
    result = investigator_result(record, directory)
    lines = [
        f"PORTWRIGHT {record['run_id']}",
        f"Repository: {report['repository']['url'] or report['repository']['path']}",
        f"Revision: {report['repository']['revision'] or 'unversioned'}",
        f"Stage: {record['stage']} | Mode: agent-guided | Active specialist: none",
        f"Investigator: {'result imported from host; not CLI-invoked' if result else 'pending handoff; not invoked'}",
        f"Stack: {report['stack']}",
        f"Windows: {report['windows_support']} | Arm64: {report['arm64_support']}",
        f"Candidate route: {report['candidate_route'] or 'investigator decision required'} (not approved)",
        f"Selected route: {record.get('route', {}).get('selected') or 'none'}",
    ]
    probe = analysis.get("historical_probe")
    if probe:
        lines.append(f"Recorded probe: {probe.get('outcome', probe['status'])}; no target execution during analysis")
        if probe["status"] == "recorded" and not (probe["same_revision"] and probe["working_tree_clean"]):
            lines.append("Probe applicability: historical only; the current baseline differs or is dirty")
    lines.append("Application: unverified; linker/runtime outcomes are not established")
    if include_findings:
        for finding in report["findings"]:
            evidence = finding["evidence"][0]
            lines.append(f"[SOURCE] {finding['title']} [{evidence['path']}:{evidence['line']}]")
            lines.append(f"  Why: {finding['why']}")
        if probe and probe.get("observed_failure"):
            lines.append(f"[OBSERVED, historical] {probe['observed_failure']['first_error']}")
        if report["ci_targets"]:
            lines.append("CI target declarations: " + ", ".join(sorted({x["platform"] for x in report["ci_targets"]})))
        if result and isinstance(result.get("next_probe_summary"), str):
            lines.append("Next proposed check (not approved): " + result["next_probe_summary"])
    lines.append(f"Task packet: {analysis['task_path']}")
    lines.append(f"Snapshot: {analysis['recorded_at_utc']} (status does not refresh source or call a model)")
    timing = record.get("timing", {})
    if "elapsed_seconds_at_last_observation" in timing:
        lines.append(f"Recorded work: {_duration(timing['elapsed_seconds_at_last_observation'])}; budget is the existing assumption")
    lines.append(
        "Next: create a migration brief; no target edits/builds approved"
        if result
        else "Next: relay the pending Investigator packet; no target edits/builds approved"
    )
    visible = [re.sub(r"[\x00-\x1f\x7f]", lambda match: f"\\x{ord(match[0]):02x}", line)
               for line in lines]
    return "\n".join(visible) + "\n"

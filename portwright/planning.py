"""Create a generic, source-bound migration brief from Investigator evidence."""

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .discovery import discover
from .workflow import investigator_result, read_json, run_path, utc_now

PLAN_FORMAT_VERSION = 3
ROUTES = {
    "native-arm64",
    "native-arm64-with-dependency-remediation",
    "arm64ec",
    "packaging-release-repair",
    "windows-enablement",
    "windows-modernization",
    "already-supported",
    "blocked",
}


def _write_text(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return
        raise ValueError(f"Refusing to replace an existing migration brief: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-plan-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        file.write(content)
    try:
        if path.exists():
            raise ValueError("The migration brief appeared concurrently.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: dict, expected: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-run-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        json.dump(value, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if path.read_bytes() != expected:
            raise ValueError("The run changed concurrently; the plan was not recorded.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"The migration brief needs a non-empty {name} list.")
    return value


def _validate_binding(record: dict, workspace: Path, report: dict) -> None:
    saved = record["analysis"]["report"]
    current = report["repository"]
    previous = saved["repository"]
    for key in ("revision", "working_tree", "working_tree_fingerprint"):
        if current.get(key) != previous.get(key):
            raise ValueError("The target changed after discovery; refresh analysis.")
    if current.get("working_tree") == "dirty":
        raise ValueError("Planning requires the discovery baseline or a newly approved baseline.")
    if report["source_fingerprint"] != saved["source_fingerprint"]:
        raise ValueError("Relevant declarations changed after discovery.")
    if Path(record["workspace"]).resolve() != workspace:
        raise ValueError("The run belongs to a different Portwright workspace.")


def _proposal(result: dict) -> dict:
    proposal = result.get("migration_brief")
    if not isinstance(proposal, dict):
        raise ValueError("The Investigator result needs a migration_brief object.")
    route = proposal.get("route")
    if route not in ROUTES:
        raise ValueError(f"Unsupported route in migration brief: {route!r}")
    task = proposal.get("useful_task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("The migration brief needs a useful_task.")
    changes = proposal.get("changes")
    if not isinstance(changes, list) or len(changes) > 5:
        raise ValueError("The migration brief needs at most five changes.")
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each migration change must be an object.")
        change_id = change.get("id")
        if not isinstance(change_id, str) or not change_id or change_id in seen:
            raise ValueError("Migration changes need unique non-empty IDs.")
        dependencies = change.get("depends_on", [])
        if not isinstance(dependencies, list) or any(item not in seen for item in dependencies):
            raise ValueError("Migration changes must follow their dependencies.")
        for key in ("title", "description"):
            if not isinstance(change.get(key), str) or not change[key].strip():
                raise ValueError(f"Migration change {change_id} needs {key}.")
        seen.add(change_id)
    for key in (
        "behavior_to_preserve",
        "fixtures",
        "expected_outputs",
        "required_components",
        "acceptance_checks",
        "stop_conditions",
    ):
        _require_strings(proposal.get(key), key)
    alternatives = proposal.get("alternatives")
    if not isinstance(alternatives, dict) or not alternatives:
        raise ValueError("The migration brief must explain rejected alternatives.")
    probe = proposal.get("first_probe")
    if route not in {"already-supported", "blocked"}:
        if not isinstance(probe, dict):
            raise ValueError("An executable route needs a first_probe.")
        for key in ("objective", "command", "working_directory", "output_root", "expected_result"):
            if not isinstance(probe.get(key), str) or not probe[key].strip():
                raise ValueError(f"The first probe needs {key}.")
        seconds = probe.get("time_limit_seconds")
        if not isinstance(seconds, int) or not 1 <= seconds <= 3600:
            raise ValueError("The first probe needs a 1-3600 second limit.")
        files = probe.get("authorized_files", [])
        if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
            raise ValueError("authorized_files must be a list of relative paths.")
    return proposal


def _bullet(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def _render(record: dict, report: dict, proposal: dict, plan_id: str) -> str:
    repository = report["repository"]
    changes = "\n".join(
        f"{index}. **{item['title']}** {item['description']}"
        for index, item in enumerate(proposal["changes"], 1)
    ) or "No source changes. The selected result is non-mutating."
    alternatives = "\n".join(
        f"- **{name.replace('-', ' ')}:** {reason}"
        for name, reason in proposal["alternatives"].items()
    )
    probe = proposal.get("first_probe")
    probe_text = "No probe. The selected route is non-mutating."
    if probe:
        files = probe.get("authorized_files", [])
        probe_text = "\n".join(
            [
                f"**Objective:** {probe['objective']}",
                f"**Command:** `{probe['command']}`",
                f"**Working directory:** `{probe['working_directory']}`",
                f"**Output root:** `{probe['output_root']}`",
                f"**Expected result:** {probe['expected_result']}",
                f"**Time limit:** {probe['time_limit_seconds']} seconds",
                "**Authorized maintained files:** "
                + (", ".join(f"`{value}`" for value in files) if files else "none"),
            ]
        )
    return f"""# Migration brief: {record['run_id']}

**Status:** Plan ready; awaiting approval.
**Plan ID:** `{plan_id}`
**Source:** `{repository.get('url') or repository.get('path')}` at `{repository.get('revision') or 'unversioned'}`.

## User task

{proposal['useful_task']}

## Route

**Selected, not approved:** `{proposal['route']}`

{proposal.get('rationale', '')}

Rejected alternatives:
{alternatives}

## Evidence and unknowns

{_bullet(_require_strings(proposal.get('evidence'), 'evidence'))}

Unknowns:
{_bullet(_require_strings(proposal.get('unknowns'), 'unknowns'))}

## Planned changes

{changes}

## Preserve

{_bullet(proposal['behavior_to_preserve'])}

## Fixtures and expected outputs

Fixtures:
{_bullet(proposal['fixtures'])}

Expected outputs:
{_bullet(proposal['expected_outputs'])}

Required components:
{_bullet(proposal['required_components'])}

## First approved probe

{probe_text}

## Acceptance

{_bullet(proposal['acceptance_checks'])}

## Stop conditions

{_bullet(proposal['stop_conditions'])}

This brief grants no execution or edit permission. Probe and migration approval are separate.
"""


def plan(workspace: Path, run_id: str) -> dict:
    started = utc_now()
    workspace = workspace.resolve(strict=True)
    path = run_path(workspace, run_id)
    previous = path.read_bytes()
    record = read_json(path)
    if record.get("execution_mode") != "agent-guided":
        raise ValueError("Planning requires an agent-guided run.")
    if record.get("analysis", {}).get("result_status") != "imported-agent-guided":
        raise ValueError("Planning requires a matching Investigator result.")
    repository = (workspace / record["target"]["checkout"]).resolve(strict=True)
    report = discover(repository)
    _validate_binding(record, workspace, report)
    result = investigator_result(record, path.parent)
    if result is None:
        raise ValueError("The Investigator handoff is pending.")
    proposal = _proposal(result)
    instructions = [
        ".github\\skills\\portwright\\SKILL.md",
        "instructions\\investigate.md",
        "instructions\\engineer.md",
        "instructions\\examine.md",
    ]
    instruction_hashes = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in instructions
    }
    binding = json.dumps(
        {
            "format": PLAN_FORMAT_VERSION,
            "run_id": run_id,
            "analysis_id": record["analysis"]["id"],
            "source": report["source_fingerprint"],
            "proposal": proposal,
            "instructions": instruction_hashes,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    plan_id = hashlib.sha256(binding.encode()).hexdigest()
    content = _render(record, report, proposal, plan_id)
    brief = path.parent / "plan.md"
    existing = record.get("plan")
    if existing:
        if existing.get("id") != plan_id:
            raise ValueError("A different migration brief already exists for this run.")
        if not brief.is_file() or hashlib.sha256(brief.read_bytes()).hexdigest() != existing["sha256"]:
            raise ValueError("The recorded migration brief is missing or changed.")
        return record
    _write_text(brief, content)
    approval_id = f"probe-{plan_id[:12]}"
    record.setdefault("approvals", []).append(
        {
            "id": approval_id,
            "category": "probe",
            "state": "requested-not-approved",
            "plan_id": plan_id,
            "source_edits_authorized": False,
            "execution_authorized": False,
            "requested_scope": proposal.get("first_probe"),
        }
    )
    record.update(
        stage="planning",
        stop_after_stage="planning",
        active_role=None,
        status="plan-ready-awaiting-approval",
    )
    record["route"] = {
        "recommended": proposal["route"],
        "selected": proposal["route"],
        "approved": False,
        "alternatives": proposal["alternatives"],
    }
    record["plan"] = {
        "format_version": PLAN_FORMAT_VERSION,
        "id": plan_id,
        "path": "plan.md",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "source_revision": report["repository"]["revision"],
        "source_fingerprint": report["source_fingerprint"],
        "working_tree_fingerprint": report["repository"]["working_tree_fingerprint"],
        "instruction_sha256": instruction_hashes,
        "approval_id": approval_id,
        "proposal": proposal,
        "created_at_utc": started,
        "execution_started": False,
    }
    record.setdefault("capabilities", {})["plan"] = "implemented-source-bound-migration-brief"
    record["next_action"] = "Review the migration brief and approve or reject the named probe."
    ended = utc_now()
    timing = record.setdefault("timing", {}).setdefault("intervals", [])
    elapsed = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    timing.append(
        {"stage": "planning", "activity": "plan", "started_at_utc": started, "ended_at_utc": ended, "elapsed_seconds": elapsed}
    )
    _write_json(path, record, previous)
    return record


def render_plan(record: dict, directory: Path) -> str:
    plan_record = record.get("plan")
    if not plan_record:
        raise ValueError("No migration brief has been recorded.")
    proposal = plan_record["proposal"]
    lines = [
        f"PORTWRIGHT PLAN {record['run_id']}",
        f"Brief: {directory / plan_record['path']}",
        f"Source: {plan_record['source_revision'] or 'unversioned'}",
        f"Route: {proposal['route']} (not approved)",
        f"Changes: {len(proposal['changes'])}",
        f"First probe: {proposal.get('first_probe', {}).get('objective', 'none')}",
        "Status: plan ready; awaiting approval",
        "Migration: not started",
    ]
    return "\n".join(lines) + "\n"

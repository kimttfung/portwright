"""Record explicit operator approval for migration or staging."""

import json
import os
import tempfile
from pathlib import Path

from .workflow import read_json, run_path, utc_now


def _write(path: Path, record: dict, expected: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-approval-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        json.dump(record, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if path.read_bytes() != expected:
            raise ValueError("The run changed concurrently; approval was not recorded.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def approve(workspace: Path, run_id: str, scope: str) -> dict:
    workspace = workspace.resolve(strict=True)
    path = run_path(workspace, run_id)
    before = path.read_bytes()
    record = read_json(path)
    if scope == "migration":
        plan = record.get("plan")
        if not isinstance(plan, dict):
            raise ValueError("Migration approval requires a recorded migration brief.")
        approval = next(
            (
                item
                for item in record.get("approvals", [])
                if item.get("id") == plan.get("approval_id")
            ),
            None,
        )
        if not approval:
            raise ValueError("The migration approval request is missing.")
        if approval.get("state") == "approved-executed":
            return record
        if approval.get("state") not in {"requested-not-approved", "approved-not-executed"}:
            raise ValueError("The migration approval is not in an approvable state.")
        approval.update(
            state="approved-not-executed",
            source_edits_authorized=True,
            execution_authorized=True,
            approved_at_utc=utc_now(),
        )
        record["route"]["approved"] = True
        record["status"] = "migration-approved"
        record["next_action"] = "Run migrate to create or import the bounded Engineer task."
    elif scope == "staging":
        migration = record.get("migration", {})
        if migration.get("result_status") != "imported-agent-guided":
            raise ValueError("Staging approval requires an imported Engineer result.")
        approval = next(
            (
                item
                for item in record.get("approvals", [])
                if item.get("id") == "staging-and-verification"
            ),
            None,
        )
        if approval is None:
            approval = {
                "id": "staging-and-verification",
                "category": "staging-and-verification",
                "state": "approved-staging-not-launched",
                "maintained_target_source_edits_authorized": False,
                "approved_at_utc": utc_now(),
            }
            record.setdefault("approvals", []).append(approval)
        elif approval.get("state") != "approved-staging-not-launched":
            raise ValueError("The staging approval is not in an approvable state.")
        record["status"] = "staging-approved"
        record["next_action"] = "Run stage with the approved staging recipe."
    else:
        raise ValueError("Approval scope must be migration or staging.")
    _write(path, record, before)
    return record


def render_approval(record: dict, scope: str) -> str:
    return "\n".join(
        [
            f"PORTWRIGHT APPROVE {record['run_id']}",
            f"Scope: {scope}",
            f"Status: {record['status']}",
            f"Next: {record['next_action']}",
        ]
    ) + "\n"

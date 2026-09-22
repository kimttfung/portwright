import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .approval import approve, render_approval
from .migration import migrate, render_migration
from .planning import plan, render_plan
from .staging import render_stage, stage_run
from .verification import render_verification, verify
from .workflow import analyze, read_json, render, run_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portwright")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    analysis = commands.add_parser("analyze", help="Inspect source and emit an agent-guided task.")
    analysis.add_argument("repository", type=Path)
    analysis.add_argument("--run-id")
    planning = commands.add_parser("plan", help="Create a source-bound migration brief.")
    planning.add_argument("run_id")
    migration = commands.add_parser("migrate", help="Prepare or import an approved Engineer task.")
    migration.add_argument("run_id")
    approval = commands.add_parser("approve", help="Approve migration execution or staging.")
    approval.add_argument("run_id")
    approval.add_argument("scope", choices=("migration", "staging"))
    staging = commands.add_parser("stage", help="Stage an application from an approved recipe.")
    staging.add_argument("run_id")
    staging.add_argument("recipe", type=Path)
    verification = commands.add_parser("verify", help="Check staged artifacts and import Examiner results.")
    verification.add_argument("run_id")
    status = commands.add_parser("status", help="Read saved state and handoff results offline.")
    status.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        workspace = args.workspace.resolve(strict=True)
        if args.command == "analyze":
            record = analyze(workspace, args.repository, args.run_id)
            path = run_path(workspace, record["run_id"])
            output = render(record, path.parent, True)
        elif args.command == "plan":
            record = plan(workspace, args.run_id)
            path = run_path(workspace, record["run_id"])
            output = render_plan(record, path.parent)
        elif args.command == "migrate":
            record = migrate(workspace, args.run_id)
            path = run_path(workspace, record["run_id"])
            output = render_migration(record, path.parent)
        elif args.command == "approve":
            record = approve(workspace, args.run_id, args.scope)
            output = render_approval(record, args.scope)
        elif args.command == "stage":
            record, manifest = stage_run(workspace, args.run_id, args.recipe)
            output = render_stage(record, manifest)
        elif args.command == "verify":
            record = verify(workspace, args.run_id)
            path = run_path(workspace, record["run_id"])
            output = render_verification(record, workspace)
        else:
            path = run_path(workspace, args.run_id)
            record = read_json(path)
            if record.get("run_id") != args.run_id:
                raise ValueError("The saved run ID does not match its directory.")
            output = (
                render_verification(record, workspace)
                if record.get("verification")
                else render_migration(record, path.parent)
                if record.get("migration")
                else render_plan(record, path.parent)
                if record.get("plan")
                else render(record, path.parent, False)
            )
        print(output, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as error:
        print(f"portwright: {error}", file=sys.stderr)
        return 1

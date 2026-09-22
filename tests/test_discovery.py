import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from portwright.approval import approve
from portwright.discovery import discover
from portwright.migration import migrate
from portwright.planning import plan
from portwright.workflow import analyze, run_path


PROJECT = """<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Release|ARM64">
      <Configuration>Release</Configuration><Platform>ARM64</Platform>
    </ProjectConfiguration>
  </ItemGroup>
  <ItemGroup>
    <ClCompile Include="main.cpp"><PrecompiledHeader Condition="'$(Platform)'=='x64'">Create</PrecompiledHeader></ClCompile>
  </ItemGroup>
</Project>
"""


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.repository = root / "target"
        self.workspace.mkdir()
        self.repository.mkdir()
        for relative in (
            ".github\\skills\\portwright\\SKILL.md",
            "instructions\\investigate.md",
            "instructions\\engineer.md",
            "instructions\\examine.md",
        ):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n", encoding="utf-8")
        (self.repository / "app.vcxproj").write_text(PROJECT, encoding="utf-8")
        (self.repository / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
        (self.repository / "package.json").write_text(
            json.dumps({"devDependencies": {"electron": "43.4.0"}}), encoding="utf-8"
        )
        (self.repository / "Cargo.toml").write_text(
            '[package]\nname="fixture"\nversion="0.1.0"\n', encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _proposal(self):
        return {
            "route": "native-arm64-with-dependency-remediation",
            "useful_task": "Launch the staged fixture and complete its synthetic task.",
            "rationale": "The native target exists, but one build declaration is incomplete.",
            "evidence": ["Release|ARM64 is declared."],
            "unknowns": ["Runtime behavior has not been checked."],
            "alternatives": {"arm64ec": "No retained x64 in-process dependency is known."},
            "changes": [
                {
                    "id": "build-metadata",
                    "title": "Repair target metadata.",
                    "description": "Extend the existing per-file rule to Arm64.",
                    "depends_on": [],
                }
            ],
            "behavior_to_preserve": ["Existing non-Arm configurations."],
            "fixtures": ["Synthetic fixture."],
            "expected_outputs": ["Native staged executable."],
            "required_components": ["Main executable."],
            "acceptance_checks": ["Architecture, launch, task, regression, and cleanup pass."],
            "stop_conditions": ["Stop after two distinct failed approaches."],
            "first_probe": {
                "objective": "Apply and check the bounded metadata repair.",
                "command": "fixture-build",
                "working_directory": ".",
                "output_root": "out\\arm64",
                "expected_result": "The declared target builds.",
                "time_limit_seconds": 300,
                "authorized_files": ["app.vcxproj"],
                "environment": {},
            },
        }

    def _analyzed_with_result(self):
        record = analyze(self.workspace, self.repository, "fixture-run")
        result_path = run_path(self.workspace, "fixture-run").parent / record["analysis"]["result_path"]
        result_path.write_text(
            json.dumps(
                {
                    "analysis_id": record["analysis"]["id"],
                    "run_id": "fixture-run",
                    "role": "Investigator",
                    "execution_mode": "agent-guided",
                    "source_fingerprint": record["analysis"]["report"]["source_fingerprint"],
                    "summary": "The target exists and needs one bounded declaration repair.",
                    "recommendation": "Use native Arm64 with dependency remediation.",
                    "migration_brief": self._proposal(),
                    "limits": ["No target command was run."],
                }
            ),
            encoding="utf-8",
        )
        return analyze(self.workspace, self.repository, "fixture-run")

    def test_discovery_inventories_multiple_ecosystems(self):
        report = discover(self.repository)
        self.assertIn("MSBuild / C++", report["stack"])
        self.assertIn("Electron / Node.js", report["stack"])
        self.assertIn("Rust / Cargo", report["stack"])
        self.assertIsNone(report["candidate_route"])
        self.assertTrue(report["arm64_support"].startswith("declared"))

    def test_discovery_reports_path_risks_without_changing_route(self):
        cloud_repository = Path(self.temporary.name) / "OneDrive - Example" / "target with spaces"
        shutil.copytree(self.repository, cloud_repository)
        report = discover(cloud_repository)
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("checkout-path-spaces", codes)
        self.assertIn("cloud-synced-checkout", codes)
        self.assertIsNone(report["candidate_route"])

    def test_analyze_creates_bound_read_only_task(self):
        record = analyze(self.workspace, self.repository, "fixture-run")
        self.assertEqual(record["stage"], "discovery")
        self.assertEqual(record["status"], "discovery-awaiting-investigator")
        task = json.loads(
            (run_path(self.workspace, "fixture-run").parent / record["analysis"]["task_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(task["permissions"]["target_execution"])
        self.assertIn("migration_brief", task["return_fields"])

    def test_reusable_agent_files_have_no_application_or_stage_labels(self):
        root = Path(__file__).parents[1]
        files = [
            *(root / "instructions").glob("*.md"),
            *(root / "patterns").glob("*.md"),
            *(root / ".github" / "skills" / "portwright").rglob("*.md"),
        ]
        forbidden = re.compile(
            r"\b(?:Ditto|OxiPNG|Stretchly|Caesium|Czkawka|Krokiet|Kando|C(?:[0-9]|10)|App-\d+)\b",
            re.IGNORECASE,
        )
        matches = [
            str(path.relative_to(root))
            for path in files
            if forbidden.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(matches, [])

    def test_plan_is_source_bound_and_semantic(self):
        self._analyzed_with_result()
        record = plan(self.workspace, "fixture-run")
        self.assertEqual(record["stage"], "planning")
        self.assertEqual(record["route"]["selected"], "native-arm64-with-dependency-remediation")
        brief = (run_path(self.workspace, "fixture-run").parent / "plan.md").read_text(encoding="utf-8")
        self.assertIn("Migration brief", brief)
        self.assertNotRegex(brief, r"\bC[0-9]\b")

    def test_planning_rejects_a_changed_baseline(self):
        self._analyzed_with_result()
        (self.repository / "app.vcxproj").write_text(PROJECT + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after discovery"):
            plan(self.workspace, "fixture-run")

    def test_migration_exchanges_a_bounded_task_and_result(self):
        self._analyzed_with_result()
        plan(self.workspace, "fixture-run")
        planned = approve(self.workspace, "fixture-run", "migration")
        run = run_path(self.workspace, "fixture-run")
        self.assertEqual(planned["status"], "migration-approved")
        pending = migrate(self.workspace, "fixture-run")
        self.assertEqual(pending["stage"], "engineering")
        self.assertEqual(pending["migration"]["authorized_files"], ["app.vcxproj"])
        changed = PROJECT.replace("=='x64'", "=='ARM64'")
        source = self.repository / "app.vcxproj"
        source.write_text(changed, encoding="utf-8")
        result_path = run.parent / pending["migration"]["result_path"]
        result_path.write_text(
            json.dumps(
                {
                    "migration_id": pending["migration"]["id"],
                    "run_id": "fixture-run",
                    "role": "Engineer",
                    "execution_mode": "agent-guided",
                    "automatic_cli_invocation": False,
                    "plan_id": pending["plan"]["id"],
                    "approval_id": pending["migration"]["approval_id"],
                    "changed_files": [
                        {
                            "path": "app.vcxproj",
                            "after_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        }
                    ],
                    "commands": ["fixture-build"],
                    "checks": [{"name": "fixture build", "status": "passed"}],
                    "artifacts": [],
                    "remaining_unknowns": ["Application verification remains."],
                }
            ),
            encoding="utf-8",
        )
        completed = migrate(self.workspace, "fixture-run")
        self.assertEqual(completed["status"], "engineering-result-imported")
        self.assertTrue(completed["approvals"][0]["execution_completed"])
        self.assertEqual(migrate(self.workspace, "fixture-run")["status"], "engineering-result-imported")
        staged = approve(self.workspace, "fixture-run", "staging")
        self.assertEqual(staged["status"], "staging-approved")

    def test_engineer_result_rejects_file_outside_approved_scope(self):
        self._analyzed_with_result()
        plan(self.workspace, "fixture-run")
        approve(self.workspace, "fixture-run", "migration")
        pending = migrate(self.workspace, "fixture-run")
        run = run_path(self.workspace, "fixture-run")
        unauthorized = self.repository / "unauthorized.txt"
        unauthorized.write_text("not approved\n", encoding="utf-8")
        result_path = run.parent / pending["migration"]["result_path"]
        result_path.write_text(
            json.dumps(
                {
                    "migration_id": pending["migration"]["id"],
                    "run_id": "fixture-run",
                    "role": "Engineer",
                    "execution_mode": "agent-guided",
                    "automatic_cli_invocation": False,
                    "plan_id": pending["plan"]["id"],
                    "approval_id": pending["migration"]["approval_id"],
                    "changed_files": [
                        {
                            "path": "unauthorized.txt",
                            "after_sha256": hashlib.sha256(unauthorized.read_bytes()).hexdigest(),
                        }
                    ],
                    "commands": ["fixture-build"],
                    "checks": [{"name": "fixture build", "status": "passed"}],
                    "artifacts": [],
                    "remaining_unknowns": [],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "approved source scope"):
            migrate(self.workspace, "fixture-run")


if __name__ == "__main__":
    unittest.main()

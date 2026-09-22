import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from portwright.staging import sha256, stage, stage_run
from portwright.verification import (
    DEFAULT_CHECKS,
    examiner_result,
    render_verification,
    static_checks,
    verify,
)
from portwright.workflow import run_path


def write_pe(path: Path, machine: int = 0xAA64) -> None:
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.sources = self.workspace / "sources"
        self.sources.mkdir()
        write_pe(self.sources / "App.exe")
        write_pe(self.sources / "Worker.dll")
        (self.sources / "LICENSE").write_text("fixture license\n", encoding="utf-8")
        self.run = run_path(self.workspace, "fixture-run")
        self.run.parent.mkdir(parents=True)
        (self.run.parent / "plan.md").write_text("fixture plan\n", encoding="utf-8")
        self.run.write_text(
            json.dumps(
                {
                    "run_id": "fixture-run",
                    "stage": "engineering",
                    "execution_mode": "agent-guided",
                    "approvals": [
                        {
                            "id": "stage-fixture",
                            "state": "approved-staging-not-launched",
                            "maintained_target_source_edits_authorized": False,
                        }
                    ],
                    "plan": {"id": "fixture-plan", "path": "plan.md"},
                }
            ),
            encoding="utf-8",
        )
        self.recipe = self.workspace / "recipe.json"
        self.recipe.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "run_id": "fixture-run",
                    "approval_id": "stage-fixture",
                    "attempt": "attempt-01",
                    "stage_root": "runs\\fixture-run\\staging\\attempt-01",
                    "folder_name": "FixtureApp",
                    "zip_name": "fixture-arm64.zip",
                    "directories": ["plugins"],
                    "files": [
                        {
                            "source": str(self.sources / "App.exe"),
                            "destination": "App.exe",
                            "role": "main",
                            "sha256": sha256(self.sources / "App.exe"),
                            "machine": "ARM64",
                        },
                        {
                            "source": str(self.sources / "Worker.dll"),
                            "destination": "plugins\\Worker.dll",
                            "role": "required-component",
                            "sha256": sha256(self.sources / "Worker.dll"),
                            "machine": "ARM64",
                        },
                        {
                            "source": str(self.sources / "LICENSE"),
                            "destination": "LICENSE",
                            "role": "notice",
                            "sha256": sha256(self.sources / "LICENSE"),
                        },
                    ],
                    "verification": {
                        "required_checks": list(DEFAULT_CHECKS),
                        "allow_launch": True,
                        "task": "Launch the fixture and complete the synthetic task.",
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _staged_record(self):
        manifest = stage(self.workspace, "fixture-run", self.recipe)
        record = json.loads(self.run.read_text(encoding="utf-8"))
        record["staging"] = {
            "manifest": str(
                Path(manifest["folder"])
                .parent.joinpath("staging-manifest.json")
                .resolve()
                .relative_to(self.workspace.resolve())
            ),
            "verification_root": str(
                (self.workspace / "runs\\fixture-run\\verification\\attempt-01").resolve()
            ),
        }
        self.run.write_text(json.dumps(record), encoding="utf-8")
        return manifest

    def test_fresh_stage_and_static_verification(self):
        record, manifest = stage_run(self.workspace, "fixture-run", self.recipe)
        self.assertTrue(all(item["status"] == "passed" for item in static_checks(manifest)))
        self.assertTrue(Path(manifest["zip"]).is_file())
        self.assertEqual(record["status"], "staging-complete")
        self.assertIn("manifest", record["staging"])
        repeated_record, repeated_manifest = stage_run(
            self.workspace, "fixture-run", self.recipe
        )
        self.assertEqual(repeated_record["staging"], record["staging"])
        self.assertEqual(repeated_manifest["zip_sha256"], manifest["zip_sha256"])

    def test_stage_rejects_recipe_without_exactly_one_main(self):
        recipe = json.loads(self.recipe.read_text(encoding="utf-8"))
        recipe["files"][0]["role"] = "payload"
        invalid = self.workspace / "invalid-recipe.json"
        invalid.write_text(json.dumps(recipe), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            stage(self.workspace, "fixture-run", invalid)

    def test_missing_and_replaced_artifacts_fail_visibly(self):
        manifest = stage(self.workspace, "fixture-run", self.recipe)
        worker = Path(manifest["folder"]) / "plugins\\Worker.dll"
        saved = worker.read_bytes()
        worker.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            static_checks(manifest)
        worker.write_bytes(saved + b"x")
        with self.assertRaisesRegex(ValueError, "replaced"):
            static_checks(manifest)

    def test_verify_emits_generic_examiner_task(self):
        self._staged_record()
        record = verify(self.workspace, "fixture-run")
        self.assertEqual(record["status"], "static-checks-passed-awaiting-examiner")
        self.assertEqual(record["stage"], "verification")
        task = json.loads((self.run.parent / record["verification"]["task_path"]).read_text())
        self.assertEqual(task["required_checks"], list(DEFAULT_CHECKS))
        self.assertTrue(task["permissions"]["staged_app_launch"])

    def test_verify_accepts_an_absolute_manifest_outside_workspace(self):
        manifest = self._staged_record()
        external = Path(self.temporary.name).parent / f"{Path(self.temporary.name).name}-external"
        external.mkdir()
        source_manifest = Path(manifest["folder"]).parent / "staging-manifest.json"
        external_manifest = external / "staging-manifest.json"
        external_manifest.write_bytes(source_manifest.read_bytes())
        record = json.loads(self.run.read_text(encoding="utf-8"))
        record["staging"]["manifest"] = str(external_manifest)
        self.run.write_text(json.dumps(record), encoding="utf-8")
        try:
            verified = verify(self.workspace, "fixture-run")
            self.assertEqual(verified["verification"]["manifest"], str(external_manifest))
        finally:
            external_manifest.unlink(missing_ok=True)
            external.rmdir()

    def test_verify_accepts_external_verification_root(self):
        self._staged_record()
        external = Path(self.temporary.name).parent / f"{Path(self.temporary.name).name}-verify"
        record = json.loads(self.run.read_text(encoding="utf-8"))
        record["staging"]["verification_root"] = str(external)
        self.run.write_text(json.dumps(record), encoding="utf-8")
        try:
            verified = verify(self.workspace, "fixture-run")
            self.assertTrue(Path(verified["verification"]["result_path"]).is_absolute())
        finally:
            if external.exists():
                import shutil

                shutil.rmtree(external)

    def test_status_renders_generic_recipe_claims(self):
        recipes = self.workspace / "recipes"
        recipes.mkdir()
        (recipes / "sample.json").write_text(
            json.dumps(
                {
                    "display_name": "Sample application",
                    "role": "example",
                    "completion": "IMPLEMENTED",
                    "verified_claims": ["Native package built and its declared task passed."],
                    "limitations": ["Hosted CI was not run."],
                }
            ),
            encoding="utf-8",
        )
        pattern = self.workspace / "patterns" / "sample.md"
        pattern.parent.mkdir()
        pattern.write_text("sample\n", encoding="utf-8")
        record = {
            "run_id": "fixture-run",
            "target": {"repository": "https://example.invalid/repo", "requested_revision": "abc"},
            "route": {"selected": "packaging-release-repair"},
            "execution_mode": "agent-guided",
            "recipes": {"sample": {"path": "recipes\\sample.json"}},
            "patterns": [
                {
                    "path": "patterns\\sample.md",
                    "status": "observed-once",
                    "promoted_sha256": hashlib.sha256(pattern.read_bytes()).hexdigest(),
                    "reused_and_checked": False,
                }
            ],
            "next_action": "Review the evidence.",
        }
        output = render_verification(record, self.workspace)
        self.assertIn("Native package built", output)
        self.assertIn("Hosted CI was not run", output)
        self.assertIn("sample: observed-once; no checked reuse", output)
        self.assertNotRegex(output, r"\bC[0-9]\b")

    def test_status_marks_missing_recipe_unknown(self):
        record = {
            "run_id": "fixture-run",
            "target": {"repository": "https://example.invalid/repo", "requested_revision": "abc"},
            "route": {"selected": "blocked"},
            "recipes": {"missing": {"path": "recipes\\missing.json"}},
            "next_action": "Supply evidence.",
        }
        output = render_verification(record, self.workspace)
        self.assertIn("UNKNOWN / MISSING EVIDENCE", output)

    def _examiner_fixture(self):
        root = self.workspace / "runs" / "fixture-run"
        root.mkdir(parents=True, exist_ok=True)
        result_path = root / "examiner-result.json"
        result = {
            "verification_id": "verification-id",
            "run_id": "fixture-run",
            "role": "Examiner",
            "execution_mode": "agent-guided",
            "automatic_cli_invocation": False,
            "manifest_sha256": "b" * 64,
            "overall": "passed",
            "checks": {name: {"status": "passed"} for name in DEFAULT_CHECKS},
            "target_source_changed": False,
            "acceptance_criteria_changed": False,
        }
        record = {
            "run_id": "fixture-run",
            "verification": {
                "id": "verification-id",
                "result_path": "examiner-result.json",
                "manifest_sha256": "b" * 64,
                "required_checks": list(DEFAULT_CHECKS),
            },
        }
        return root, result_path, record, result

    def test_examiner_import_accepts_complete_result(self):
        root, path, record, result = self._examiner_fixture()
        path.write_text(json.dumps(result), encoding="utf-8")
        self.assertEqual(examiner_result(record, root), result)

    def test_examiner_import_rejects_missing_or_nonpassing_check(self):
        root, path, record, result = self._examiner_fixture()
        result["checks"].pop("cleanup")
        path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cleanup"):
            examiner_result(record, root)
        root, path, record, result = self._examiner_fixture()
        result["checks"]["launch"]["status"] = "blocked"
        path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "launch"):
            examiner_result(record, root)

    def test_examiner_import_rejects_changed_boundaries(self):
        for field, value in (
            ("verification_id", "stale"),
            ("role", "Engineer"),
            ("automatic_cli_invocation", True),
            ("target_source_changed", True),
            ("acceptance_criteria_changed", True),
        ):
            with self.subTest(field=field):
                root, path, record, result = self._examiner_fixture()
                result[field] = value
                path.write_text(json.dumps(result), encoding="utf-8")
                with self.assertRaises(ValueError):
                    examiner_result(record, root)


if __name__ == "__main__":
    unittest.main()

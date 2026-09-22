"""Generic staged-artifact checks, Examiner handoff, and evidence status."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .staging import pe_machine, sha256
from .workflow import read_json, run_path, utc_now

ALLOWED_OVERALL = {"passed", "failed", "blocked"}
ALLOWED_CHECK_STATUSES = {"passed", "failed", "blocked", "unknown", "not-run"}
DEFAULT_CHECKS = ("architecture", "components", "launch", "user_task", "regression", "cleanup")


def _manifest(workspace: Path, record: dict) -> tuple[Path, dict]:
    value = record.get("staging", {}).get("manifest")
    if not value:
        raise ValueError("No staged manifest is recorded.")
    path = (workspace / value).resolve(strict=True)
    return path, read_json(path)


def static_checks(manifest: dict, allowed_generated: list[str] | None = None) -> list[dict]:
    folder = Path(manifest["folder"]).resolve(strict=True)
    expected = {"Portwright-manifest.json"}
    results = []
    for item in manifest["files"]:
        expected.add(item["path"].replace("\\", "/"))
        path = folder / item["path"]
        if not path.is_file():
            raise ValueError(f"Required staged artifact is missing: {item['path']}")
        if sha256(path) != item["sha256"]:
            raise ValueError(f"Checked staged artifact was replaced: {item['path']}")
        machine = pe_machine(path)
        if item.get("machine") and machine != item["machine"]:
            raise ValueError(f"Wrong staged architecture: {item['path']}")
        results.append({"check": item["path"], "status": "passed", "machine": machine})
    actual = {
        str(path.relative_to(folder)).replace("\\", "/")
        for path in folder.rglob("*")
        if path.is_file()
    }
    allowed = {value.replace("\\", "/") for value in (allowed_generated or [])}
    missing = expected - actual
    unexpected = actual - expected - allowed
    if missing:
        raise ValueError(f"Required staged artifact is missing: {sorted(missing)}")
    if unexpected:
        raise ValueError(f"Staged file set differs from manifest: {sorted(unexpected)}")
    zip_path = Path(manifest["zip"]).resolve(strict=True)
    if sha256(zip_path) != manifest["zip_sha256"]:
        raise ValueError("The staged ZIP identity changed.")
    with zipfile.ZipFile(zip_path) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}
    root = manifest["folder_name"]
    zip_expected = {f"{root}/{name}" for name in expected}
    if names != zip_expected:
        raise ValueError("The ZIP contents differ from the staged folder.")
    results.append({"check": "ZIP identity and contents", "status": "passed"})
    return results


def negative_checks(root: Path, manifest: dict) -> list[dict]:
    root.mkdir(parents=True, exist_ok=False)
    results = []
    command = subprocess.run([sys.executable, "-c", "raise SystemExit(7)"], check=False)
    results.append(
        {
            "check": "required command failure",
            "status": "passed" if command.returncode == 7 else "failed",
            "observed_exit_code": command.returncode,
        }
    )
    missing = root / "missing"
    missing.mkdir()
    try:
        static_checks({**manifest, "folder": str(missing)})
        visible = False
    except ValueError as error:
        visible = "missing" in str(error)
    results.append({"check": "missing required artifact", "status": "passed" if visible else "failed"})
    main = next((item for item in manifest["files"] if item.get("role") == "main"), None)
    if main is None:
        raise ValueError("The staging manifest needs one file with role=main.")
    replaced_root = root / "replaced"
    shutil.copytree(Path(manifest["folder"]), replaced_root)
    source = Path(manifest["folder"]) / main["path"]
    replacement = replaced_root / main["path"]
    replacement.write_bytes(source.read_bytes() + b"\0")
    try:
        static_checks({**manifest, "folder": str(replaced_root)})
        replaced_visible = False
    except ValueError as error:
        replaced_visible = "replaced" in str(error)
    results.append(
        {
            "check": "replaced checked artifact",
            "status": "passed" if replaced_visible else "failed",
        }
    )
    if any(item["status"] != "passed" for item in results):
        raise ValueError("One or more bounded failure fixtures did not fail visibly.")
    return results


def examiner_result(record: dict, root: Path) -> dict | None:
    verification = record.get("verification")
    if not verification:
        return None
    path = root / verification["result_path"]
    if not path.exists():
        return None
    result = read_json(path)
    expected = {
        "verification_id": verification["id"],
        "run_id": record["run_id"],
        "role": "Examiner",
        "execution_mode": "agent-guided",
        "manifest_sha256": verification["manifest_sha256"],
        "automatic_cli_invocation": False,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise ValueError(f"Examiner result has a stale or invalid {key}.")
    for key in ("target_source_changed", "acceptance_criteria_changed"):
        if key not in result or result[key] is not False:
            raise ValueError(f"Examiner result must explicitly set {key} to false.")
    if result.get("overall") not in ALLOWED_OVERALL:
        raise ValueError("Examiner result has an invalid overall status.")
    required = verification.get("required_checks") or list(DEFAULT_CHECKS)
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("Examiner result has no checks object.")
    for name in required:
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") not in ALLOWED_CHECK_STATUSES:
            raise ValueError(f"Examiner result is missing or invalid for: {name}.")
        if result["overall"] == "passed" and check["status"] != "passed":
            raise ValueError(f"A passing result requires {name} to pass.")
    return result


def verify(workspace: Path, run_id: str) -> dict:
    workspace = workspace.resolve(strict=True)
    path = run_path(workspace, run_id)
    before = path.read_bytes()
    record = read_json(path)
    manifest_path, manifest = _manifest(workspace, record)
    checks = static_checks(manifest, record.get("staging", {}).get("runtime_generated_files", []))
    root_value = Path(record["staging"]["verification_root"])
    root = (
        root_value.resolve()
        if root_value.is_absolute()
        else (workspace / root_value).resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    negatives_path = root / "negative-results.json"
    if negatives_path.exists():
        negatives = read_json(negatives_path)["results"]
    else:
        negatives = negative_checks(root / "negative-fixtures", manifest)
        negatives_path.write_text(json.dumps({"results": negatives}, indent=2) + "\n", encoding="utf-8")
    contract = manifest.get("verification_contract", {})
    required = contract.get("required_checks") or list(DEFAULT_CHECKS)
    binding = json.dumps(
        {
            "run_id": run_id,
            "plan_id": record["plan"]["id"],
            "manifest_sha256": sha256(manifest_path),
            "required_checks": required,
        },
        sort_keys=True,
    )
    verification_id = hashlib.sha256(binding.encode()).hexdigest()
    task_path = str(Path("logs") / f"examiner-{verification_id[:16]}.task.json")
    try:
        result_path = str(root.relative_to(workspace) / "examiner-result.json")
        result_destination = workspace / result_path
    except ValueError:
        result_path = str(root / "examiner-result.json")
        result_destination = Path(result_path)
    try:
        recorded_manifest = str(manifest_path.relative_to(workspace))
    except ValueError:
        recorded_manifest = str(manifest_path)
    task = {
        "verification_id": verification_id,
        "run_id": run_id,
        "role": "Examiner",
        "execution_mode": "agent-guided",
        "automatic_cli_invocation": False,
        "plan_path": str(path.parent / record["plan"]["path"]),
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "static_checks": checks,
        "negative_checks": negatives,
        "required_checks": required,
        "task": contract.get("task", "Verify the staged application against the approved brief."),
        "permissions": {
            "target_source": "read-only",
            "staged_app_launch": bool(contract.get("allow_launch")),
            "verification_root": str(root),
            "result_path": str(result_destination),
        },
        "handoff": "A fresh approved host context performs the checks. Portwright did not invoke it.",
    }
    task_file = path.parent / task_path
    if not task_file.exists():
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    record["verification"] = {
        "id": verification_id,
        "task_path": task_path,
        "result_path": result_path,
        "manifest": recorded_manifest,
        "manifest_sha256": sha256(manifest_path),
        "static_checks": checks,
        "negative_checks": negatives,
        "required_checks": required,
        "agent_invoked": False,
        "result_status": "pending",
        "created_at_utc": utc_now(),
    }
    result = examiner_result(record, workspace)
    if result:
        record["verification"]["result_status"] = "imported-agent-guided"
        record["verification"]["result"] = result
        record["status"] = "verification-complete" if result["overall"] == "passed" else "verification-incomplete"
    else:
        record["status"] = "static-checks-passed-awaiting-examiner"
    record.update(stage="verification", stop_after_stage="verification", active_role=None)
    record.setdefault("capabilities", {})["verify"] = "implemented-static-checks-and-agent-guided-examiner"
    record["next_action"] = (
        "Review the completed verification result and evidence."
        if result
        else "Complete the fresh Examiner handoff, then review the result."
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".portwright-verify-", suffix=".tmp", delete=False
    ) as file:
        temporary = Path(file.name)
        json.dump(record, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if path.read_bytes() != before:
            raise ValueError("The run changed concurrently.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def _recipe(workspace: Path | None, record: dict, key: str) -> dict | None:
    if workspace is None:
        return None
    value = record.get("recipes", {}).get(key, {}).get("path")
    if not value:
        return None
    path = (workspace / value).resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _passed_examiner(record: dict) -> bool:
    result = record.get("verification", {}).get("result")
    if not isinstance(result, dict) or result.get("overall") != "passed":
        return False
    required = record["verification"].get("required_checks") or list(DEFAULT_CHECKS)
    checks = result.get("checks", {})
    return all(checks.get(name, {}).get("status") == "passed" for name in required)


def _payload(recipe: dict) -> dict:
    legacy = recipe.get("portwright")
    return legacy if isinstance(legacy, dict) else recipe


def _claims(recipe: dict, field: str) -> list[str]:
    payload = _payload(recipe)
    value = payload.get(field, [])
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    if field != "verified_claims":
        return []
    claims = []
    for key in ("changes", "demonstrated_behavior"):
        items = payload.get(key, [])
        if isinstance(items, list):
            claims.extend(item for item in items if isinstance(item, str))
    achievement = payload.get("achievement")
    if isinstance(achievement, dict):
        useful = {
            "native_build",
            "architecture",
            "code_fix",
            "post_fix_test_result",
            "release_changes",
            "task",
        }
        claims.extend(
            value for key, value in achievement.items() if key in useful and isinstance(value, str)
        )
    task = payload.get("task")
    if isinstance(task, dict) and isinstance(task.get("result"), str):
        claims.append(task["result"])
    delivery = payload.get("primary_delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("type"), str):
        claims.append(delivery["type"])
    return claims


def _evidence_status(record: dict, workspace: Path | None) -> str:
    target = record.get("target", {})
    route = record.get("route", {})
    repository = record.get("analysis", {}).get("report", {}).get("repository", {})
    lines = [
        f"PORTWRIGHT STATUS {record.get('run_id', 'unknown')}",
        f"Repository: {target.get('repository') or repository.get('url') or repository.get('path') or target.get('checkout', 'unknown')} @ {target.get('requested_revision') or repository.get('revision') or 'unknown'}",
        f"Mode: {record.get('execution_mode', 'unknown')}",
        f"Selected route: {route.get('selected') or 'unknown'}",
    ]
    verified, imported, evaluated, limited, patterns, missing = [], [], [], [], [], []
    for key, item in record.get("recipes", {}).items():
        recipe = _recipe(workspace, record, key)
        if recipe is None:
            missing.append(f"Recipe evidence is unavailable: {key}")
            continue
        payload = _payload(recipe)
        repository = payload.get("repository")
        repository_name = (
            repository.rstrip("/").rsplit("/", 1)[-1]
            if isinstance(repository, str) and repository
            else None
        )
        name = recipe.get("display_name") or repository_name or key
        role = recipe.get("role", "example")
        completion = payload.get("completion", "unknown")
        claims = _claims(recipe, "verified_claims")
        evaluations = _claims(recipe, "evaluated_claims")
        if role == "primary" and completion == "IMPLEMENTED" and _passed_examiner(record):
            verified.extend(f"{name}: {claim}" for claim in claims)
        elif completion == "IMPLEMENTED":
            imported.extend(f"{name}: {claim}" for claim in claims)
        else:
            evaluated.extend(f"{name}: {claim}" for claim in claims + evaluations)
        limited.extend(f"{name}: {claim}" for claim in _claims(recipe, "limitations"))
    if workspace is not None:
        for item in record.get("patterns", []):
            value, status = item.get("path"), item.get("status")
            if not value or not status:
                continue
            path = (workspace / value).resolve()
            expected = item.get("promoted_sha256")
            if path.is_relative_to(workspace.resolve()) and path.is_file() and (
                not expected or sha256(path) == expected
            ):
                suffix = "" if item.get("reused_and_checked") else "; no checked reuse"
                patterns.append(f"{path.stem}: {status}{suffix}")
    if record.get("patterns") and not patterns:
        missing.append("Promoted pattern evidence is unavailable or changed")
    for title, values in (
        ("VERIFIED PRIMARY", verified),
        ("IMPORTED IMPLEMENTED WORK", imported),
        ("EVALUATED / PARTIAL", evaluated),
        ("LIMITATIONS", list(dict.fromkeys(limited))),
        ("PATTERNS", patterns),
        ("UNKNOWN / MISSING EVIDENCE", missing),
    ):
        if values:
            lines.extend(["", title])
            lines.extend(f"- {value}" for value in values)
    lines.extend(["", f"Next: {record.get('next_action', 'unknown')}"])
    return "\n".join(lines) + "\n"


def render_verification(record: dict, workspace: Path | None = None) -> str:
    if record.get("recipes"):
        return _evidence_status(record, workspace)
    verification = record["verification"]
    result = verification.get("result")
    repository = record.get("analysis", {}).get("report", {}).get("repository", {})
    lines = [
        f"PORTWRIGHT STATUS {record['run_id']}",
        f"Repository: {repository.get('url') or repository.get('path') or record.get('target', {}).get('checkout', 'unknown')}",
        f"Revision: {repository.get('revision') or 'unversioned'}",
        f"Selected route: {record.get('route', {}).get('selected') or 'unknown'}",
        f"Staged manifest: {verification['manifest']}",
        "Static artifacts/architectures: passed",
        "Failure fixtures: command failure, missing artifact and replaced artifact passed",
        "Examiner: result imported; not CLI-invoked" if result else "Examiner: pending fresh host handoff",
    ]
    if result:
        lines.append(f"Overall: {result['overall']}")
        lines.extend(
            f"{name}: {result['checks'][name]['status']}"
            for name in verification.get("required_checks", [])
        )
        limits = result.get("limits", [])
        if isinstance(limits, list) and limits:
            lines.extend(["", "LIMITATIONS"])
            lines.extend(f"- {value}" for value in limits if isinstance(value, str))
    patterns = record.get("patterns", [])
    if patterns:
        lines.extend(["", "PATTERNS"])
        for item in patterns:
            if item.get("path") and item.get("status"):
                suffix = "" if item.get("reused_and_checked") else "; no checked reuse"
                lines.append(f"- {Path(item['path']).stem}: {item['status']}{suffix}")
    lines.extend(["", f"Next: {record.get('next_action', 'unknown')}"])
    return "\n".join(lines) + "\n"

"""Create a fresh staged folder and archive from an explicit generic recipe."""

import hashlib
import json
import os
import shutil
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .workflow import read_json, run_path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pe_machine(path: Path) -> str | None:
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    return {0xAA64: "ARM64", 0x8664: "x64", 0x14C: "x86"}.get(machine, f"0x{machine:04X}")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-stage-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        json.dump(value, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if path.exists():
            raise ValueError(f"Refusing to replace staging evidence: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _approval(record: dict, approval_id: str) -> dict:
    value = next(
        (item for item in record.get("approvals", []) if item.get("id") == approval_id),
        None,
    )
    if not value or value.get("state") != "approved-staging-not-launched":
        raise ValueError("The exact staging scope is not approved.")
    if value.get("maintained_target_source_edits_authorized"):
        raise ValueError("Staging approval must not authorize target-source edits.")
    return value


def _validate_recipe(recipe: dict) -> None:
    files = recipe.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("The staging recipe needs a non-empty files list.")
    mains = [item for item in files if isinstance(item, dict) and item.get("role") == "main"]
    if len(mains) != 1:
        raise ValueError("The staging recipe needs exactly one file with role=main.")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each staging file must be an object.")
        for key in ("source", "destination", "role"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"Each staging file needs {key}.")
    verification = recipe.get("verification")
    if not isinstance(verification, dict):
        raise ValueError("The staging recipe needs a verification object.")
    checks = verification.get("required_checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(item, str) or not item for item in checks
    ):
        raise ValueError("The staging recipe needs required_checks.")
    if not isinstance(verification.get("allow_launch"), bool):
        raise ValueError("The staging recipe needs a boolean allow_launch.")


def stage(workspace: Path, run_id: str, recipe_path: Path) -> dict:
    workspace = workspace.resolve(strict=True)
    record = read_json(run_path(workspace, run_id))
    recipe_location = recipe_path if recipe_path.is_absolute() else workspace / recipe_path
    recipe = read_json(recipe_location.resolve(strict=True))
    if recipe.get("run_id") != run_id:
        raise ValueError("The staging recipe belongs to another run.")
    _validate_recipe(recipe)
    _approval(record, recipe.get("approval_id", "staging-and-verification"))
    attempt = (workspace / recipe["stage_root"]).resolve()
    if attempt.exists():
        raise ValueError("The staging attempt already exists; use a fresh attempt.")
    folder_name = recipe["folder_name"]
    folder = attempt / folder_name
    folder.mkdir(parents=True)
    for directory in recipe.get("directories", []):
        (folder / directory).mkdir(parents=True, exist_ok=True)
    files = []
    for item in recipe["files"]:
        source = Path(item["source"])
        if not source.is_absolute():
            source = (workspace / source).resolve()
        if not source.is_file():
            raise ValueError(f"Required staging source is missing: {source}")
        digest = sha256(source)
        if item.get("sha256") and digest != item["sha256"]:
            raise ValueError(f"Staging source identity changed: {source}")
        destination = (folder / item["destination"]).resolve()
        if not destination.is_relative_to(folder):
            raise ValueError("A staging destination leaves the staged folder.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        machine = pe_machine(destination)
        if item.get("machine") and machine != item["machine"]:
            raise ValueError(f"Wrong staged architecture for {item['destination']}: {machine}")
        files.append(
            {
                "path": item["destination"],
                "role": item["role"],
                "source": str(source),
                "bytes": destination.stat().st_size,
                "sha256": sha256(destination),
                "machine": machine,
                "signature_expectation": item.get("signature"),
            }
        )
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "attempt": recipe["attempt"],
        "staged_at_utc": datetime.now(timezone.utc).isoformat(),
        "folder_name": folder_name,
        "folder": str(folder),
        "zip": str(attempt / recipe["zip_name"]),
        "files": files,
        "directories": recipe.get("directories", []),
        "system_dependencies": recipe.get("system_dependencies", []),
        "excluded": recipe.get("excluded", []),
        "verification_contract": recipe.get("verification", {}),
        "application_launched": False,
        "package_installed": False,
    }
    manifest_path = folder / "Portwright-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    zip_path = Path(manifest["zip"])
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, str(Path(folder_name) / path.relative_to(folder)))
    manifest["zip_sha256"] = sha256(zip_path)
    _atomic_json(attempt / "staging-manifest.json", manifest)
    return manifest


def stage_run(workspace: Path, run_id: str, recipe_path: Path) -> tuple[dict, dict]:
    workspace = workspace.resolve(strict=True)
    path = run_path(workspace, run_id)
    before = path.read_bytes()
    existing_record = read_json(path)
    existing_manifest = existing_record.get("staging", {}).get("manifest")
    if isinstance(existing_manifest, str):
        existing_path = Path(existing_manifest)
        if not existing_path.is_absolute():
            existing_path = workspace / existing_path
        if existing_path.is_file():
            return existing_record, read_json(existing_path)
    recipe_location = recipe_path if recipe_path.is_absolute() else workspace / recipe_path
    recipe = read_json(recipe_location.resolve(strict=True))
    manifest = stage(workspace, run_id, recipe_location)
    manifest_path = Path(manifest["folder"]).parent / "staging-manifest.json"
    verification_value = recipe.get("verification_root")
    if verification_value:
        verification_root = Path(verification_value)
        if not verification_root.is_absolute():
            verification_root = workspace / verification_root
    else:
        verification_root = (
            workspace / "runs" / run_id / "verification" / str(recipe["attempt"])
        )
    try:
        recorded_manifest = str(manifest_path.relative_to(workspace))
    except ValueError:
        recorded_manifest = str(manifest_path)
    record = read_json(path)
    approval = next(
        (
            item
            for item in record.get("approvals", [])
            if item.get("id") == recipe.get("approval_id", "staging-and-verification")
        ),
        None,
    )
    if approval:
        approval.update(
            state="approved-staged",
            staging_started=True,
            staging_completed=True,
        )
    record["staging"] = {
        "manifest": recorded_manifest,
        "verification_root": str(verification_root.resolve()),
        "zip_sha256": manifest["zip_sha256"],
    }
    record.update(
        stage="staging",
        stop_after_stage="staging",
        active_role=None,
        status="staging-complete",
    )
    record.setdefault("capabilities", {})["stage"] = "implemented-recipe-driven-staging"
    record["next_action"] = "Run verify to create or import the fresh Examiner task."
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".portwright-stage-run-",
        suffix=".tmp",
        delete=False,
        newline="\n",
    ) as file:
        temporary = Path(file.name)
        json.dump(record, file, indent=2, ensure_ascii=True)
        file.write("\n")
    try:
        if path.read_bytes() != before:
            raise ValueError("The run changed concurrently; staging state was not recorded.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return record, manifest


def render_stage(record: dict, manifest: dict) -> str:
    return "\n".join(
        [
            f"PORTWRIGHT STAGE {record['run_id']}",
            f"Folder: {manifest['folder']}",
            f"ZIP: {manifest['zip']}",
            f"Files: {len(manifest['files'])}",
            f"SHA-256: {manifest['zip_sha256']}",
            f"Next: {record['next_action']}",
        ]
    ) + "\n"

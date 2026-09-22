"""Inspect declarations as data. Never evaluate a project or run target code."""

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import tomllib

NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
SKIP_DIRS = {".git", ".venv", "node_modules", "packages", "__pycache__", "runs"}
SUFFIXES = {".vcxproj", ".sln", ".props", ".targets", ".bat", ".cmd", ".ps1"}


def _git(root: Path, *args: str) -> str:
    executable = shutil.which("git")
    if not executable:
        raise ValueError("Git is required to inspect this checkout's identity.")
    executable_path = Path(executable).resolve()
    if executable_path.is_relative_to(root):
        raise ValueError("Refusing to execute a Git binary supplied by the target repository.")
    result = subprocess.run(
        [str(executable_path), "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
         "-C", str(root), *args],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode:
        raise ValueError(f"Git metadata inspection failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\r\n")


def identity(root: Path) -> dict:
    if not (root / ".git").exists():
        return {"path": str(root), "revision": None, "url": None, "working_tree": "unversioned",
                "working_tree_fingerprint": None}
    urls = _git(root, "remote", "-v").splitlines()
    origin = next((line.split()[1] for line in urls if line.startswith("origin\t")), None)
    if origin and "://" in origin:
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        origin = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    digest = hashlib.sha256()
    for name in sorted(filter(None, _git(root, "ls-files", "-z").split("\0"))):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Tracked source link leaves the read scope: {name}")
        digest.update(name.encode("utf-8") + b"\0")
        if path.is_file():
            with path.open("rb") as file:
                digest.update(hashlib.file_digest(file, "sha256").digest())
        else:
            digest.update(b"missing")
    return {
        "path": str(root), "revision": _git(root, "rev-parse", "HEAD"), "url": origin,
        "working_tree": "dirty" if _git(root, "status", "--porcelain=v1", "-uno") else "clean",
        "working_tree_fingerprint": digest.hexdigest(),
    }


def _sources(root: Path) -> dict[str, str]:
    sources = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs if name not in SKIP_DIRS
            and not (Path(directory) / name).is_symlink()
            and not getattr(Path(directory) / name, "is_junction", lambda: False)()
        )
        for name in sorted(files):
            path = Path(directory) / name
            workflow = ".github" in path.parts and "workflows" in path.parts
            if not (path.suffix.lower() in SUFFIXES
                    or name.lower() in {"packages.config", "package.json", "cargo.toml", "pyproject.toml"}
                    or (workflow and path.suffix.lower() in {".yml", ".yaml"})):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"Source link leaves the read scope: {path}")
            if path.stat().st_size > 2_000_000 or len(sources) >= 2000:
                raise ValueError("Declaration scan limit reached; narrow the repository scope.")
            data = path.read_bytes()
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            sources[str(path.relative_to(root))] = data.decode(encoding)
    return sources


def _xml(text: str) -> ET.Element:
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD/entity declarations are outside the supported manifest scope.")
    return ET.fromstring(text)


def _pair(condition: str | None) -> str:
    if not condition:
        return "*"
    match = re.fullmatch(
        r"""\s*['"]\$\(\s*Configuration\s*\)\|\$\(\s*Platform\s*\)['"]\s*==\s*['"]([^'"]+)['"]\s*""",
        condition, re.I,
    )
    return match.group(1) if match else "?"


def _line(text: str, needle: str, start: int = 0) -> int:
    offset = text.find(needle, start)
    if offset < 0:
        raise ValueError(f"Cannot locate source evidence for {needle!r}.")
    return text.count("\n", 0, offset) + 1


def _evidence(path: str, line: int, text: str) -> dict:
    return {"path": path, "line": line, "excerpt": text.splitlines()[line - 1].strip()}


def _item_line(text: str, source: str) -> int:
    pattern = r"""<(?:[\w.-]+:)?ClCompile\b[^>]*\bInclude\s*=\s*(['"])(.*?)\1"""
    for match in re.finditer(pattern, text):
        if html.unescape(match.group(2)) == source:
            return text.count("\n", 0, match.start()) + 1
    raise ValueError(f"Cannot locate the compile item for {source!r}.")


def _project(path: str, text: str, root: Path) -> tuple[dict, list[dict]]:
    tree = _xml(text)
    configurations = [e.attrib["Include"] for e in tree.findall(".//m:ProjectConfiguration", NS)]
    arm = [pair for pair in configurations if pair.split("|")[-1].upper() == "ARM64"]
    pch_modes = {}
    relative_libraries = {}
    for group in tree.findall("m:ItemDefinitionGroup", NS):
        pair = _pair(group.get("Condition"))
        if pair not in arm:
            continue
        pch_modes[pair] = group.findtext("m:ClCompile/m:PrecompiledHeader", namespaces=NS)
        for node in group.findall("m:Link/m:AdditionalDependencies", NS):
            for value in (node.text or "").split(";"):
                value = value.strip()
                if value.lower().endswith(".lib") and ("\\" in value or "/" in value) and "$(" not in value:
                    relative_libraries.setdefault(value, []).append(pair)
    items = []
    for item in tree.findall("m:ItemGroup/m:ClCompile", NS):
        modes = {_pair(e.get("Condition")): e.text for e in item.findall("m:PrecompiledHeader", NS)}
        if "Include" in item.attrib:
            items.append((item.attrib["Include"], modes))
    missing = []
    no_creator = []
    for pair in arm:
        if pch_modes.get(pair) != "Use":
            continue
        configuration = pair.split("|")[0]
        if not any(modes.get(pair, modes.get("*")) == "Create" for _, modes in items):
            no_creator.append(pair)
        for source, modes in items:
            if pair in modes or "*" in modes or "?" in modes:
                continue
            reference = {
                value for key, value in modes.items()
                if key in {f"{configuration}|x64", f"{configuration}|Win32"}
            }
            if len(reference) == 1 and next(iter(reference)) in {"Create", "NotUsing"}:
                missing.append({
                    "source": source, "configuration": pair, "value": next(iter(reference)),
                    "line": _item_line(text, source),
                })
    findings = []
    if missing or no_creator:
        ordered = sorted(missing, key=lambda item: (item["value"] != "Create", item["line"]))
        evidence_lines = list(dict.fromkeys(item["line"] for item in ordered))
        if not evidence_lines:
            evidence_lines = [_line(text, "<PrecompiledHeader>Use</PrecompiledHeader>")]
        findings.append({
            "code": "arm64-pch-metadata", "level": "source-backed",
            "title": "Arm64 precompiled-header metadata is incomplete",
            "why": "PCH consumers need a creator; architecture-specific exceptions must keep their intended mode.",
            "evidence": [_evidence(path, line, text) for line in evidence_lines],
            "configurations_without_creator": no_creator,
            "proposed_overrides": missing,
            "limits": "Simple equality conditions only; imported properties and compiler behavior are not evaluated.",
        })
    for library, pairs in relative_libraries.items():
        candidate = root / Path(path).parent / Path(library)
        if candidate.is_absolute() and candidate.resolve().is_relative_to(root) and not candidate.is_file():
            findings.append({
                "code": "missing-native-input", "level": "source-backed",
                "title": f"Unresolved native input: {library}",
                "why": "The project names this input, but it is absent from the inspected checkout.",
                "evidence": [_evidence(path, _line(text, library), text)],
                "configurations": pairs,
                "limits": "May be generated by another step; this is not an observed linker failure.",
            })
    return {
        "path": path, "configurations": configurations, "arm64": arm,
        "mfc": any(e.text in {"Dynamic", "Static"} for e in tree.findall(".//m:UseOfMfc", NS)),
    }, findings


def discover(repository: Path) -> dict:
    root = repository.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The target must be a directory.")
    metadata = identity(root)
    sources = _sources(root)
    projects, findings, packages, ci, solutions, ecosystems = [], [], [], [], [], []
    for path, text in sources.items():
        suffix = Path(path).suffix.lower()
        if suffix == ".vcxproj":
            if "MSBuild / C++" not in ecosystems:
                ecosystems.append("MSBuild / C++")
            project, issues = _project(path, text, root)
            projects.append(project)
            findings.extend(issues)
        elif suffix == ".sln":
            configurations = []
            for number, line in enumerate(text.splitlines(), 1):
                match = re.fullmatch(r"\s*([^\s|=]+)\|(\w+)\s*=\s*\1\|\2\s*", line)
                if match:
                    configurations.append({
                        "configuration": f"{match.group(1)}|{match.group(2)}",
                        "evidence": _evidence(path, number, text),
                    })
            solutions.append({"path": path, "configurations": configurations})
        elif Path(path).name.lower() == "packages.config":
            for package in _xml(text).findall("package"):
                packages.append({
                    "id": package.get("id"), "version": package.get("version"),
                    "evidence": _evidence(path, _line(text, f'id="{package.get("id")}"'), text),
                })
        elif Path(path).name.lower() == "package.json":
            package = json.loads(text)
            dependencies = {
                **(package.get("dependencies") or {}),
                **(package.get("devDependencies") or {}),
            }
            ecosystem = "Electron / Node.js" if "electron" in dependencies else "Node.js"
            if ecosystem not in ecosystems:
                ecosystems.append(ecosystem)
            packages.extend(
                {
                    "id": name,
                    "version": str(version),
                    "source": path,
                    "native_candidate": any(
                        token in name.lower() for token in ("native", "ffi", "sqlite", "sharp")
                    ),
                }
                for name, version in sorted(dependencies.items())
            )
        elif Path(path).name.lower() == "cargo.toml":
            manifest = tomllib.loads(text)
            if "Rust / Cargo" not in ecosystems:
                ecosystems.append("Rust / Cargo")
            dependencies = manifest.get("dependencies") or {}
            packages.extend(
                {
                    "id": name,
                    "version": str(value),
                    "source": path,
                    "native_candidate": False,
                }
                for name, value in sorted(dependencies.items())
            )
        elif Path(path).name.lower() == "pyproject.toml":
            if "Python" not in ecosystems:
                ecosystems.append("Python")
        elif suffix in {".bat", ".cmd", ".ps1"}:
            lines = []
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().lower().startswith(("#", "rem ", "::")):
                    continue
                if (re.search(r"\b(copy|xcopy|robocopy|copy-item)\b", line, re.I)
                        and re.search(r"(%windir%|%systemroot%|\$env:windir)[\\/](system32|syswow64)", line, re.I)
                        and ".dll" in line.lower()):
                    lines.append(number)
            if lines:
                findings.append({
                    "code": "host-runtime-staging", "level": "source-backed",
                    "title": "Staging copies runtime DLLs from the build host",
                    "why": "Host system directories do not establish a target-correct, redistributable payload.",
                    "evidence": [_evidence(path, number, text) for number in lines],
                    "explicit_arm64_text": bool(re.search(r"\barm64\b", text, re.I)),
                    "limits": "Script read as text; branches and any resulting package remain untested.",
                })
        elif ".github" in Path(path).parts and "workflows" in Path(path).parts:
            for number, line in enumerate(text.splitlines(), 1):
                match = re.search(r"(?:/p:|-p:)Platform\s*=\s*[\"']?([A-Za-z0-9_]+)", line, re.I)
                if match:
                    ci.append({"platform": match.group(1), "evidence": _evidence(path, number, text)})
    repository_text = str(root)
    if " " in repository_text:
        findings.append({
            "code": "checkout-path-spaces",
            "level": "environment-backed",
            "title": "The checkout path contains spaces",
            "why": "Some native package scripts and node-gyp wrappers still fail or hang on spaced paths.",
            "evidence": [{"path": ".", "line": 0, "excerpt": repository_text}],
            "limits": "This is a compatibility risk, not proof that the current build will fail.",
        })
    if any("onedrive" in part.lower() for part in root.parts):
        findings.append({
            "code": "cloud-synced-checkout",
            "level": "environment-backed",
            "title": "The checkout is inside a cloud-synced directory",
            "why": "Files On-Demand hydration and sync activity can block or delay native build tools.",
            "evidence": [{"path": ".", "line": 0, "excerpt": repository_text}],
            "limits": "Sync state is not inspected; a fully local checkout may work normally.",
        })
    arm = any(project["arm64"] for project in projects)
    codes = {finding["code"] for finding in findings}
    repair_codes = codes & {
        "arm64-pch-metadata",
        "missing-native-input",
        "host-runtime-staging",
    }
    if arm and "missing-native-input" in codes:
        route = "native-arm64-with-dependency-remediation"
    elif arm and repair_codes:
        route = "packaging-release-repair"
    elif projects and not arm:
        route = "native-arm64"
    else:
        route = None
    digest = hashlib.sha256()
    for path, text in sorted(sources.items()):
        digest.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    digest.update(json.dumps(findings, sort_keys=True).encode("utf-8"))
    stack = " + ".join(ecosystems) if ecosystems else "unclassified by the current declaration scanner"
    if any(project["mfc"] for project in projects):
        stack += " / MFC"
    return {
        "repository": metadata, "stack": stack, "projects": projects, "solutions": solutions,
        "windows_support": "declared VC++ projects" if projects else "unknown",
        "arm64_support": "declared; execution not implied" if arm else "not established",
        "findings": findings, "packages": packages, "ci_targets": ci,
        "candidate_route": route, "source_fingerprint": digest.hexdigest(),
        "inspected_files": list(sources),
        "limitations": [
            "Static declaration inventory with detailed VC++ rules and lightweight ecosystem detection.",
            "No MSBuild evaluation, package restore, target script, test, executable or package was run.",
            "Route selection, no-port, Arm64EC, native addon compatibility and runtime behavior require Investigator evidence.",
        ],
    }

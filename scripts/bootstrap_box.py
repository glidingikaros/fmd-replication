#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fmd.collection.tools.host.vmware import vmsd_snapshot_count, vmx_disk_descriptors
from fmd.core.hashing import sha256_file

DEFAULT_BOX_NAME = "fmd/windows-11-arm64"
DEFAULT_PROVIDER = "vmware_desktop"
DEPENDENCY_LOCK_ENV = "FMD_DEPENDENCY_LOCK"
PORTABLE_LOCK_SCHEMA = "generation_dependency_lock.v2"
VAGRANT_HOME_ENV = "VAGRANT_HOME"
DEFAULT_VAGRANT_HOME = Path("~/.vagrant.d")
RECIPE_MODULE = ROOT / "src/fmd/generation" / "recipe.py"
DEFAULT_REPORT_PATH = Path("~/.cache/fmd/box/bootstrap-report.json")
DESCRIPTOR_PREFIX_BYTES = 65536
DESCRIPTOR_MAGIC = "# Disk DescriptorFile"
FLAT_PARENT_CID = "ffffffff"
BOX_LIST_PATTERN = re.compile(
    r"^(?P<name>\S+)\s+\((?P<provider>[^,()]+),\s*(?P<version>[^,()]+?)(?:,\s*\((?P<arch>[^()]+)\))?\)\s*$"
)
EXTENT_PATTERN = re.compile(
    r'(?m)^\s*(?P<access>RW|RDONLY|NOACCESS)\s+(?P<sectors>\d+)\s+(?P<type>\S+)\s+"(?P<file>[^"]+)"(?:\s+(?P<offset>\d+))?'
)
PARENT_HINT_PATTERN = re.compile(r"(?im)^\s*parentFileNameHint\s*=")
PARENT_CID_PATTERN = re.compile(r'(?im)^\s*parentCID\s*=\s*"?([0-9a-f]+)"?\s*$')
CREATE_TYPE_PATTERN = re.compile(r'(?im)^\s*createType\s*=\s*"?([^"\s]+)"?\s*$')
SNAPSHOT_DISK_PATTERN = re.compile(r"-\d{6}\.vmdk$", re.IGNORECASE)
VAGRANT_TIMEOUT_SECONDS = 120
BOX_ADD_TIMEOUT_SECONDS = 3600

Runner = Callable[..., subprocess.CompletedProcess]
Hasher = Callable[[Path], str]


class BootstrapError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_recipe_module(path: Path = RECIPE_MODULE) -> ModuleType:
    spec = importlib.util.spec_from_file_location("fmd_generation_recipe", path)
    if spec is None or spec.loader is None:
        raise BootstrapError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_dependency_lock(
    explicit: Path | None,
    environ: Mapping[str, str] = os.environ,
) -> tuple[Path | None, str]:
    if explicit is not None:
        return explicit.expanduser(), "argument"
    value = environ.get(DEPENDENCY_LOCK_ENV)
    if value:
        return Path(value).expanduser(), "environment"
    return None, "unresolved"


def load_dependency_lock(path: Path, recipe: ModuleType) -> dict[str, Any]:
    if not path.is_file():
        raise BootstrapError(f"dependency lock is missing: {path}")
    return recipe.read_json(path)


def parse_box_list(text: str) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = BOX_LIST_PATTERN.match(line.strip())
        if match is None:
            continue
        boxes.append(
            {
                "name": match.group("name"),
                "provider": match.group("provider").strip(),
                "version": match.group("version").strip(),
                "architecture": (match.group("arch") or "").strip() or None,
            }
        )
    return boxes


def list_boxes(run: Runner, vagrant: str = "vagrant") -> list[dict[str, Any]]:
    try:
        completed = run(
            [vagrant, "box", "list"],
            capture_output=True,
            text=True,
            timeout=VAGRANT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BootstrapError(f"cannot run {vagrant} box list: {error}") from error
    if completed.returncode != 0:
        raise BootstrapError(f"{vagrant} box list failed: {(completed.stderr or completed.stdout or '').strip()[:500]}")
    return parse_box_list(completed.stdout or "")


def find_box(boxes: list[dict[str, Any]], name: str, provider: str) -> dict[str, Any] | None:
    matches = [box for box in boxes if box["name"] == name and box["provider"] == provider]
    if len(matches) > 1:
        raise BootstrapError(f"{name} ({provider}) is installed in {len(matches)} versions; pin one with --box-version")
    return matches[0] if matches else None


def box_directory(vagrant_home: Path, box: dict[str, Any]) -> Path | None:
    base = vagrant_home.expanduser() / "boxes" / box["name"].replace("/", "-VAGRANTSLASH-") / box["version"]
    candidates = []
    if box.get("architecture"):
        candidates.append(base / box["architecture"] / box["provider"])
    candidates.append(base / box["provider"])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def read_descriptor(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        prefix = handle.read(DESCRIPTOR_PREFIX_BYTES)
    text = prefix.decode("utf-8", errors="replace").replace("\x00", "")
    is_descriptor = DESCRIPTOR_MAGIC in text
    parent_cid = PARENT_CID_PATTERN.search(text)
    create_type = CREATE_TYPE_PATTERN.search(text)
    extents = [
        {
            "access": match.group("access"),
            "sectors": int(match.group("sectors")),
            "type": match.group("type"),
            "file": match.group("file"),
            "offset": int(match.group("offset")) if match.group("offset") else None,
        }
        for match in EXTENT_PATTERN.finditer(text)
    ] if is_descriptor else []
    return {
        "path": str(path),
        "is_descriptor": is_descriptor,
        "parent_file_name_hint": bool(PARENT_HINT_PATTERN.search(text)),
        "parent_cid": parent_cid.group(1).lower() if parent_cid else None,
        "create_type": create_type.group(1) if create_type else None,
        "extents": extents,
    }


def verify_flat_box(box_dir: Path, *, provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
    box_dir = box_dir.expanduser().resolve()
    problems: list[str] = []
    report: dict[str, Any] = {
        "box_dir": str(box_dir),
        "vmx_path": None,
        "metadata": None,
        "descriptors": [],
        "extent_paths": [],
        "snapshot_count": 0,
        "locks": [],
        "problems": problems,
    }
    metadata_path = box_dir / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except ValueError:
            metadata = None
            problems.append("metadata_json_unreadable")
        report["metadata"] = metadata
        if isinstance(metadata, dict) and metadata.get("provider") != provider:
            problems.append(f"metadata_provider_differs:{metadata.get('provider')}")
    else:
        problems.append("metadata_json_missing")
    vmx_files = sorted(box_dir.glob("*.vmx"))
    if len(vmx_files) != 1:
        problems.append(f"vmx_count:{len(vmx_files)}")
        return report
    vmx_path = vmx_files[0]
    report["vmx_path"] = str(vmx_path)
    locks = sorted(str(path) for path in box_dir.glob("*.lck"))
    report["locks"] = locks
    if locks:
        problems.append("lock_files_present")
    descriptors = vmx_disk_descriptors(vmx_path)
    if not descriptors:
        problems.append("vmx_has_no_vmdk")
    extent_paths: list[str] = []
    for descriptor_path in descriptors:
        if not descriptor_path.is_file():
            problems.append(f"descriptor_missing:{descriptor_path.name}")
            continue
        if SNAPSHOT_DISK_PATTERN.search(descriptor_path.name):
            problems.append(f"snapshot_disk_selected:{descriptor_path.name}")
        descriptor = read_descriptor(descriptor_path)
        report["descriptors"].append(descriptor)
        if not descriptor["is_descriptor"]:
            problems.append(f"not_a_descriptor:{descriptor_path.name}")
            continue
        if descriptor["parent_file_name_hint"]:
            problems.append(f"descriptor_has_parent_file_name_hint:{descriptor_path.name}")
        if descriptor["parent_cid"] is not None and descriptor["parent_cid"] != FLAT_PARENT_CID:
            problems.append(f"descriptor_parent_cid_not_flat:{descriptor_path.name}")
        if not descriptor["extents"]:
            problems.append(f"descriptor_has_no_extents:{descriptor_path.name}")
        for extent in descriptor["extents"]:
            extent_path = (descriptor_path.parent / extent["file"]).resolve()
            extent["path"] = str(extent_path)
            if not extent_path.is_file():
                extent["size_bytes"] = None
                problems.append(f"extent_missing:{extent['file']}")
                continue
            extent["size_bytes"] = extent_path.stat().st_size
            if extent_path != descriptor_path and read_descriptor(extent_path)["parent_file_name_hint"]:
                problems.append(f"extent_has_parent_file_name_hint:{extent['file']}")
            if str(extent_path) not in extent_paths:
                extent_paths.append(str(extent_path))
    report["extent_paths"] = extent_paths
    snapshot_count = vmsd_snapshot_count(box_dir)
    report["snapshot_count"] = snapshot_count
    if snapshot_count:
        problems.append(f"snapshots_present:{snapshot_count}")
    return report


def verify_against_lock(
    lock: dict[str, Any],
    *,
    box: dict[str, Any],
    flat: dict[str, Any],
    recipe: ModuleType,
    hasher: Hasher = sha256_file,
    max_hash_bytes: int | None = None,
) -> dict[str, Any]:
    problems: list[str] = []
    report: dict[str, Any] = {"problems": problems, "rows": [], "hashed_bytes": 0, "skipped_hash_bytes": 0}
    try:
        recipe.validate_dependency_lock(lock, verify_bytes=False)
    except ValueError as error:
        problems.append(f"lock_invalid:{error}")
        report["status"] = "mismatch"
        return report
    if lock.get("schema_version") == PORTABLE_LOCK_SCHEMA:
        location = lock["base"]["location"]
        base = {key: location[key] for key in ("box", "provider", "version")}
        box_dir = Path(flat["vmx_path"]).parent if flat.get("vmx_path") else None
        base_artifacts = ({str(box_dir / row["path"]): row for row in lock["base"]["files"]}
                          if box_dir is not None else {})
        if box_dir is not None and flat["vmx_path"] != str(box_dir / lock["base"]["entry"]):
            problems.append(f"lock_vmx_entry_differs:{lock['base']['entry']}")
    else:
        base = lock["base"]
        if flat.get("vmx_path") and str(Path(base["vmx_path"]).expanduser().resolve()) != flat["vmx_path"]:
            problems.append(f"lock_vmx_path_differs:{base['vmx_path']}")
        base_artifacts = {record["path"]: record for record in lock["artifacts"] if record["role"] == "base"}
    report["base"] = dict(base)
    if base["box"] != box["name"]:
        problems.append(f"lock_box_name_differs:{base['box']}")
    if base["provider"] != box["provider"]:
        problems.append(f"lock_provider_differs:{base['provider']}")
    if str(base["version"]) != str(box["version"]):
        problems.append(f"lock_version_differs:{base['version']}")
    required = [flat["vmx_path"]] if flat.get("vmx_path") else []
    required += [descriptor["path"] for descriptor in flat.get("descriptors", [])]
    required += list(flat.get("extent_paths", []))
    for path in required:
        if path not in base_artifacts:
            problems.append(f"unlocked_base_file:{path}")
    for path_text, record in sorted(base_artifacts.items()):
        path = Path(path_text)
        row: dict[str, Any] = {
            "path": path_text,
            "expected_size_bytes": record["size_bytes"],
            "expected_sha256": record["sha256"],
            "observed_size_bytes": None,
            "observed_sha256": None,
        }
        if not path.is_file():
            row["status"] = "missing"
            problems.append(f"base_file_missing:{path_text}")
        else:
            size = path.stat().st_size
            row["observed_size_bytes"] = size
            if size != record["size_bytes"]:
                row["status"] = "size_mismatch"
                problems.append(f"base_file_size_mismatch:{path_text}")
            elif max_hash_bytes is not None and size > max_hash_bytes:
                row["status"] = "size_verified_hash_skipped"
                report["skipped_hash_bytes"] += size
                problems.append(f"base_file_hash_not_verified:{path_text}")
            else:
                observed = hasher(path)
                row["observed_sha256"] = observed
                report["hashed_bytes"] += size
                if observed == record["sha256"]:
                    row["status"] = "verified"
                else:
                    row["status"] = "hash_mismatch"
                    problems.append(f"base_file_hash_mismatch:{path_text}")
        report["rows"].append(row)
    report["status"] = "verified" if not problems else "mismatch"
    report["hash_skipped_count"] = sum(1 for row in report["rows"] if row["status"] == "size_verified_hash_skipped")
    return report


def verify_installed_box(
    *,
    name: str,
    provider: str,
    vagrant_home: Path,
    run: Runner,
    vagrant: str = "vagrant",
    lock: dict[str, Any] | None = None,
    lock_path: Path | None = None,
    recipe: ModuleType | None = None,
    hasher: Hasher = sha256_file,
    max_hash_bytes: int | None = None,
    box_version: str | None = None,
) -> dict[str, Any]:
    problems: list[str] = []
    report: dict[str, Any] = {
        "schema_version": "fmd_box_bootstrap_report.v1",
        "checked_at": utc_now(),
        "box": {"name": name, "provider": provider},
        "vagrant_home": str(vagrant_home.expanduser()),
        "problems": problems,
    }
    boxes = list_boxes(run, vagrant)
    if box_version is not None:
        boxes = [box for box in boxes if box["version"] == box_version]
    box = find_box(boxes, name, provider)
    if box is None:
        problems.append("box_not_installed")
        report["status"] = "mismatch"
        return report
    report["box"] = box
    box_dir = box_directory(vagrant_home, box)
    if box_dir is None:
        problems.append("box_directory_missing")
        report["status"] = "mismatch"
        return report
    flat = verify_flat_box(box_dir, provider=provider)
    report["flat"] = flat
    problems.extend(f"flat:{problem}" for problem in flat["problems"])
    if lock is not None:
        recipe = recipe or load_recipe_module()
        lock_report = verify_against_lock(lock, box=box, flat=flat, recipe=recipe, hasher=hasher, max_hash_bytes=max_hash_bytes)
        lock_report["lock_path"] = str(lock_path) if lock_path else None
        lock_report["lock_sha256"] = sha256_file(lock_path) if lock_path else None
        report["lock"] = lock_report
        problems.extend(f"lock:{problem}" for problem in lock_report["problems"])
    else:
        report["lock"] = None
    report["status"] = "verified" if not problems else "mismatch"
    return report


def add_box(
    box_file: Path,
    *,
    name: str,
    provider: str,
    run: Runner,
    vagrant: str = "vagrant",
    expected_sha256: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    box_file = box_file.expanduser().resolve()
    if not box_file.is_file():
        raise BootstrapError(f"box archive is missing: {box_file}")
    observed = sha256_file(box_file)
    if expected_sha256 and observed != expected_sha256.lower():
        raise BootstrapError(f"box archive hashes to {observed}, not the expected {expected_sha256}")
    argv = [vagrant, "box", "add", "--name", name, "--provider", provider]
    if replace:
        argv.append("--force")
    argv.append(str(box_file))
    completed = run(argv, capture_output=True, text=True, timeout=BOX_ADD_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise BootstrapError(f"vagrant box add failed: {(completed.stderr or completed.stdout or '').strip()[:1000]}")
    return {"box_file": str(box_file), "sha256": observed, "argv": argv}


def format_report(report: dict[str, Any]) -> str:
    box = report["box"]
    described = f"{box['name']} ({box['provider']}"
    if box.get("version") is not None:
        described += f", {box['version']}"
    if box.get("architecture"):
        described += f", {box['architecture']}"
    lines = [f"box: {described})", f"status: {report['status']}"]
    flat = report.get("flat")
    if flat:
        lines.append(f"box directory: {flat['box_dir']}")
        lines.append(f"vmx: {flat['vmx_path']}")
        for descriptor in flat["descriptors"]:
            lines.append(
                f"  descriptor {Path(descriptor['path']).name}: createType={descriptor['create_type']} "
                f"parentCID={descriptor['parent_cid']} parentFileNameHint={descriptor['parent_file_name_hint']} "
                f"extents={len(descriptor['extents'])}"
            )
        lines.append(f"  snapshots: {flat['snapshot_count']}, locks: {len(flat['locks'])}")
    lock = report.get("lock")
    if lock:
        lines.append(f"dependency lock: {lock.get('lock_path')} (sha256 {lock.get('lock_sha256')})")
        for row in lock.get("rows", []):
            lines.append(f"  {row['status']:<28} {row['expected_size_bytes']:>12} bytes  {Path(row['path']).name}")
        lines.append(
            f"  hashed {lock.get('hashed_bytes', 0)} bytes; size-only {lock.get('skipped_hash_bytes', 0)} bytes "
            f"in {lock.get('hash_skipped_count', 0)} file(s)"
        )
    if report["problems"]:
        lines.append("problems:")
        lines.extend(f"  {problem}" for problem in report["problems"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="verify or add the flat Vagrant base box")
    parser.add_argument("--verify-only", action="store_true", help="inspect the installed box (default when no --box-file is given)")
    parser.add_argument("--box-file", type=Path, default=None, help="retained .box archive to add before verifying")
    parser.add_argument("--expected-box-sha256", default=None, help="sha256 the .box archive must hash to")
    parser.add_argument("--replace", action="store_true", help="pass --force to vagrant box add")
    parser.add_argument("--box-name", default=DEFAULT_BOX_NAME)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--box-version", default=None, help="pin the installed version when several are present")
    parser.add_argument("--vagrant", default="vagrant", help="vagrant executable")
    parser.add_argument("--vagrant-home", type=Path, default=None, help="default: VAGRANT_HOME or ~/.vagrant.d")
    parser.add_argument("--dependency-lock", type=Path, default=None, help="default: FMD_DEPENDENCY_LOCK")
    parser.add_argument("--no-lock", action="store_true", help="check flatness only")
    parser.add_argument("--max-hash-bytes", type=int, default=None, help="hash only base files up to this size; larger ones are size-checked")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    run: Runner = subprocess.run,
    environ: Mapping[str, str] = os.environ,
    hasher: Hasher = sha256_file,
    recipe: ModuleType | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        vagrant_home = args.vagrant_home or Path(environ.get(VAGRANT_HOME_ENV) or DEFAULT_VAGRANT_HOME)
        lock = None
        lock_path = None
        if not args.no_lock:
            lock_path, source = resolve_dependency_lock(args.dependency_lock, environ)
            if lock_path is None:
                raise BootstrapError(
                    "dependency lock not resolved: pass --dependency-lock, set FMD_DEPENDENCY_LOCK, "
                    "or pass --no-lock for a flatness-only check"
                )
            recipe = recipe or load_recipe_module()
            lock = load_dependency_lock(lock_path, recipe)
            print(f"dependency lock: {lock_path} [{source}]")
        added = None
        if args.box_file is not None:
            if args.verify_only:
                raise BootstrapError("--verify-only cannot add or replace a box")
            added = add_box(
                args.box_file,
                name=args.box_name,
                provider=args.provider,
                run=run,
                vagrant=args.vagrant,
                expected_sha256=args.expected_box_sha256,
                replace=args.replace,
            )
        report = verify_installed_box(
            name=args.box_name,
            provider=args.provider,
            vagrant_home=vagrant_home,
            run=run,
            vagrant=args.vagrant,
            lock=lock,
            lock_path=lock_path,
            recipe=recipe,
            hasher=hasher,
            max_hash_bytes=args.max_hash_bytes,
            box_version=args.box_version,
        )
        report["added"] = added
        print(format_report(report))
        if args.report is not None:
            report_path = args.report.expanduser()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"report: {report_path}")
        if args.json:
            print(json.dumps(report, indent=2))
        return 0 if report["status"] == "verified" else 1
    except (BootstrapError, OSError, ValueError) as error:
        print(f"bootstrap_box: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

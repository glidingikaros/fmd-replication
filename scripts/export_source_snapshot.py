#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import time
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "source_snapshot.v1"
MANIFEST_NAME = "snapshot-manifest.json"
SNAPSHOT_ROOTS = ("src", "scripts", "tests", "LICENSES", "tools")
SNAPSHOT_FILES = ("pyproject.toml", "uv.lock", "LICENSE", "THIRD_PARTY_NOTICES.md",
                  "MANIFEST.in")
EXCLUDED_PARTS = frozenset(
    {
        ".fmd",
        ".attic",
        ".venv",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".vagrant",
        "outputs",
        "node_modules",
        "private",
        "private-recipe",
        "private_recipe",
        "secrets",
    }
)
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
EXCLUDED_NAMES = frozenset({".ds_store", "ground_truth.json", "ground-truth.json", ".env",
                            "private-generation.json", "private_generation.json",
                            "recipe-runtime-receipt.json", "credentials.json"})
DEFAULT_OUTPUT_PARENT = Path(".fmd") / "source-snapshots"
CHUNK_SIZE = 1024 * 1024
GIT_TIMEOUT_SECONDS = 120


class SnapshotError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def sha256_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return sha256_stream(handle)


def excluded_reason(relative: str) -> str | None:
    parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
    if any(part in EXCLUDED_PARTS or part.endswith((".egg-info", ".dist-info")) for part in parts):
        return "excluded_directory"
    name = parts[-1] if parts else ""
    if name in EXCLUDED_NAMES or name.startswith(".env."):
        return "excluded_name"
    if "generation" in parts and "receipt" in name and name.endswith(".json"):
        return "private_generation_receipt"
    if PurePosixPath(name).suffix in EXCLUDED_SUFFIXES:
        return "excluded_suffix"
    return None


def within_roots(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in SNAPSHOT_FILES
    return parts[0] in SNAPSHOT_ROOTS


def git_output(root: Path, git: str, arguments: list[str]) -> bytes | None:
    try:
        completed = subprocess.run(
            [git, "-C", str(root), *arguments],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def git_identity(root: Path, git: str) -> dict[str, Any]:
    head = git_output(root, git, ["rev-parse", "HEAD"])
    status = git_output(root, git, ["status", "--porcelain"])
    return {
        "head": head.decode("utf-8").strip() if head else None,
        "status_clean": (status.strip() == b"") if status is not None else None,
    }


def git_listing(root: Path, git: str) -> list[str] | None:
    pathspec = ["--", *SNAPSHOT_ROOTS, *SNAPSHOT_FILES]
    tracked = git_output(root, git, ["ls-files", "-z", *pathspec])
    if tracked is None:
        return None
    others = git_output(root, git, ["ls-files", "-z", "--others", "--exclude-standard", *pathspec])
    if others is None:
        return None
    names = {entry.decode("utf-8") for entry in tracked.split(b"\0") if entry}
    names |= {entry.decode("utf-8") for entry in others.split(b"\0") if entry}
    return sorted(names)


def walk_listing(root: Path) -> list[str]:
    found: set[str] = set()
    for name in SNAPSHOT_FILES:
        if (root / name).is_file():
            found.add(name)
    for top in SNAPSHOT_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        deadline = time.monotonic() + 30
        for directory, directories, names in os.walk(base, followlinks=False):
            directory = Path(directory)
            if len(directory.relative_to(base).parts) > 20 or time.monotonic() > deadline:
                raise SnapshotError("source inventory exceeded its depth/time budget")
            directories[:] = [name for name in directories
                              if not (directory / name).is_symlink()
                              and excluded_reason((directory / name).relative_to(root).as_posix()) is None]
            for name in names:
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if excluded_reason(relative) or path.is_symlink() or not path.is_file():
                    continue
                found.add(relative)
                if len(found) > 20000:
                    raise SnapshotError("source inventory exceeded its file budget")
    return sorted(found)


def select_files(root: Path, listing: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    files: list[str] = []
    skipped: list[dict[str, str]] = []
    for relative in sorted(set(listing)):
        member = PurePosixPath(relative)
        if member.is_absolute() or member.as_posix() != relative or ".." in member.parts or "\\" in relative:
            raise SnapshotError("source listing contains a noncanonical path")
        if not within_roots(relative):
            skipped.append({"path": relative, "reason": "outside_roots"})
            continue
        reason = excluded_reason(relative)
        if reason:
            skipped.append({"path": relative, "reason": reason})
            continue
        path = root / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            skipped.append({"path": relative, "reason": "symlink"})
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            raise SnapshotError("source file resolves outside the source root")
        if not path.exists():
            skipped.append({"path": relative, "reason": "missing_in_worktree"})
            continue
        if not path.is_file():
            skipped.append({"path": relative, "reason": "not_regular_file"})
            continue
        files.append(relative)
    return files, skipped


def write_tarball(root: Path, files: list[str], tar_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with tar_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for relative in sorted(files):
                    path = root / relative
                    data = path.read_bytes()
                    executable = bool(path.stat().st_mode & 0o111)
                    info = tarfile.TarInfo(name=relative)
                    info.type = tarfile.REGTYPE
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o755 if executable else 0o644
                    tar.addfile(info, io.BytesIO(data))
                    entries.append(
                        {
                            "path": relative,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size_bytes": len(data),
                            "mode": "0755" if executable else "0644",
                        }
                    )
    return entries


def create_snapshot(
    root: Path,
    output_dir: Path,
    *,
    name: str | None = None,
    use_git: bool = True,
    git: str = "git",
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise SnapshotError(f"root is not a directory: {root}")
    moment = now or utc_now()
    identity = git_identity(root, git) if use_git else {"head": None, "status_clean": None}
    listing = git_listing(root, git) if use_git else None
    listing_source = "git_ls_files"
    if listing is None:
        listing = walk_listing(root)
        listing_source = "filesystem_walk"
    files, skipped = select_files(root, listing)
    if not files:
        raise SnapshotError(f"no files to snapshot below {root}")
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    tar_name = name or f"source-snapshot-{(identity['head'] or 'nogit')[:12]}-{stamp}.tar.gz"
    if Path(tar_name).name != tar_name or "\\" in tar_name or tar_name in {".", ".."}:
        raise SnapshotError("snapshot name must be a single filename")
    tar_path = output_dir / tar_name
    manifest_path = output_dir / MANIFEST_NAME
    if tar_path.exists() or manifest_path.exists():
        raise SnapshotError(f"refusing to overwrite an existing snapshot in {output_dir}")
    entries = write_tarball(root, files, tar_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": moment.isoformat().replace("+00:00", "Z"),
        "root": str(root),
        "git_head": identity["head"],
        "git_status_clean": identity["status_clean"],
        "listing_source": listing_source,
        "roots": list(SNAPSHOT_ROOTS),
        "root_files": list(SNAPSHOT_FILES),
        "exclusions": {
            "directories": sorted(EXCLUDED_PARTS),
            "suffixes": sorted(EXCLUDED_SUFFIXES),
            "names": sorted(EXCLUDED_NAMES),
        },
        "file_count": len(entries),
        "total_bytes": sum(entry["size_bytes"] for entry in entries),
        "files": entries,
        "skipped": skipped,
        "tarball": {
            "name": tar_name,
            "sha256": sha256_file(tar_path),
            "size_bytes": tar_path.stat().st_size,
            "format": "tar.gz; GNU tar, entries sorted by path, regular files only, mtime 0, uid/gid 0, gzip mtime 0",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "created",
        "tarball": str(tar_path),
        "manifest": str(manifest_path),
        "tarball_sha256": manifest["tarball"]["sha256"],
        "file_count": len(entries),
        "total_bytes": manifest["total_bytes"],
        "skipped_count": len(skipped),
        "git_head": identity["head"],
        "listing_source": listing_source,
        "files": {entry["path"]: entry["sha256"] for entry in entries},
    }


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SnapshotError(f"manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"unsupported manifest schema in {path}")
    if not isinstance(manifest.get("files"), list):
        raise SnapshotError(f"manifest has no file list: {path}")
    return manifest


def manifest_files(manifest: dict[str, Any]) -> dict[str, str]:
    files = {}
    for row in manifest["files"]:
        name, digest = row["path"], row["sha256"]
        safe_member_path(name)
        if name in files or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SnapshotError("snapshot manifest contains duplicate members or invalid hashes")
        files[name] = digest
    return files


def safe_member_path(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if not name or name.startswith("/") or "\\" in name or member.is_absolute() or member.as_posix() != name:
        raise SnapshotError(f"refusing member with an absolute or malformed name: {name!r}")
    if any(part in {"", ".", ".."} for part in member.parts):
        raise SnapshotError(f"refusing member that escapes the snapshot: {name!r}")
    if not within_roots(name):
        raise SnapshotError(f"refusing member outside the snapshot roots: {name!r}")
    if excluded_reason(name):
        raise SnapshotError("snapshot member violates the public source policy")
    return member


def verify_snapshot(tar_path: Path, manifest_path: Path) -> dict[str, Any]:
    tar_path = tar_path.expanduser()
    if not tar_path.is_file():
        raise SnapshotError(f"snapshot is missing: {tar_path}")
    manifest = load_manifest(manifest_path)
    problems: list[str] = []
    observed = sha256_file(tar_path)
    if observed != manifest["tarball"]["sha256"]:
        problems.append("tarball_sha256_mismatch")
    expected = manifest_files(manifest)
    seen: list[str] = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            safe_member_path(member.name)
            if not member.isreg():
                problems.append(f"non_regular_member:{member.name}")
                continue
            if member.mtime != 0 or member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                problems.append(f"nondeterministic_header:{member.name}")
            seen.append(member.name)
            digest = expected.get(member.name)
            if digest is None:
                problems.append(f"undeclared_member:{member.name}")
                continue
            handle = tar.extractfile(member)
            if handle is None:
                problems.append(f"unreadable_member:{member.name}")
                continue
            with handle:
                if sha256_stream(handle) != digest:
                    problems.append(f"member_sha256_mismatch:{member.name}")
    if seen != sorted(seen):
        problems.append("members_not_sorted")
    if len(seen) != len(set(seen)):
        problems.append("duplicate_members")
    for missing in sorted(set(expected) - set(seen)):
        problems.append(f"member_missing:{missing}")
    return {
        "status": "verified" if not problems else "mismatch",
        "tarball": str(tar_path),
        "manifest": str(manifest_path),
        "tarball_sha256": observed,
        "expected_tarball_sha256": manifest["tarball"]["sha256"],
        "member_count": len(seen),
        "manifest_file_count": len(expected),
        "problems": problems,
        "files": expected,
    }


def verify_declaration(files: dict[str, str], declaration_path: Path) -> dict[str, Any]:
    declaration_path = declaration_path.expanduser()
    if not declaration_path.is_file():
        raise SnapshotError(f"declaration is missing: {declaration_path}")
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    block = declaration.get("source_sha256")
    if not isinstance(block, dict) or not block:
        raise SnapshotError(f"declaration has no source_sha256 block: {declaration_path}")
    matched: list[str] = []
    missing: list[str] = []
    mismatched: list[dict[str, str]] = []
    for path, digest in sorted(block.items()):
        if not isinstance(digest, str) or len(digest) != 64:
            raise SnapshotError(f"declaration hash for {path} is not a sha256")
        observed = files.get(path)
        if observed is None:
            missing.append(path)
        elif observed != digest.lower():
            mismatched.append({"path": path, "declared_sha256": digest, "snapshot_sha256": observed})
        else:
            matched.append(path)
    undeclared = sorted(set(files) - set(block))
    return {
        "status": "verified" if not missing and not mismatched else "mismatch",
        "declaration": str(declaration_path),
        "declaration_schema_version": declaration.get("schema_version"),
        "declared_file_count": len(block),
        "matched_count": len(matched),
        "missing_in_snapshot": missing,
        "mismatched": mismatched,
        "undeclared_in_snapshot_count": len(undeclared),
        "undeclared_in_snapshot_sample": undeclared[:20],
    }


def extract_snapshot(tar_path: Path, manifest: dict[str, Any], destination: Path) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SnapshotError(f"extraction target must be an absent or empty directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    expected = manifest_files(manifest)
    extracted: list[str] = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            if not member.isreg():
                raise SnapshotError(f"refusing to extract a non-regular member: {member.name!r}")
            relative = safe_member_path(member.name)
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination):
                raise SnapshotError(f"member resolves outside the destination: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            if handle is None:
                raise SnapshotError(f"unreadable member: {member.name!r}")
            with handle, target.open("wb") as out:
                shutil.copyfileobj(handle, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            extracted.append(member.name)
    problems: list[str] = []
    for relative, digest in sorted(expected.items()):
        path = destination.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file():
            problems.append(f"extracted_file_missing:{relative}")
        elif sha256_file(path) != digest:
            problems.append(f"extracted_sha256_mismatch:{relative}")
    for relative in extracted:
        if relative not in expected:
            problems.append(f"extracted_undeclared:{relative}")
    return {
        "status": "verified" if not problems else "mismatch",
        "destination": str(destination),
        "extracted_count": len(extracted),
        "problems": problems,
    }


def print_declaration_summary(declared: dict[str, Any]) -> None:
    print(
        f"declaration {declared['declaration']}: {declared['status']} "
        f"({declared['matched_count']}/{declared['declared_file_count']} matched, "
        f"{len(declared['missing_in_snapshot'])} missing, {len(declared['mismatched'])} mismatched, "
        f"{declared['undeclared_in_snapshot_count']} snapshot files not declared)"
    )


def print_section_problems(section: dict[str, Any]) -> None:
    for problem in section.get("problems", []):
        print(f"  problem: {problem}")
    for path in section.get("missing_in_snapshot", []):
        print(f"  missing in snapshot: {path}")
    for row in section.get("mismatched", []):
        print(f"  mismatched: {row['path']} declared {row['declared_sha256']} snapshot {row['snapshot_sha256']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="export, verify or extract a hashed source snapshot")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--output-dir", type=Path, default=None, help="default: <root>/.fmd/source-snapshots/<UTC stamp>")
    parser.add_argument("--name", default=None, help="tarball file name (default: source-snapshot-<head>-<stamp>.tar.gz)")
    parser.add_argument("--no-git", action="store_true", help="list files by walking the roots instead of git ls-files")
    parser.add_argument("--verify", type=Path, default=None, metavar="SNAPSHOT", help="verify this tarball against its manifest")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest for --verify (default: snapshot-manifest.json beside the tarball)")
    parser.add_argument("--verify-declaration", type=Path, default=None, metavar="DECLARATION", help="compare the declaration's source_sha256 block")
    parser.add_argument("--extract", type=Path, default=None, metavar="DIR", help="with --verify: unpack into a fresh directory and re-verify")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.extract is not None and args.verify is None:
        parser.error("--extract requires --verify SNAPSHOT")
    if args.manifest is not None and args.verify is None:
        parser.error("--manifest requires --verify SNAPSHOT")
    report: dict[str, Any] = {}
    try:
        if args.verify is not None:
            manifest_path = (args.manifest or args.verify.expanduser().parent / MANIFEST_NAME).expanduser()
            verification = verify_snapshot(args.verify, manifest_path)
            if verification["status"] != "verified":
                raise SnapshotError("snapshot verification failed; extraction was not attempted")
            files = verification.pop("files")
            report["snapshot"] = verification
            print(f"snapshot: {verification['tarball']}")
            print(f"manifest: {verification['manifest']}")
            print(f"tarball sha256: {verification['tarball_sha256']}")
            print(f"members: {verification['member_count']} (manifest lists {verification['manifest_file_count']})")
            print(f"snapshot verification: {verification['status']}")
            statuses = [verification["status"]]
            if args.verify_declaration is not None:
                declared = verify_declaration(files, args.verify_declaration)
                if declared["status"] != "verified":
                    raise SnapshotError("declaration verification failed; extraction was not attempted")
                report["declaration"] = declared
                statuses.append(declared["status"])
                print_declaration_summary(declared)
            if args.extract is not None:
                extracted = extract_snapshot(args.verify.expanduser(), load_manifest(manifest_path), args.extract)
                report["extraction"] = extracted
                statuses.append(extracted["status"])
                print(f"extracted {extracted['extracted_count']} files into {extracted['destination']}: {extracted['status']}")
            for section in report.values():
                print_section_problems(section)
            if args.json:
                print(json.dumps(report, indent=2))
            return 0 if all(status == "verified" for status in statuses) else 1
        output_dir = args.output_dir or (args.root.expanduser().resolve() / DEFAULT_OUTPUT_PARENT / utc_now().strftime("%Y%m%dT%H%M%SZ"))
        created = create_snapshot(args.root, output_dir, name=args.name, use_git=not args.no_git)
        files = created.pop("files")
        report["snapshot"] = created
        print(f"snapshot: {created['tarball']}")
        print(f"manifest: {created['manifest']}")
        print(f"tarball sha256: {created['tarball_sha256']}")
        print(f"files: {created['file_count']} ({created['total_bytes']} bytes), skipped {created['skipped_count']}, listing {created['listing_source']}, git HEAD {created['git_head']}")
        status = "verified"
        if args.verify_declaration is not None:
            declared = verify_declaration(files, args.verify_declaration)
            report["declaration"] = declared
            status = declared["status"]
            print_declaration_summary(declared)
            print_section_problems(declared)
        if args.json:
            print(json.dumps(report, indent=2))
        return 0 if status == "verified" else 1
    except (SnapshotError, OSError, ValueError, tarfile.TarError) as error:
        print(f"export_source_snapshot: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

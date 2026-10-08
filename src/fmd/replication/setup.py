"""One-time preparation of a host: the pinned .NET runtime, Ansible, the collection toolchain and,
on QEMU hosts, the Windows base image (installed from Microsoft's ISO the way the paper built its box)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from fmd.replication import host

AGENT = {"User-Agent": "Mozilla/5.0"}  # download.ericzimmermanstools.com refuses urllib's default agent


def log(message: str) -> None:
    print(f"[fmd replicate] {message}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    log(" ".join(str(part) for part in command))
    return subprocess.run([str(part) for part in command], check=True, env=host.environment(), **kwargs)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=300) as response:
        return response.read()


def dotnet_runtime() -> None:
    """The exact runtime the toolchain lock verifies, side by side with any other .NET, under the cache."""
    version = host.PINS["dotnet_runtime"]["version"]
    dotnet = host.which("dotnet")
    listed = subprocess.run([dotnet, "--list-runtimes"], capture_output=True, text=True, check=False,
                            env=host.environment()).stdout if dotnet else ""
    if f"Microsoft.NETCore.App {version} " in listed:
        return
    target = host.dotnet_home()
    with tempfile.TemporaryDirectory() as temporary:
        if host.WINDOWS:
            script = Path(temporary) / "dotnet-install.ps1"
            script.write_bytes(fetch("https://dot.net/v1/dotnet-install.ps1"))
            run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 "-Runtime", "dotnet", "-Version", version, "-InstallDir", target, "-NoPath"])
        else:
            script = Path(temporary) / "dotnet-install.sh"
            script.write_bytes(fetch("https://dot.net/v1/dotnet-install.sh"))
            run(["bash", script, "--runtime", "dotnet", "--version", version, "--install-dir", target, "--no-path"])


def ansible() -> None:
    pins = host.PINS["ansible"]
    if host.MACOS:
        return  # the paper's VMware path uses the host's Ansible (brew install ansible)
    if host.WINDOWS:
        distro, venv = host.PINS["wsl_distribution"], "/mnt/c/fmd-ansible"
        run(["wsl.exe", "-d", distro, "--exec", "bash", "-c",
             f"test -x {venv}/bin/ansible || python3 -m venv {venv} && "
             f"{venv}/bin/pip install -q ansible-core=={pins['ansible-core']} pywinrm=={pins['pywinrm']} && "
             f"{venv}/bin/ansible-galaxy collection install ansible.windows:=={pins['ansible.windows']} -p {venv}/collections"])
        run(["uv", "tool", "install", "--force", host.REPO / "ci" / "wsl-ansible"])
        return
    run(["uv", "tool", "install", "--force", f"ansible-core=={pins['ansible-core']}", "--with", f"pywinrm=={pins['pywinrm']}"])
    run([host.which("ansible-galaxy"), "collection", "install", f"ansible.windows:=={pins['ansible.windows']}"])


def toolchain() -> Path:
    """The EZ tools bytes eztools-lock.json pins, the dfir_ntfs environment and the official
    PECmd/SBECmd downloads, all at the locations fmd uses by default."""
    root = host.toolchain_root()
    pins = host.PINS["eztools"]
    if not root.exists():
        try:
            data = fetch(pins["url"])
        except OSError:  # a private release needs the GitHub CLI's credentials
            release = pins["release"]
            with tempfile.TemporaryDirectory() as temporary:
                run(["gh", "release", "download", release["tag"], "--repo", release["repository"],
                     "--pattern", release["asset"], "--dir", temporary])
                data = (Path(temporary) / release["asset"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != pins["sha256"]:
            raise SystemExit("the EZ tools archive does not match its pinned sha256")
        root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root.parent) as temporary, tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(temporary, filter="data")
            (Path(temporary) / "net9").rename(root)
    run([sys.executable, host.REPO / "scripts" / "bootstrap_eztools.py", "--verify-only", "--root", root])
    dfir = [sys.executable, host.REPO / "scripts" / "bootstrap_dfir_ntfs.py"]
    if not Path(host.environment()["FMD_DFIR_NTFS_ENV"]).exists():
        run(dfir)
    elif subprocess.run([str(part) for part in [*dfir, "--verify-only"]], env=host.environment(), check=False).returncode:
        run([*dfir, "--recreate"])  # fmd's own cache: rebuild an environment that no longer verifies
    parsers = windows_parsers()
    for name, url in host.PINS["windows_parsers"].items():
        if not (parsers / name).is_file():
            zipfile.ZipFile(io.BytesIO(fetch(url))).extract(name, parsers)
    return root


def windows_parsers() -> Path:
    return host.cache() / "windows-parsers"


def base() -> dict:
    """Install the Windows base from Microsoft's ISO (ci/windows/install.py, as CI builds it) and
    place it where the QEMU provider looks for it. About 40 minutes, once per host."""
    from fmd.generation.recipe import qemu_box

    facts = host.base_guest_facts()
    if facts is not None:
        return facts
    work = host.cache() / "base-build"
    pins = host.PINS["base_builder"]
    run(["uv", "run", "--no-project", "--with", f"pycdlib=={pins['pycdlib']}", "--with", f"pywinrm=={pins['pywinrm']}",
         "--with", f"psutil=={pins['psutil']}", "--with", "tzdata", "python", host.REPO / "ci" / "windows" / "install.py",
         "--iso-url", host.PINS["windows_iso"]["url"], "--work", work])
    box = host.base_home() / qemu_box().replace("/", "-VAGRANTSLASH-") / "0"
    target = box / "amd64" / "qemu"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("base.qcow2", "base-vars.fd"):
        shutil.move(work / name, target / name)
    shutil.move(work / "guest.json", box / "guest.json")
    log(f"base placed in {target}")
    return json.loads((box / "guest.json").read_text(encoding="utf-8"))


def all_steps(*, build_base: bool = True) -> None:
    dotnet_runtime()
    ansible()
    toolchain()
    if build_base and host.provider() == "qemu":
        log(f"base guest: {base()}")

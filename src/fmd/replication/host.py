"""Where a replication runs: the paper's VMware path on macOS, QEMU elsewhere (KVM on Linux,
WHPX on Windows), with every cache under ~/.cache/fmd unless FMD_CACHE says otherwise."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PINS = json.loads((Path(__file__).with_name("pins.json")).read_text(encoding="utf-8"))
QEMU_WINDOWS_DIR = Path(r"C:\Program Files\qemu")
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"


def cache() -> Path:
    return Path(os.environ.get("FMD_CACHE") or Path.home() / ".cache" / "fmd").expanduser()


def provider() -> str:
    return "vmware_desktop" if MACOS else "qemu"


def base_home() -> Path:
    return Path(os.environ.get("FMD_QEMU_BASE_HOME") or cache() / "qemu-bases").expanduser()


def base_guest_facts() -> dict | None:
    """What the base install observed in the guest (build, time zone, locale, licence)."""
    from fmd.generation.recipe import qemu_box

    path = base_home() / qemu_box().replace("/", "-VAGRANTSLASH-") / "0" / "guest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def dotnet_home() -> Path:
    return cache() / "dotnet"


def uv_tool_bin() -> Path | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    completed = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, check=False)
    return Path(completed.stdout.strip()) if completed.returncode == 0 and completed.stdout.strip() else None


def environment() -> dict[str, str]:
    """The child-process environment: fmd's private .NET runtime, uv's tool directory (Ansible, or
    the WSL launchers on Windows) and QEMU's folder on Windows ahead of PATH. No shell setup needed."""
    env = dict(os.environ)
    front = [str(path) for path in (dotnet_home(), uv_tool_bin(), QEMU_WINDOWS_DIR if WINDOWS else None)
             if path and Path(path).is_dir()]
    env["PATH"] = os.pathsep.join(front + [env.get("PATH", "")])
    if dotnet_home().is_dir():
        env["DOTNET_ROOT"] = str(dotnet_home())
    env.setdefault("FMD_QEMU_BASE_HOME", str(base_home()))
    env.setdefault("FMD_HOST_TOOLCHAIN_ROOT", str(cache() / "eztools" / "net9"))
    env.setdefault("FMD_DFIR_NTFS_ENV", str(cache() / "dfir-ntfs" / "venv"))
    env.setdefault("PYTHONUTF8", "1")
    return env


def toolchain_root() -> Path:
    return Path(environment()["FMD_HOST_TOOLCHAIN_ROOT"])


def which(name: str) -> str | None:
    return shutil.which(name, path=environment()["PATH"])


def free_gib(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 2**30


def accelerates(qemu: str, accelerator: str) -> bool:
    """QEMU keeps a paused, diskless machine running only if the accelerator initialised (no admin needed)."""
    process = subprocess.Popen([qemu, "-machine", f"q35,accel={accelerator}", "-cpu", "max", "-m", "64", "-S",
                                "-display", "none", "-nodefaults"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=8)
        return False
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return True


def check(rows: list, name: str, found, fix: str) -> None:
    rows.append((name, bool(found), str(found) if found and found is not True else ("found" if found else fix)))


def checks() -> list[tuple[str, bool, str]]:
    """(what, ok, what was found or how to fix it) for everything a replication needs on this host."""
    rows: list[tuple[str, bool, str]] = []
    system = platform.system()
    # one image in flight: the guest's writes (up to ~8 GiB), the exported image (~25 GiB), collection (~3 GiB)
    rows.append(("free disk at the cache (40 GiB per image in flight)", free_gib(cache()) >= 40,
                 f"{free_gib(cache()):.0f} GiB free at {cache()}"))
    runtimes = subprocess.run([which("dotnet"), "--list-runtimes"], capture_output=True, text=True,
                              check=False).stdout if which("dotnet") else ""
    wanted = f"Microsoft.NETCore.App {PINS['dotnet_runtime']['version']}"
    check(rows, f".NET runtime {PINS['dotnet_runtime']['version']}", wanted in runtimes,
          "run: fmd replicate setup (installs it under the cache)")
    if MACOS:
        vmrun = Path("/Applications/VMware Fusion.app/Contents/Public/vmrun")
        check(rows, "VMware Fusion (vmrun)", vmrun.exists() and vmrun, "install VMware Fusion 13")
        check(rows, "Vagrant", which("vagrant"), "install Vagrant and the vagrant-vmware-desktop plugin")
        check(rows, "Ansible", which("ansible-playbook"), "brew install ansible")
        boxes = Path(os.environ.get("VAGRANT_HOME") or Path.home() / ".vagrant.d").expanduser() / "boxes"
        box = boxes / "fmd-VAGRANTSLASH-windows-11-arm64"
        check(rows, "the paper's Windows box (fmd/windows-11-arm64)", box.is_dir() and box,
              "build it: tools/base-image/windows11-arm64/build-vmware-box.sh (or set VAGRANT_HOME to where it is)")
        return rows
    qemu = which("qemu-system-x86_64")
    check(rows, "QEMU", qemu and which("qemu-img") and which("qemu-io") and qemu,
          "sudo apt-get install qemu-system-x86 qemu-utils ovmf" if system == "Linux"
          else "winget install SoftwareFreedomConservancy.QEMU")
    if system == "Linux":
        check(rows, "KVM (/dev/kvm read-write)", os.access("/dev/kvm", os.R_OK | os.W_OK),
              "sudo usermod -aG kvm $USER, then log in again")
    else:
        check(rows, "Windows Hypervisor Platform (QEMU starts with WHPX)", qemu and accelerates(qemu, "whpx"),
              "admin PowerShell: Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform; reboot")
        listed = subprocess.run(["wsl.exe", "--list", "--quiet"], capture_output=True, check=False).stdout
        distro = PINS["wsl_distribution"]
        check(rows, f"WSL distribution {distro}", distro in listed.decode("utf-16-le", "replace"),
              f"wsl --install -d {distro} --no-launch; wsl --set-version {distro} 1")
    check(rows, "Ansible with WinRM", which("ansible-playbook"), "run: fmd replicate setup")
    facts = base_guest_facts()
    check(rows, "Windows base image", facts and f"build {facts['build']}", "run: fmd replicate setup (about 40 minutes)")
    return rows

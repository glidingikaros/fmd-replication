from __future__ import annotations

import os
import platform
import plistlib
import shutil
from pathlib import Path
from typing import Protocol

VMRUN_CANDIDATES = (
    Path("/Applications/VMware Fusion.app/Contents/Library/vmrun"),
    Path("/Applications/VMware Fusion Tech Preview.app/Contents/Library/vmrun"),
)


class VmBackend(Protocol):
    name: str
    boot_phase: str
    base_store: str
    base_entry: str
    TOOLS: tuple[str, ...]

    @staticmethod
    def discover_tools() -> dict[str, str | None]:
        pass

    @staticmethod
    def hypervisor_identity(tools: dict[str, str | None]) -> dict:
        pass

    @staticmethod
    def base_location(box: str, version: str) -> dict:
        pass

    @staticmethod
    def base_directory(location: dict) -> Path:
        pass

    @staticmethod
    def base_files(directory: Path) -> list[Path]:
        pass

    @staticmethod
    def check_base(directory: Path, entry: str, rows: list[dict]) -> None:
        pass

    @staticmethod
    def host_facts(tools: dict[str, str | None]) -> dict:
        pass

    @staticmethod
    def supported_host() -> str | None:
        pass

    @staticmethod
    def host_checks(tools: dict[str, str | None]) -> list[tuple[str, bool, object]]:
        pass

    def check_host(self) -> None:
        pass

    def required_tools(self) -> list[tuple[str, str | None]]:
        pass

    def preflight_source(self) -> dict:
        pass

    def boot(self) -> str:
        pass

    def halt(self) -> None:
        pass

    def system_disk(self) -> Path:
        pass


class VmwareFusionBackend:

    name = "vmware_desktop"
    boot_phase = "vagrant_boot"
    base_store = "vagrant_box"
    base_entry = "box.vmx"
    TOOLS = ("vagrant", "qemu-img", "qemu-io", "vmrun", "ansible", "ansible-playbook")

    def __init__(self, pipeline):
        self.pipeline = pipeline

    @staticmethod
    def discover_tools() -> dict[str, str | None]:
        tools = {name: shutil.which(name) for name in VmwareFusionBackend.TOOLS}
        if tools["vmrun"] is None:
            tools["vmrun"] = next((str(path) for path in VMRUN_CANDIDATES if path.exists()), None)
        return tools

    @staticmethod
    def hypervisor_identity(tools: dict[str, str | None]) -> dict:
        if not tools.get("vmrun"):
            raise RuntimeError("vmrun is not installed")
        vmrun = Path(tools["vmrun"]).resolve()
        bundle = next((parent for parent in vmrun.parents if parent.suffix == ".app"), None)
        if bundle is None:
            raise RuntimeError(f"vmrun is not inside a VMware Fusion application bundle: {vmrun}")
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        return {"product": info["CFBundleName"], "version": info["CFBundleShortVersionString"],
                "build": info["CFBundleVersion"]}

    @staticmethod
    def _box_root(box: str, version: str) -> Path:
        home = Path(os.environ.get("VAGRANT_HOME", str(Path.home() / ".vagrant.d"))).expanduser()
        return home / "boxes" / box.replace("/", "-VAGRANTSLASH-") / version

    @staticmethod
    def base_location(box: str, version: str) -> dict:
        root = VmwareFusionBackend._box_root(box, version)
        found = sorted(root.glob(f"**/{VmwareFusionBackend.name}/box.vmx"))
        if len(found) != 1:
            raise RuntimeError(f"expected one installed {VmwareFusionBackend.name} box {box} {version}, found {len(found)}")
        relative = found[0].parent.relative_to(root).parts
        return {"kind": VmwareFusionBackend.base_store, "box": box, "version": version,
                "architecture": relative[0] if len(relative) == 2 else None,
                "provider": VmwareFusionBackend.name}

    @staticmethod
    def base_directory(location: dict) -> Path:
        directory = VmwareFusionBackend._box_root(location["box"], location["version"])
        if location["architecture"] is not None:
            directory = directory / location["architecture"]
        return (directory / location["provider"]).resolve()

    @staticmethod
    def base_files(directory: Path) -> list[Path]:
        from fmd.generation import vmware_clone

        return sorted(path for path in vmware_clone.source_entries(directory) if path.is_file())

    @staticmethod
    def check_base(directory: Path, entry: str, rows: list[dict]) -> None:
        from fmd.generation.recipe import _verify_base_closure

        files = {str((directory / row["path"]).resolve()): {**row, "role": "base"} for row in rows}
        _verify_base_closure({"vmx_path": str(directory / entry)}, files)

    @staticmethod
    def host_checks(tools: dict[str, str | None]) -> list[tuple[str, bool, object]]:
        plugins = VmwareFusionBackend.host_facts(tools).get("vagrant_plugins") if tools.get("vagrant") else None
        utility = Path("/opt/vagrant-vmware-desktop/bin/vagrant-vmware-utility")
        return [
            ("vagrant-vmware-desktop plugin",
             isinstance(plugins, list) and any(line.startswith("vagrant-vmware-desktop") for line in plugins),
             plugins or "run: vagrant plugin install vagrant-vmware-desktop"),
            ("vagrant-vmware-utility", utility.is_file(),
             str(utility) if utility.is_file() else "install the Vagrant VMware Utility from HashiCorp"),
        ]

    @staticmethod
    def host_facts(tools: dict[str, str | None]) -> dict:
        from fmd.core.owned_process import run_owned

        try:
            listing = run_owned([tools["vagrant"], "plugin", "list"], capture_output=True, timeout=120).stdout
        except Exception as error:
            return {"vagrant_plugins": f"unavailable: {type(error).__name__}"}
        return {"vagrant_plugins": [line.strip() for line in listing.splitlines() if line.strip()]}

    @staticmethod
    def supported_host() -> str | None:
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            return "paper generation requires macOS on ARM64"
        return None

    def check_host(self) -> None:
        if not self.pipeline.is_macos or self.supported_host() is not None:
            raise RuntimeError("paper generation requires macOS on ARM64")

    def required_tools(self) -> list[tuple[str, str | None]]:
        p = self.pipeline
        return [("vagrant", p.vagrant_cmd), ("qemu-img", p.qemu_img_cmd),
                ("qemu-io", p.first_available(["qemu-io"])), ("vmrun", p.vmrun_cmd),
                ("ansible", p.ansible_cmd), ("ansible-playbook", p.ansible_playbook_cmd)]

    def preflight_source(self) -> dict:
        source = self.pipeline.preflight_vmware_source()
        self.pipeline.preflight_generation_storage(source["vmx_path"])
        return source

    def boot(self) -> str:
        return self.pipeline.run_vmware_vagrant_boot(self.pipeline.vagrant_environment())

    def halt(self) -> None:
        self.pipeline.halt_vm()

    def system_disk(self) -> Path:
        return self.pipeline.discover_disk_path()


QEMU_BASE_HOME_ENV = "FMD_QEMU_BASE_HOME"
QEMU_ACCELERATORS = {"Linux": "kvm", "Windows": "whpx", "Darwin": "hvf"}
QEMU_WINDOWS_DIR = Path(r"C:\Program Files\qemu")
# Hide VT-x/AMD-V: Windows 11 24H2+ otherwise starts its own hypervisor, which hangs nested guests.
# On KVM the Hyper-V clock and timer enlightenments keep guest time within the generator's
# +-2 s clock checkpoints (VMware provides its own timekeeping; WHPX is Hyper-V already).
QEMU_CPU = {"kvm": "host,-vmx,-svm,hv-relaxed,hv-vapic,hv-spinlocks=0x1fff,hv-time", "whpx": "max,-vmx,-svm",
            "hvf": "host"}
QEMU_FIRMWARE = (("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
                 ("share/edk2-x86_64-code.fd", "share/edk2-i386-vars.fd"),
                 ("../share/qemu/edk2-x86_64-code.fd", "../share/qemu/edk2-i386-vars.fd"))


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def ansible_winrm_vars(port: int) -> dict:
    """Connection variables for the QEMU guest: loopback port forward, encrypted NTLM as vagrant."""
    return {"ansible_host": "127.0.0.1", "ansible_port": int(port), "ansible_user": "vagrant",
            "ansible_password": "vagrant", "ansible_connection": "winrm", "ansible_winrm_scheme": "http",
            "ansible_winrm_transport": "ntlm", "ansible_winrm_message_encryption": "always",
            "ansible_winrm_server_cert_validation": "ignore",
            # as the playbook run allows: a guest busy just after boot can take over a minute to answer
            "ansible_winrm_operation_timeout_sec": 120, "ansible_winrm_read_timeout_sec": 130}


def ansible_adhoc(ansible: str, port: int, module: str, args: dict, *, timeout: int):
    import json
    import subprocess

    # no -a for a module without arguments: ansible-core reads an empty JSON object as a raw
    # parameter, which modules such as win_ping refuse
    module_args = ["-a", json.dumps(args)] if args else []
    return subprocess.run([ansible, "all", "-i", "127.0.0.1,", "-m", module, *module_args,
                           "-e", json.dumps(ansible_winrm_vars(port))],
                          capture_output=True, text=True, timeout=timeout)


def qemu_guest_command(qemu: Path, state: Path, *, winrm_port: int, monitor_port: int,
                       memory_mib: int = 4096) -> list[str]:
    """Machine, firmware, system disk and a WinRM-only network for a guest booted from state/system.qcow2."""
    accel = QemuBackend.accelerator()
    return [str(qemu), "-machine", f"q35,accel={accel}", "-cpu", QEMU_CPU[accel], "-smp", "2",
            "-m", str(memory_mib),
            "-drive", f"if=pflash,format=raw,readonly=on,file={state / 'code.fd'}",
            "-drive", f"if=pflash,format=raw,file={state / 'vars.fd'}",
            "-drive", f"id=system,if=none,format=qcow2,file={state / 'system.qcow2'}",
            "-device", "ide-hd,drive=system,bus=ide.0,bootindex=0",
            "-vga", "std", "-display", "none",
            "-monitor", f"tcp:127.0.0.1:{monitor_port},server,nowait",
            "-serial", f"file:{state / 'serial.log'}"]


def prepare_overlay(qemu_img: str, base: Path, qemu: Path, state: Path) -> None:
    """A disposable overlay on the base plus private copies of the UEFI firmware and variables."""
    import subprocess

    subprocess.run([qemu_img, "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
                    str(state / "system.qcow2")], check=True, timeout=120)
    code, _ = QemuBackend._firmware(qemu)
    shutil.copyfile(code, state / "code.fd")
    shutil.copyfile(base.with_name("base-vars.fd"), state / "vars.fd")


class QemuBackend:
    """The paper's guest on plain QEMU (KVM, WHPX or HVF) instead of VMware Fusion.

    Mirrors the Vagrantfile's VM: 4 GiB, 2 vCPUs, the frozen MAC and BIOS UUID, two NICs,
    the RTC started at UTC minus the frozen bias, three 64 MiB USB disks on xHCI ports
    5/3/2, no host time sync. Guest writes go to a disposable overlay on the base.
    """

    name = "qemu"
    boot_phase = "qemu_boot"
    base_store = "qemu_base"
    base_entry = "base.qcow2"
    TOOLS = ("qemu-system", "qemu-img", "qemu-io", "ansible", "ansible-playbook")
    WINRM_USER = ("vagrant", "vagrant")

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.process = None
        self.winrm_port = None

    @staticmethod
    def _which(name: str) -> str | None:
        found = shutil.which(name)
        if found is None and platform.system() == "Windows" and (QEMU_WINDOWS_DIR / f"{name}.exe").is_file():
            found = str(QEMU_WINDOWS_DIR / f"{name}.exe")
        return found

    @staticmethod
    def discover_tools() -> dict[str, str | None]:
        system = ("qemu-system-aarch64" if platform.machine().lower() in {"arm64", "aarch64"}
                  else "qemu-system-x86_64")
        return {"qemu-system": QemuBackend._which(system), "qemu-img": QemuBackend._which("qemu-img"),
                "qemu-io": QemuBackend._which("qemu-io"), "ansible": shutil.which("ansible"),
                "ansible-playbook": shutil.which("ansible-playbook")}

    @staticmethod
    def accelerator() -> str:
        return QEMU_ACCELERATORS[platform.system()]

    @staticmethod
    def hypervisor_identity(tools: dict[str, str | None]) -> dict:
        import subprocess

        if not tools.get("qemu-system"):
            raise RuntimeError("qemu-system is not installed")
        line = subprocess.run([tools["qemu-system"], "--version"], capture_output=True, text=True,
                              timeout=60, check=True).stdout.splitlines()[0].strip()
        version = line.removeprefix("QEMU emulator version ").split()[0]
        return {"product": "QEMU", "version": version, "build": f"{line}; accel={QemuBackend.accelerator()}"}

    @staticmethod
    def _base_root(box: str, version: str) -> Path:
        home = Path(os.environ.get(QEMU_BASE_HOME_ENV, str(Path.home() / ".cache/fmd/qemu-bases"))).expanduser()
        return home / box.replace("/", "-VAGRANTSLASH-") / version

    @staticmethod
    def base_location(box: str, version: str) -> dict:
        root = QemuBackend._base_root(box, version)
        found = sorted(root.glob(f"*/{QemuBackend.name}/{QemuBackend.base_entry}"))
        if len(found) != 1:
            raise RuntimeError(f"expected one QEMU base {box} {version} under {root}, found {len(found)}")
        return {"kind": QemuBackend.base_store, "box": box, "version": version,
                "architecture": found[0].parent.parent.name, "provider": QemuBackend.name}

    @staticmethod
    def base_directory(location: dict) -> Path:
        root = QemuBackend._base_root(location["box"], location["version"])
        return (root / location["architecture"] / location["provider"]).resolve()

    @staticmethod
    def base_files(directory: Path) -> list[Path]:
        return sorted(path for path in directory.iterdir() if path.is_file())

    @staticmethod
    def check_base(directory: Path, entry: str, rows: list[dict]) -> None:
        import json
        import subprocess

        if entry != QemuBackend.base_entry or {row["path"] for row in rows} != {entry, "base-vars.fd"}:
            raise ValueError("a QEMU base is exactly base.qcow2 and base-vars.fd")
        tool = QemuBackend.discover_tools()["qemu-img"] or "qemu-img"
        info = json.loads(subprocess.run([tool, "info", "--output=json", str(directory / entry)],
                                         capture_output=True, text=True, timeout=120, check=True).stdout)
        if info.get("format") != "qcow2" or info.get("backing-filename"):
            raise ValueError("the QEMU base must be a standalone qcow2 image")

    @staticmethod
    def host_facts(tools: dict[str, str | None]) -> dict:
        return {"accelerator": QemuBackend.accelerator(), "machine": platform.machine(),
                "processor": platform.processor() or None}

    @staticmethod
    def supported_host() -> str | None:
        system = platform.system()
        if system not in QEMU_ACCELERATORS:
            return f"QEMU generation does not support {system}"
        if system == "Linux" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
            return "QEMU generation on Linux needs read-write access to /dev/kvm"
        return None

    @staticmethod
    def host_checks(tools: dict[str, str | None]) -> list[tuple[str, bool, object]]:
        problem = QemuBackend.supported_host()
        return [("hardware acceleration", problem is None, problem or QemuBackend.accelerator())]

    def check_host(self) -> None:
        problem = self.supported_host()
        if problem is not None:
            raise RuntimeError(problem)

    def required_tools(self) -> list[tuple[str, str | None]]:
        p = self.pipeline
        tools = self.discover_tools()
        return [("qemu-system", tools["qemu-system"]), ("qemu-img", p.qemu_img_cmd), ("qemu-io", tools["qemu-io"]),
                ("ansible", p.ansible_cmd), ("ansible-playbook", p.ansible_playbook_cmd)]

    def base_path(self) -> Path:
        from fmd.generation import dependency_lock

        return dependency_lock.base_directory(self.pipeline.recipe_bundle["lock"]) / self.base_entry

    def preflight_source(self) -> dict:
        base = self.base_path()
        if not base.is_file():
            raise FileNotFoundError(f"QEMU base is missing: {base}")
        self.pipeline.preflight_generation_storage(base)
        return {"base_path": str(base)}

    @staticmethod
    def _firmware(qemu: Path) -> tuple[Path, Path]:
        for code, variables in QEMU_FIRMWARE:
            code_path, vars_path = (qemu.parent / code).resolve(), (qemu.parent / variables).resolve()
            if code_path.is_file() and vars_path.is_file():
                return code_path, vars_path
        raise FileNotFoundError("no x86_64 UEFI firmware next to QEMU or in /usr/share/OVMF")

    def _monitor(self, command: str) -> str:
        """Run one monitor command; returns what the monitor printed back."""
        import socket
        import time

        with socket.create_connection(("127.0.0.1", self.monitor_port), timeout=10) as connection:
            connection.recv(4096)
            connection.sendall(command.encode() + b"\n")
            time.sleep(0.5)
            connection.settimeout(2)
            try:
                return connection.recv(65536).decode(errors="replace")
            except OSError:
                return ""

    def media_commands(self, hardware: dict, media: list[dict]) -> list[str]:
        """Monitor commands that plug the frozen virtual USB disks into the running guest, in order.

        VMware connects its virtual USB disks once the VM is running, so Windows installs each as a newly
        plugged device and SetupAPI records the install, which the USB scenario requires; QEMU disks
        present at boot are configured without that record. A hot-plugged usb-bot stays detached until
        its SCSI disk is in place and its 'attached' property is set (QEMU's documented sequence).
        Windows takes the USBSTOR serial from the usb-bot; QEMU 9+ caps a SCSI serial at 20 characters.
        """
        commands = []
        for row in media:
            unit, serial = int(row["unit"]), self.usb_serial(hardware, unit=int(row["unit"]))
            path = Path(row["path"]).resolve().as_posix().replace(",", ",,")
            commands += [f'drive_add 0 "if=none,id=usb{unit},format=vmdk,file={path}"',
                         f"device_add usb-bot,id=usb{unit}bot,bus=xhci.0,port={int(row['port'])},serial={serial}",
                         f"device_add scsi-hd,bus=usb{unit}bot.0,scsi-id=0,lun=0,drive=usb{unit},serial={serial[:20]}",
                         f"qom-set /machine/peripheral/usb{unit}bot attached true"]
        return commands

    def attach_media(self, hardware: dict, media: list[dict]) -> None:
        import time

        for command in self.media_commands(hardware, media):
            reply = self._monitor(command).replace(command, "").lower()  # the monitor echoes the command
            if any(word in reply for word in ("error", "could not", "failed", "invalid", "not found", "unknown")):
                raise RuntimeError(f"QEMU refused {command!r}: {reply.strip()[:500]}")
            time.sleep(5 if command.startswith("qom-set") else 1)  # one plug-in at a time, as by hand

    def await_media(self, ansible: str, count: int, timeout: int = 300) -> None:
        """Wait until Windows has installed the plugged disks as USBSTOR disk drives."""
        import subprocess
        import time

        script = ("$n = @(Get-CimInstance Win32_DiskDrive | Where-Object { $_.PNPDeviceID -like 'USBSTOR\\*' }).Count;"
                  f" if ($n -lt {count}) {{ throw \"$n of {count} USB disks installed\" }}")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if ansible_adhoc(ansible, self.winrm_port, "ansible.windows.win_powershell", {"script": script},
                                 timeout=150).returncode == 0:
                    time.sleep(15)  # let SetupAPI finish the install records
                    return
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"Windows did not install the {count} plugged virtual USB disks")
            time.sleep(10)

    def _guest_powershell(self, script: str, timeout: int = 180):
        """Run one PowerShell script in the guest through Ansible's encrypted WinRM connection."""
        return ansible_adhoc(self.pipeline.ansible_cmd, self.winrm_port, "ansible.windows.win_powershell",
                             {"script": script}, timeout=timeout)

    @staticmethod
    def usb_serial(hardware: dict, unit: int) -> str:
        import hashlib

        return hashlib.sha256(f"fmd-qemu-usb.v1:{hardware['uuid_bios']}:{unit}".encode()).hexdigest()[:32].upper()

    def command(self, qemu: Path, state: Path, inputs: dict) -> list[str]:
        from datetime import datetime, timedelta, timezone

        hardware = inputs["fmd_hardware"]
        bias = inputs.get("fmd_vmware_boot_clock_bias_minutes", 0)
        rtc = (datetime.now(timezone.utc) - timedelta(minutes=bias)).strftime("%Y-%m-%dT%H:%M:%S")
        mac = hardware["base_mac"]
        second_mac = mac[:-2] + f"{(int(mac[-2:], 16) + 1) % 256:02X}"
        command = qemu_guest_command(qemu, state, winrm_port=self.winrm_port, monitor_port=self.monitor_port)
        command[1:1] = ["-name", hardware["display_name"], "-uuid", hardware["uuid_bios"]]
        # the virtual USB disks are plugged in once Windows runs (media_commands)
        command += ["-rtc", f"base={rtc},clock=host", "-device", "qemu-xhci,id=xhci,p2=8,p3=8"]
        command += [
            "-netdev", f"user,id=nat,restrict=on,hostfwd=tcp:127.0.0.1:{self.winrm_port}-:5985",
            "-device", f"e1000e,netdev=nat,mac={mac}",
            # its own subnet, as VMware's host-only network was: one default route, via the NAT NIC
            "-netdev", "user,id=hostonly,restrict=on,net=192.168.56.0/24",
            "-device", f"e1000e,netdev=hostonly,mac={second_mac}",
        ]
        return command

    def boot(self) -> str:
        import json
        import subprocess
        import time

        p = self.pipeline
        tools = self.discover_tools()
        qemu = Path(tools["qemu-system"])
        state = p.vagrant_state_dir
        state.mkdir(parents=True)
        p.provider_launch_attempted = True
        prepare_overlay(p.qemu_img_cmd, self.base_path(), qemu, state)
        inputs = json.loads(Path(p.population_inputs_path).read_text(encoding="utf-8"))
        media = list(p.native_media_sources)
        frozen = inputs["generation_inputs"]["scenario_inputs"]["usbstor_setupapi_discrepancy_01"]["media"]
        if ([(int(row["unit"]), int(row["port"]), Path(row["path"]).name) for row in media]
                != [(item["unit"], item["port"], item["source_file"]) for item in frozen]):
            raise ValueError("pilot USB layout differs from its frozen recipe")
        self.winrm_port, self.monitor_port = _free_port(), _free_port()
        command = self.command(qemu, state, inputs)
        log = (state / "qemu.log").open("w")
        self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        started = time.monotonic()
        wait = ("$e = Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue |"
                " Where-Object { $_.UserName -like '*\\vagrant' };"
                " if (-not $e) { throw 'no interactive vagrant session yet' }")
        while time.monotonic() - started < 1800:
            if self.process.poll() is not None:
                raise RuntimeError("QEMU exited during boot: " + (state / "qemu.log").read_text(errors="replace")[-2000:])
            try:
                if self._guest_powershell(wait, timeout=120).returncode == 0:
                    self.attach_media(inputs["fmd_hardware"], media)
                    self.await_media(p.ansible_cmd, len(media))
                    return (f"[*] QEMU guest ready after {time.monotonic() - started:.0f}s "
                            f"(WinRM 127.0.0.1:{self.winrm_port}, interactive vagrant session, "
                            f"{len(media)} virtual USB disks plugged in)\n")
            except subprocess.TimeoutExpired:
                pass
            time.sleep(15)
        try:
            self._monitor(f"screendump {p.output_dir / 'qemu-boot-timeout.png'} -f png")
        except OSError:
            pass
        raise TimeoutError("QEMU guest did not reach an interactive vagrant session within 30 minutes")

    def winrm_endpoint(self) -> dict:
        return {"host": "127.0.0.1", "port": str(self.winrm_port), "user": self.WINRM_USER[0],
                "password": self.WINRM_USER[1]}

    def halt(self) -> None:
        import subprocess

        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("the QEMU guest is not running; refusing forensic export")
        try:
            self._guest_powershell("shutdown.exe /s /t 5 /f", timeout=120)
        except subprocess.TimeoutExpired:
            pass  # the connection may drop as Windows shuts down
        try:
            code = self.process.wait(timeout=300)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("guest shutdown did not finish; refusing forensic export from "
                               "an uncleanly stopped guest") from error
        if code != 0:
            raise RuntimeError(f"QEMU exited with {code}; refusing forensic export")

    def system_disk(self) -> Path:
        return self.pipeline.vagrant_state_dir / "system.qcow2"

    def destroy(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=60)
        state = self.pipeline.vagrant_state_dir
        if state.exists():
            shutil.rmtree(state)
        return True


BACKENDS = {VmwareFusionBackend.name: VmwareFusionBackend, QemuBackend.name: QemuBackend}


def backend_for(pipeline) -> VmBackend:
    provider = getattr(pipeline, "provider", VmwareFusionBackend.name)
    try:
        return BACKENDS[provider](pipeline)
    except KeyError:
        raise ValueError(
            f"no generation backend for provider {provider!r}; available: {', '.join(sorted(BACKENDS))}"
        ) from None

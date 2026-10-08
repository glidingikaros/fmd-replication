"""Boot the QEMU base as generation does and exercise the guest paths a replication needs.

One boot with the generation VM definition (frozen-style MAC/UUID, USB disks, two NICs,
RTC bias): WinRM through Ansible (WSL on Windows hosts), the guest facts the dependency
lock declares, and a playbook fetch into a host directory given in an @extra-vars file,
as the generation's checkpoint export does. Then the collection's Windows-parser path:
the QEMU worker on Linux, PowerShell on Windows, running a real PECmd.exe.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fmd.collection.tools.host import parser_appliance
from fmd.generation import recipe
from fmd.generation.backends import QemuBackend, _free_port, ansible_adhoc, ansible_winrm_vars, prepare_overlay
from guest_console import diagnose, wsman_status

FACTS = ("$o = Get-CimInstance Win32_OperatingSystem;"
         " $p = Get-CimInstance SoftwareLicensingProduct -Filter \"ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f'"
         " AND PartialProductKey IS NOT NULL\"; $logs = 'C:\\Windows\\System32\\winevt\\Logs';"
         " [ordered]@{build = [string]$o.BuildNumber; timezone = (Get-TimeZone).Id; locale = (Get-Culture).Name;"
         " arch = $o.OSArchitecture; caption = $o.Caption;"
         " profiles = @(Get-NetConnectionProfile | ForEach-Object { [string]$_.NetworkCategory });"
         " addresses = @(Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + '/' + $_.PrefixLength });"
         " license_status = [int]$p.LicenseStatus; license_grace_minutes = [int]$p.GracePeriodRemaining; license_name = [string]$p.Name;"
         " eventlogs_compressed = ([bool]((Get-Item $logs).Attributes -band [IO.FileAttributes]::Compressed) -or"
         " [bool]((Get-Item \"$logs\\Security.evtx\").Attributes -band [IO.FileAttributes]::Compressed));"
         " where_year = (Get-Item C:\\Windows\\System32\\where.exe).LastWriteTimeUtc.Year;"
         " sysmain = [string](Get-Service SysMain).StartType;"
         " prefetcher = (Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters').EnablePrefetcher;"
         " folder_tabs = (Get-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced).OpenFolderInNewTab}")
CLOCK = "(Get-Date).ToUniversalTime().ToString('o')"
# The USB scenario's own helper on the first virtual USB disk, with what Windows reports about the disks
# and the SetupAPI install sections the helper requires; the scenario hides its output (no_log).
USB_PROBE = """$disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { [ordered]@{index = $_.Index; pnp = $_.PNPDeviceID; size = [UInt64]$_.Size; model = $_.Model} })
$volumes = @(Get-Disk | ForEach-Object { [ordered]@{number = $_.Number; bus = [string]$_.BusType; size = [UInt64]$_.Size; style = [string]$_.PartitionStyle; offline = $_.IsOffline; readonly = $_.IsReadOnly; name = $_.FriendlyName} })
$setup = @(Select-String -Path C:\\Windows\\INF\\setupapi.dev.log -Pattern 'Device Install \\(Hardware initiated\\) - ' | ForEach-Object { $_.Line.Trim() } | Where-Object { $_ -match 'USB' })
$pilotDiskIndex = 0
$scenarioInput = [pscustomobject]@{before_name = 'before.txt'; after_name = 'after.txt'; file_name = 'probe.bin'; shortcut_name = 'probe.lnk'; companion_file = 'probe.vmdk'}
try { $binding = & { HELPER }; $helper = 'ok: ' + ($binding | ConvertTo-Json -Compress -Depth 4) }
catch { $helper = 'failed: ' + $_.Exception.Message + ' (line ' + $_.InvocationInfo.ScriptLineNumber + ': ' + $_.InvocationInfo.Line.Trim() + ')' }
$log = Get-Item C:\\Windows\\INF\\setupapi.dev.log
$headers = @(Select-String -Path $log.FullName -Pattern '^>>>  \\[' | ForEach-Object { $_.Line.Trim() })
$usbstor = @(Select-String -Path $log.FullName -Pattern 'USBSTOR' | Select-Object -First 8 | ForEach-Object { $_.Line.Trim() })
$level = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup' -ErrorAction SilentlyContinue).LogLevel
[ordered]@{helper = $helper; disk_drives = $disks; disks = $volumes; setupapi_usb = $setup;
  setupapi = [ordered]@{bytes = $log.Length; written = $log.LastWriteTimeUtc.ToString('o'); headers = $headers.Count;
  last_headers = @($headers | Select-Object -Last 12); usbstor_lines = $usbstor; log_level = $level}} | ConvertTo-Json -Depth 6 -Compress"""
PLAYBOOK = """- hosts: all
  gather_facts: false
  tasks:
    - ansible.windows.win_shell: Set-Content -Path C:\\fmd-smoke.txt -Value fmd-smoke
    - ansible.builtin.fetch:
        src: C:\\fmd-smoke.txt
        dest: "{{ fmd_factual_checkpoint_directory }}/fmd-smoke.txt"
        flat: true
"""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def result_json(text: str) -> dict:
    return json.loads(text[text.index("{"):])  # "127.0.0.1 | CHANGED => {...}"


def answered(ansible: str, port: int, report: dict) -> bool:
    try:
        result = ansible_adhoc(ansible, port, "ansible.windows.win_ping", {}, timeout=150)
    except subprocess.TimeoutExpired:
        report["last_ansible_output"] = "timed out after 150 s"
        return False
    report["last_ansible_output"] = (result.stdout[-1500:] + result.stderr[-1500:]).strip()
    return result.returncode == 0


def probe(report: dict, key: str, check) -> None:
    """Record one check's result, or its error, so one failing probe never loses the others."""
    try:
        report[key] = check()
    except Exception as error:
        report[f"{key}_error"] = f"{type(error).__name__}: {str(error)[:1500]}"
        log(f"{key}: {report[f'{key}_error']}")


def clock_offset(ansible: str, port: int) -> float:
    """Guest UTC minus host UTC, in seconds, around one WinRM round trip."""
    before = datetime.now(timezone.utc)
    result = ansible_adhoc(ansible, port, "ansible.windows.win_powershell", {"script": CLOCK}, timeout=300)
    after = datetime.now(timezone.utc)
    guest = datetime.fromisoformat(str(result_json(result.stdout)["output"][0]).replace("Z", "+00:00"))
    return round((guest - (before + (after - before) / 2)).total_seconds(), 3)


def boot_and_check(box: str, expected: dict, work: Path) -> dict:
    tools = QemuBackend.discover_tools()
    qemu, ansible = Path(tools["qemu-system"]), tools["ansible"]
    report: dict = {"host_accelerator": QemuBackend.accelerator()}
    # the control node alone, with JSON arguments as the generator passes them (through WSL on Windows)
    control = subprocess.run([ansible, "all", "-i", "localhost,", "-c", "local", "-m", "ansible.builtin.debug",
                              "-a", json.dumps({"msg": "fmd smoke: a b"}), "-e", json.dumps({"fmd_probe": "x y"})],
                             capture_output=True, text=True, timeout=300)
    report["control_node"] = {"exit": control.returncode, "output": (control.stdout + control.stderr)[-1500:]}
    log(f"control node: exit {control.returncode}: {(control.stdout + control.stderr)[-600:]}")
    base = QemuBackend.base_directory(QemuBackend.base_location(box, "0")) / QemuBackend.base_entry
    state = work / "guest"
    state.mkdir(parents=True)
    prepare_overlay(tools["qemu-img"], base, qemu, state)
    media = []
    for unit, port in ((8, 5), (9, 3), (10, 2)):
        path = state / f"media{unit}.vmdk"
        subprocess.run([tools["qemu-img"], "create", "-q", "-f", "vmdk", str(path), str(64 << 20)], check=True)
        media.append({"path": path, "unit": unit, "port": port})
    backend = QemuBackend(pipeline=None)
    backend.winrm_port, backend.monitor_port = _free_port(), _free_port()
    inputs = {"fmd_hardware": recipe.resolved_hardware(20261008), "fmd_vmware_boot_clock_bias_minutes": 480}
    command = backend.command(qemu, state, inputs)
    log(" ".join(command))
    with (state / "qemu.log").open("w") as qemu_log:
        process = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT)
    try:
        started = time.monotonic()
        shot = 0
        while not answered(ansible, backend.winrm_port, report):
            elapsed = time.monotonic() - started
            report["wsman_http"] = wsman_status(backend.winrm_port)
            if report["wsman_http"].startswith("HTTP/") and "wsman_http_seconds" not in report:
                report["wsman_http_seconds"] = round(elapsed)
                log(f"WinRM listener answered over HTTP after {round(elapsed)} s: {report['wsman_http']}")
            if elapsed > shot * 300:
                backend._monitor(f"screendump {work / f'smoke-boot-{shot * 5:02d}min.png'} -f png")
                log(f"waiting for WinRM: listener {report['wsman_http']!r}; "
                    f"Ansible: {report['last_ansible_output'][-300:]!r}")
                shot += 1
            if elapsed > 720 and "console_diagnostic" not in report:
                log("no WinRM after 12 minutes: logging on at the console for a diagnostic")
                report["console_diagnostic"] = round(elapsed)  # a smoke check that needed this fails
                try:
                    diagnose(backend.monitor_port, work)
                except OSError as error:
                    log(f"console diagnostic: {error}")
            if process.poll() is not None:
                report["boot_error"] = "QEMU exited: " + (state / "qemu.log").read_text(errors="replace")[-1500:]
                return report
            if elapsed > 1800:
                backend._monitor(f"screendump {work / 'smoke-boot-timeout.png'} -f png")
                report["boot_error"] = "no WinRM answer within 30 minutes, the generator's boot allowance"
                return report
            time.sleep(15)
        report["winrm_seconds"] = round(time.monotonic() - started)
        log(f"WinRM answered after {report['winrm_seconds']} s")
        backend._monitor(f"screendump {work / 'smoke-booted.png'} -f png")

        # generation waits for vagrant's own desktop session (autologon), so the smoke check does too
        desktop = ("if (-not (Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue |"
                   " Where-Object { $_.UserName -like '*\\vagrant' })) { throw 'no vagrant desktop session yet' }")
        for _ in range(20):
            try:
                if ansible_adhoc(ansible, backend.winrm_port, "ansible.windows.win_powershell", {"script": desktop},
                                 timeout=150).returncode == 0:
                    report["desktop_seconds"] = round(time.monotonic() - started)
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(15)
        # as generation does: the virtual USB disks are plugged in once vagrant's desktop is up
        def plug_media():
            backend.attach_media(inputs["fmd_hardware"], media)
            backend.await_media(ansible, len(media))
            return round(time.monotonic() - started)

        probe(report, "media_plugged_seconds", plug_media)
        probe(report, "clock_offset_start", lambda: clock_offset(ansible, backend.winrm_port))

        def facts():
            result = ansible_adhoc(ansible, backend.winrm_port, "ansible.windows.win_powershell", {"script": FACTS},
                                   timeout=300)
            return result_json(result.stdout)["output"][0] if result.returncode == 0 else result.stdout[-1500:]

        probe(report, "facts", facts)
        log(f"guest facts: {report.get('facts')}")
        guest_facts = report.get("facts") if isinstance(report.get("facts"), dict) else {}
        report["facts_match"] = all(guest_facts.get(key) == value for key, value in expected.items())
        # what the paper's base guaranteed: a licence that keeps Windows up (an unactivated or expired
        # evaluation shuts down hourly; Pro on the generic volume key, as the paper's, stays unactivated),
        # uncompressed event logs (post-export injection and collection read them raw) and a where.exe
        # older than 2026 (the pilot's old-copy control)
        licence_ok = (guest_facts.get("license_status") == 1
                      or "Eval" not in str(guest_facts.get("license_name", "Eval")))
        report["base_ready"] = (licence_ok and guest_facts.get("eventlogs_compressed") is False
                                and isinstance(guest_facts.get("where_year"), int) and guest_facts["where_year"] < 2026)

        def usb_media():
            helper = (Path(parser_appliance.__file__).parents[3] / "generation" / "ansible" / "roles" / "manipulation"
                      / "files" / "pilot_media_prepare.ps1").read_text(encoding="utf-8")
            result = ansible_adhoc(ansible, backend.winrm_port, "ansible.windows.win_powershell",
                                   {"script": USB_PROBE.replace("HELPER", helper)}, timeout=600)
            return json.loads(result_json(result.stdout)["output"][0]) if result.returncode == 0 else result.stdout[-2500:]

        probe(report, "usb_media", usb_media)
        log(f"usb media: {json.dumps(report.get('usb_media'), default=str)[:3000]}")

        checkpoint = work / "checkpoint dir"  # a space, as host paths may have
        checkpoint.mkdir()
        (work / "playbook.yml").write_text(PLAYBOOK, encoding="utf-8")
        (work / "vars.json").write_text(json.dumps({"fmd_factual_checkpoint_directory": str(checkpoint)}),
                                        encoding="utf-8")
        def fetch_through_extra_vars():
            play = subprocess.run([tools["ansible-playbook"], "-i", "127.0.0.1,", str(work / "playbook.yml"),
                                   "-e", json.dumps(ansible_winrm_vars(backend.winrm_port)),
                                   "-e", f"@{work / 'vars.json'}"], capture_output=True, text=True, timeout=600)
            fetched = checkpoint / "fmd-smoke.txt"
            if fetched.is_file() and fetched.read_text(errors="replace").strip() == "fmd-smoke":
                return True
            log("playbook output:\n" + play.stdout[-3000:] + play.stderr[-1500:])
            return False

        probe(report, "extra_vars_fetch", fetch_through_extra_vars)
        probe(report, "clock_offset_end", lambda: clock_offset(ansible, backend.winrm_port))
        if "clock_offset_start" in report and "clock_offset_end" in report:
            report["clock_drift_seconds"] = round(report["clock_offset_end"] - report["clock_offset_start"], 3)
        report["clock_elapsed_seconds"] = round(time.monotonic() - started)
        try:  # the connection may drop as Windows goes down
            ansible_adhoc(ansible, backend.winrm_port, "ansible.windows.win_powershell",
                          {"script": "shutdown.exe /s /t 5 /f"}, timeout=120)
        except subprocess.TimeoutExpired:
            pass
        probe(report, "qemu_exit", lambda: process.wait(timeout=300))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)
    return report


def parser_check(box: str, parsers: Path, work: Path) -> dict:
    """The collection's parser runner with one real command: PECmd.exe --help."""
    package = work / "inputs.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("plan.json", json.dumps({"commands": [
            {"label": "pecmd-help", "category": "smoke", "executable": "PECmd.exe", "arguments": ["--help"]}]}))
        archive.write(parsers / "PECmd.exe", "bin/PECmd.exe")
        archive.writestr("targets/", "")
    script = work / "run_parsers.ps1"
    script.write_text(parser_appliance.GUEST_SCRIPT, encoding="utf-8")
    outputs = work / "outputs.zip"
    if sys.platform == "win32":
        completed = parser_appliance._run_native_windows_parsers(
            package_path=package, script_path=script, outputs_zip=outputs)
    else:
        completed = parser_appliance._run_qemu_parsers(
            windows_box=box, package_path=package, script_path=script, outputs_zip=outputs, log=log)
    report = {"runtime": parser_appliance.parser_runtime()[0], "exit": completed.returncode}
    if outputs.is_file():
        with zipfile.ZipFile(outputs) as archive:
            # Windows PowerShell 5.1 writes backslash entry names; Python converts them only on Windows
            names = {name.replace("\\", "/"): name for name in archive.namelist()}
            receipt = json.load(io.TextIOWrapper(archive.open(names["receipt.json"]), encoding="utf-8-sig"))
            stdout = archive.read(names["logs/pecmd-help.stdout.txt"]).decode("utf-8", "replace")
        report.update(receipt_results=receipt["results"], pecmd_banner="PECmd" in stdout)
    else:
        report["output"] = (completed.stdout or "")[-2000:] + (completed.stderr or "")[-1000:]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-build", required=True)
    parser.add_argument("--parsers", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    box = recipe.qemu_box()
    args.work.mkdir(parents=True, exist_ok=True)
    expected = {"build": args.windows_build, "timezone": "Pacific Standard Time", "locale": "en-US"}
    report: dict = {}

    def keep() -> None:  # the report is written after each part, so a later failure loses nothing
        (args.work / "smoke-report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    probe(report, "guest", lambda: boot_and_check(box, expected, Path(tempfile.mkdtemp(dir=args.work))))
    keep()
    if sys.platform != "win32" and "winrm_seconds" not in report.get("guest", {}):
        report["parsers"] = {"skipped": "the parser VM boots the same base, which never answered WinRM"}
    else:
        probe(report, "parsers", lambda: parser_check(box, args.parsers, Path(tempfile.mkdtemp(dir=args.work))))
    keep()
    print(json.dumps(report, indent=2, default=str))
    guest, parsers = report.get("guest", {}), report.get("parsers", {})
    usb_helper = str((guest.get("usb_media") or {}).get("helper", "")) if isinstance(guest.get("usb_media"), dict) else ""
    passed = ("console_diagnostic" not in guest and guest.get("facts_match") and guest.get("base_ready")
              and usb_helper.startswith("ok:")
              and "desktop_seconds" in guest
              and guest.get("extra_vars_fetch") is True and guest.get("qemu_exit") == 0
              and parsers.get("exit") == 0 and parsers.get("pecmd_banner"))
    log("SMOKE " + ("PASSED" if passed else "FAILED"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

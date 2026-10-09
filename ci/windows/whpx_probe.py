"""Two firmware operations Windows Setup needs, under WHPX and under TCG: a guest reset, and a write of a
non-volatile UEFI variable (to the pflash varstore). The UEFI shell runs startup.nsh from a virtual FAT
disk. Also prints QEMU's raw monitor reply to `info blockstats` on this host."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "src")]
from fmd.generation.qemu_host import uefi_firmware  # noqa: E402

SCRIPTS = {
    "reset": "@echo -off\r\nreset\r\n",
    "setvar": "@echo -off\r\nsetvar FmbProbe -guid 0d8c8e5d-1d7e-4e8c-9c2b-2b0e8e0c6a11 -nv -bs -rt =01020304\r\nreset -s\r\n",
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def raw_reply(port: int, command: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        greeting = connection.recv(65536)
        connection.sendall(command.encode() + b"\n")
        data, deadline = b"", time.monotonic() + 6
        connection.settimeout(1)
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except OSError:
                continue
            if not chunk:
                break
            data += chunk
        return greeting + b"||" + data


def trial(qemu: Path, accel: str, test: str, seconds: int) -> dict:
    code, variables = uefi_firmware(qemu)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        disk = work / "disk"
        disk.mkdir()
        (disk / "startup.nsh").write_text(SCRIPTS[test], newline="")
        shutil.copyfile(variables, work / "vars.fd")
        port = free_port()
        command = [str(qemu), "-machine", "q35", "-accel", accel, "-cpu", "max,-vmx,-svm" if accel == "whpx" else "max",
                   "-m", "1024", "-smp", "2", "-display", "none", "-nic", "none",
                   "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                   "-drive", f"if=pflash,format=raw,file={work / 'vars.fd'}",
                   "-drive", f"if=none,id=probe,driver=vvfat,dir={disk},fat-type=16",
                   "-device", "ide-hd,drive=probe,bus=ide.0",
                   "-serial", f"file:{work / 'serial.log'}", "-monitor", f"tcp:127.0.0.1:{port},server,nowait"]
        started = time.monotonic()
        vm = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        reply = b""
        try:
            vm.wait(timeout=seconds)
            exited = round(time.monotonic() - started)
        except subprocess.TimeoutExpired:
            exited = None
            try:
                reply = raw_reply(port, "info blockstats")
            except OSError as error:
                reply = repr(error).encode()
            vm.kill()
        output = vm.communicate()[0].decode(errors="replace")
        serial = (work / "serial.log").read_bytes().decode(errors="replace")
        return {"accel": accel, "test": test, "qemu_exited_after_s": exited, "boots": serial.count("BdsDxe: loading"),
                "shell_started": "Shell>" in serial or "startup.nsh" in serial,
                "qemu_output": output[-400:], "serial_tail": serial[-600:], "blockstats_reply": repr(reply[:900])}


def main() -> int:
    qemu = Path(shutil.which("qemu-system-x86_64") or r"C:\Program Files\qemu\qemu-system-x86_64.exe").resolve()
    for accel, test, seconds in (("whpx", "reset", 90), ("whpx", "setvar", 90), ("tcg", "reset", 180),
                                 ("tcg", "setvar", 180)):
        print("PROBE " + json.dumps(trial(qemu, accel, test, seconds)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

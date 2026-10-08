"""Measure how fast a QEMU guest's clock runs against the host's, under one set of timer options.

Boots an overlay of the cached base the way generation boots it (frozen-style UUID and MACs, a
second NIC, no route out, the RTC at UTC minus 480 minutes), stops the guest's time service as
the generation's clock policy does, then samples the guest's UTC clock over WinRM for a few
minutes. Prints the drift in parts per million: the generation's checkpoints allow 2 s between
its one clock sync and its last checkpoint, so a guest needs to stay well under 1000 ppm.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from install import ACCELERATORS, firmware, free_port, monitor, qemu_binary, qemu_tool, session  # noqa: E402

SAMPLE = "[DateTime]::UtcNow.ToString('o')"


def sample(port: int) -> tuple[float, float]:
    """(host midpoint, guest minus host) in seconds, from one short WinRM round trip."""
    shell = session(port)
    sent = time.time()
    result = shell.run_ps(SAMPLE)
    received = time.time()
    guest = datetime.fromisoformat(result.std_out.decode().strip().replace("Z", "+00:00")).timestamp()
    middle = (sent + received) / 2
    return middle, guest - middle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True, help="directory with base.qcow2 and base-vars.fd")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cpu", required=True)
    parser.add_argument("--machine", default="q35")
    parser.add_argument("--accel-options", default="", help="appended to -accel <name>, e.g. ,kernel-irqchip=off")
    parser.add_argument("--minutes", type=float, default=6)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()

    accelerator = ACCELERATORS[platform.system()]
    qemu = qemu_binary()
    code, _ = firmware(qemu)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([qemu_tool(qemu, "qemu-img"), "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
                    str((args.base / "base.qcow2").resolve()), "probe.qcow2"], cwd=work, check=True)
    shutil.copyfile(code, work / "code.fd")
    shutil.copyfile(args.base / "base-vars.fd", work / "vars.fd")
    winrm_port, monitor_port = free_port(), free_port()
    rtc = (datetime.now(timezone.utc) - timedelta(minutes=480)).strftime("%Y-%m-%dT%H:%M:%S")
    command = [
        str(qemu), "-name", "Forensic-Gen-bb68e6cd", "-uuid", "bb68e6cd-df60-7a1b-cfdf-8b48b8238358",
        "-machine", args.machine, "-accel", accelerator + args.accel_options, "-cpu", args.cpu,
        "-smp", "2", "-m", "4096", "-rtc", f"base={rtc},clock=host",
        "-drive", "if=pflash,format=raw,readonly=on,file=code.fd", "-drive", "if=pflash,format=raw,file=vars.fd",
        "-drive", "id=disk,if=none,format=qcow2,file=probe.qcow2", "-device", "ide-hd,drive=disk,bus=ide.0,bootindex=0",
        "-device", "qemu-xhci,id=xhci,p2=8,p3=8",
        "-netdev", f"user,id=nat,restrict=on,hostfwd=tcp:127.0.0.1:{winrm_port}-:5985",
        "-device", "e1000e,netdev=nat,mac=00:50:56:3B:68:E6",
        "-netdev", "user,id=hostonly,restrict=on,net=192.168.56.0/24",
        "-device", "e1000e,netdev=hostonly,mac=00:50:56:3B:68:E7",
        "-vga", "std", "-display", "none", "-monitor", f"tcp:127.0.0.1:{monitor_port},server,nowait",
    ]
    print(" ".join(command), flush=True)
    with (work / "qemu.log").open("w") as qemu_log:
        vm = subprocess.Popen(command, cwd=work, stdout=qemu_log, stderr=subprocess.STDOUT)
    report: dict = {"label": args.label, "cpu": args.cpu, "machine": args.machine, "accel": args.accel_options}
    try:
        started = time.monotonic()
        while True:
            try:
                session(winrm_port).run_ps("$null = Stop-Service w32time -Force -ErrorAction SilentlyContinue; 'ok'")
                break
            except Exception:
                if vm.poll() is not None or time.monotonic() - started > 1500:
                    report["error"] = "no WinRM: " + (work / "qemu.log").read_text(errors="replace")[-800:]
                    return 1
                time.sleep(10)
        report["winrm_seconds"] = round(time.monotonic() - started)
        time.sleep(60)  # let the boot settle before measuring
        points = []
        end = time.monotonic() + args.minutes * 60
        while time.monotonic() < end:
            points.append(sample(winrm_port))
            time.sleep(20)
        xs = [x - points[0][0] for x, _ in points]
        ys = [y for _, y in points]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sum((x - mean_x) ** 2 for x in xs)
        report.update(samples=len(points), seconds=round(xs[-1]), drift_ppm=round(slope * 1e6),
                      gained_seconds=round(ys[-1] - ys[0], 3), offsets=[round(y, 3) for y in ys])
        monitor(monitor_port, f"screendump {work / 'probe.png'} -f png")
        return 0
    finally:
        print("CLOCK PROBE " + json.dumps(report), flush=True)
        (work / "clock-probe.json").write_text(json.dumps(report, indent=2))
        vm.kill()
        vm.wait()


if __name__ == "__main__":
    raise SystemExit(main())

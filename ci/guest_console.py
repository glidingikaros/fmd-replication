"""Look inside a QEMU Windows guest whose WinRM stays silent: probe the forwarded port from the
host, then log on at the console through the QEMU monitor and screenshot what PowerShell reports.
Standard library only, so the base install (no fmd environment) and the smoke check share it.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

KEY_NAMES = {" ": "spc", "-": "minus", "=": "equal", ".": "dot", ",": "comma", "/": "slash", "\\": "backslash",
             ";": "semicolon", "'": "apostrophe", "[": "bracket_left", "]": "bracket_right", "\n": "ret"}
SHIFTED = {"|": "backslash", "&": "7", ">": "dot", "<": "comma", '"': "apostrophe", ":": "semicolon", "_": "minus",
           "+": "equal", "(": "9", ")": "0", "{": "bracket_left", "}": "bracket_right", "$": "4", "@": "2",
           "!": "1", "?": "slash", "*": "8", "%": "5", "#": "3"}
# One screenshot per group: addresses and profiles, services and the WinRM listener, autologon; then
# the licence, the firewall profiles and the latest System errors.
CHECKS = (
    ["Get-NetIPAddress -AddressFamily IPv4 | ft InterfaceAlias,IPAddress,PrefixOrigin -a",
     "Get-NetConnectionProfile | ft InterfaceAlias,NetworkCategory,IPv4Connectivity -a",
     "Get-Service WinRM,MpsSvc,sppsvc,Dhcp | ft Name,Status,StartType -a",
     "netstat -ano -p tcp | findstr 5985",
     "Test-WSMan localhost | ft ProductVersion -a",
     "(gp 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon').AutoAdminLogon"],
    ["cls",
     "cscript //nologo C:\\Windows\\System32\\slmgr.vbs /dli",
     "Get-NetFirewallProfile | ft Name,Enabled,DefaultInboundAction -a",
     "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2} -MaxEvents 8 | ft TimeCreated,Id,ProviderName -a"],
)


def wsman_status(port: int) -> str:
    """The guest's WinRM listener seen from the host without Ansible: an unauthenticated POST gets 401."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=20) as connection:
            connection.settimeout(20)
            connection.sendall(b"POST /wsman HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n")
            return connection.recv(200).decode(errors="replace").splitlines()[0] or "empty reply"
    except (OSError, IndexError) as error:
        return f"{type(error).__name__}: {error}"


def typed(text: str) -> list[str]:
    """Monitor sendkey commands that type text on the guest's US keyboard."""
    commands = []
    for char in text:
        key = ("shift-" + SHIFTED[char] if char in SHIFTED else "shift-" + char.lower() if char.isupper()
               else KEY_NAMES.get(char, char))
        commands.append(f"sendkey {key} 40")
    return commands


def send(port: int, commands: list[str], settle: float) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.recv(4096)
        for command in commands:
            connection.sendall(command.encode() + b"\n")
            time.sleep(0.05)
    time.sleep(settle)


def diagnose(port: int, shots: Path) -> None:
    """Log on as vagrant at the console, open PowerShell from Start search and screenshot CHECKS."""
    def shot(name: str) -> str:
        return f"screendump {shots / f'console-{name}.png'} -f png"

    send(port, ["sendkey ret"], 6)  # the lock screen curtain, then the password box
    send(port, [shot("1-logon")] + typed("vagrant\n"), 75)
    send(port, [shot("2-desktop"), "sendkey meta_l"], 4)
    send(port, typed("powershell"), 8)  # Start search needs its results before Enter
    send(port, ["sendkey ret"], 25)
    send(port, ["sendkey meta_l-up"], 3)
    for number, lines in enumerate(CHECKS, start=3):
        text = "\n".join(lines) + "\n"
        send(port, typed(text), 0.15 * len(text) + 25)
        send(port, [shot(f"{number}-console")], 2)

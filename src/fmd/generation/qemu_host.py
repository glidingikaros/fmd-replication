"""How this host runs the QEMU guest: the accelerator, the CPU model and the UEFI firmware.

Standard library only: the base installer (ci/windows/install.py), which runs outside the project's
environment, imports this too, so the base is installed on the same CPU model and firmware that
generation boots it on.
"""

from __future__ import annotations

from pathlib import Path

QEMU_ACCELERATORS = {"Linux": "kvm", "Windows": "whpx", "Darwin": "hvf"}
# Hide VT-x/AMD-V: Windows 11 24H2+ otherwise starts its own hypervisor, which hangs nested guests.
# On KVM the Hyper-V clock and timer enlightenments keep guest time within the generator's
# +-2 s clock checkpoints (VMware provides its own timekeeping; WHPX is Hyper-V already).
QEMU_CPU = {"kvm": "host,-vmx,-svm,hv-relaxed,hv-vapic,hv-spinlocks=0x1fff,hv-time", "whpx": "max,-vmx,-svm",
            "hvf": "host"}
# (code, variable store), beside qemu-system-x86_64 unless absolute. The first pair present wins, so the
# base install and every generation on a host boot the same firmware: the base keeps its variable store.
QEMU_FIRMWARE = (
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),  # Debian, Ubuntu: ovmf
    ("share/edk2-x86_64-code.fd", "share/edk2-i386-vars.fd"),  # QEMU for Windows
    ("../share/qemu/edk2-x86_64-code.fd", "../share/qemu/edk2-i386-vars.fd"),  # Homebrew, Nix, source builds
    ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),  # Fedora, RHEL: edk2-ovmf
    ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),  # Arch: edk2-ovmf
    ("/usr/share/qemu/ovmf-x86_64-4m-code.bin", "/usr/share/qemu/ovmf-x86_64-4m-vars.bin"),  # openSUSE
)


def uefi_firmware(qemu: Path) -> tuple[Path, Path]:
    """The x86-64 UEFI firmware (OVMF) and its variable-store template for this QEMU."""
    for code, variables in QEMU_FIRMWARE:
        pair = (qemu.parent / code).resolve(), (qemu.parent / variables).resolve()
        if pair[0].is_file() and pair[1].is_file():
            return pair
    raise FileNotFoundError(f"no x86-64 UEFI firmware (OVMF) beside {qemu} or where Debian, Ubuntu, Fedora, Arch or "
                            "openSUSE install it: install your distribution's OVMF package (ovmf or edk2-ovmf)")

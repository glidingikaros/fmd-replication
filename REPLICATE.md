# Replicating the I-series experiment

This reruns the paper's experiment on your machine: it generates the Windows disk images I1, I2 and I3,
collects their evidence, and runs the deterministic analysis. An image replicates when strict admission
passes, which requires the deterministic arm to answer all nine questions exactly.

Every image is a Windows guest. Your machine is the host that runs the guest:

| Host | Guest engine | Windows guest |
|---|---|---|
| macOS on Apple silicon | VMware Fusion through Vagrant, the paper's own setup | Windows 11 ARM64, the paper's box |
| Linux x86-64 | QEMU with KVM | Windows 11 Pro x64, built from Microsoft's ISO |
| Windows x64 | QEMU with Windows Hypervisor Platform; Ansible runs in WSL 1 | Windows 11 Pro x64, built from Microsoft's ISO |

The guest definition, scenario scripts, collection tools and analysis are the same on every host.

## What you need

On every host:

- [uv](https://docs.astral.sh/uv/) and a checkout of this repository;
- about 40 GB free per image in flight, plus about 10 GB for the cached Windows base;
- internet access during setup.

On Linux and Windows, also Microsoft's Windows 11 ISO, which you download yourself (its links last a
day): on [microsoft.com/software-download/windows11](https://www.microsoft.com/software-download/windows11)
choose *Windows 11 (multi-edition ISO for x64 devices)*, then *English (United States)*. The file is
`Windows11_Client_x64_en-us_26300_9457.iso`; setup checks its SHA-256. Microsoft offers only its current
build, so a later download can be a newer one. Setup then stops; `--unpinned-iso` builds the base from it
anyway, and every result records that build and the ISO's SHA-256.

Then, depending on the host:

**Linux**

```bash
sudo apt-get update && sudo apt-get install qemu-system-x86 qemu-utils ovmf
```

On Fedora, `sudo dnf install qemu-system-x86-core qemu-img edk2-ovmf`; on Arch, `sudo pacman -S qemu-system-x86 qemu-img edk2-ovmf`; on openSUSE, `sudo zypper install qemu-x86 qemu-tools qemu-ovmf-x86_64`.
Your user needs read-write access to `/dev/kvm`. If it doesn't have it, run `sudo usermod -aG kvm $USER` and log in again.

**Windows** (an administrator PowerShell, then one reboot)

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform
New-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force
winget install SoftwareFreedomConservancy.QEMU
wsl --install -d Ubuntu-24.04 --no-launch
wsl --set-version Ubuntu-24.04 1
```

The second line lifts Windows' 260-character path limit: collection writes longer paths.

**macOS**

- VMware Fusion 13 and Vagrant with the `vagrant-vmware-desktop` plugin.
- Ansible (`brew install ansible`).
- The paper's box, built with `tools/base-image/windows11-arm64/build-vmware-box.sh`.

## Run it

```bash
uv sync --locked --all-extras
uv run fmd replicate doctor
uv run fmd replicate setup --iso ~/Downloads/Windows11_Client_x64_en-us_26300_9457.iso  # macOS: no --iso
uv run fmd replicate run I1 I2 I3
```

- **`doctor`** checks the host and prints the exact fix for anything missing.
- **`setup`** installs, all under `~/.cache/fmd` (or `FMD_CACHE`):
  - the pinned .NET runtime;
  - Ansible;
  - the pinned collection tools.

  On Linux and Windows it also builds the Windows base from the ISO. That takes about 50 minutes and happens once. `fmd replicate` puts all of these on `PATH` for the commands it runs, so no shell configuration is needed.
- **`run`** writes each image to `replication/<image>/` and a `replication/summary.json` that lists, per image, the admission result, how many questions were exact, and F1.

  Each image takes roughly an hour: about 30 minutes to generate on a fast host (longer under nested virtualization) and about 15 minutes to collect and analyse. A generation that fails while the guest boots or is provisioned is retried with the same frozen recipe, up to `--attempts` times (default 3). The study needed repeat attempts too, because the native ShellBag step is occasionally slow.

## What is pinned, and what can drift

- **The collection tools.** `eztools-lock.json` pins the exact bytes of Eric Zimmerman's tools. Setup downloads those bytes; building from source cannot reproduce them, for two reasons:
  - Costura compresses each tool's embedded dependencies through CPU-specific code paths;
  - 46 registry plugins stamp their build time into their version.

  PECmd and SBECmd are Microsoft-Windows-only. Setup downloads the official builds, and collection checks them against the lock's validated hashes. They run in the paper's parser VM on macOS, natively on Windows, and in a disposable QEMU guest on Linux.
- **The Windows base on Linux and Windows.** It is Windows 11 Pro from Microsoft's consumer ISO, on the paper's generic volume licence key and never activated, as the paper's base was. It has the paper's base settings:
  - user `vagrant` with automatic logon, Pacific time, US English;
  - sleep, updates and automatic restarts off;
  - outbound traffic blocked and event logs uncompressed, by the paper's `offline-base.ps1`.

  The Enterprise Evaluation is not used. Its online activation does not survive the virtual hardware that generation boots it on, and an unactivated evaluation shuts down every hour. Microsoft offers only its current build, so once it replaces 26300.9457 the pinned SHA-256 stops matching and setup asks for `--unpinned-iso`. `guest.json` records the build and the ISO's SHA-256, the dependency lock pins the build for every run on that host, and each row of `summary.json` names the base (`windows_base`: build, ISO SHA-256, whether it is the pinned ISO).
- **Python.** `.python-version` pins CPython 3.13.5, which every run so far used; uv downloads it when the host lacks it.
- **Images are never bit-identical.** Neither are the paper's: each frozen recipe draws a fresh random assignment, and Windows installs vary. What replicates is the protocol and the result: 9/9 exact under strict admission.

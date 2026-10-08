# Replicating the I-series experiment

This reruns the paper's experiment on your machine: it generates the Windows disk images I1, I2 and I3,
collects their evidence, and runs the deterministic analysis. An image replicates when strict admission
passes, which requires the deterministic arm to answer all nine questions exactly.

Every image is a Windows guest. Your machine is the host that runs the guest:

| Host | Guest engine | Windows guest |
|---|---|---|
| macOS on Apple silicon | VMware Fusion through Vagrant, the paper's own setup | Windows 11 ARM64, the paper's box |
| Linux x86-64 | QEMU with KVM | Windows 11 x64, built from Microsoft's ISO |
| Windows x64 | QEMU with Windows Hypervisor Platform; Ansible runs in WSL 1 | Windows 11 x64, built from Microsoft's ISO |

The guest definition, scenario scripts, collection tools and analysis are the same on every host.

## What you need

On every host:

- [uv](https://docs.astral.sh/uv/) and a checkout of this repository;
- about 40 GB free per image in flight, plus about 10 GB for the cached Windows base;
- internet access during setup.

Then, depending on the host:

**Linux**

```bash
sudo apt-get install qemu-system-x86 qemu-utils ovmf
```

Your user needs read-write access to `/dev/kvm`. If it doesn't have it, run `sudo usermod -aG kvm $USER` and log in again.

**Windows** (an administrator PowerShell, then one reboot)

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform
winget install SoftwareFreedomConservancy.QEMU
wsl --install -d Ubuntu-24.04 --no-launch
wsl --set-version Ubuntu-24.04 1
```

**macOS**

- VMware Fusion 13 and Vagrant with the `vagrant-vmware-desktop` plugin.
- Ansible (`brew install ansible`).
- The paper's box, built with `tools/base-image/windows11-arm64/build-vmware-box.sh`.

## Run it

```bash
uv sync --locked --all-extras
uv run fmd replicate doctor
uv run fmd replicate setup
uv run fmd replicate run I1 I2 I3
```

- **`doctor`** checks the host and prints the exact fix for anything missing.
- **`setup`** installs, all under `~/.cache/fmd` (or `FMD_CACHE`):
  - the pinned .NET runtime;
  - Ansible;
  - the pinned collection tools.

  On Linux and Windows it also builds the Windows base from Microsoft's ISO. That takes about 40 minutes and happens once. `fmd replicate` puts all of these on `PATH` for the commands it runs, so no shell configuration is needed.
- **`run`** writes each image to `replication/<image>/` and a `replication/summary.json` that lists, per image, the admission result, how many questions were exact, and F1.

  Each image takes roughly an hour: about 30 minutes to generate on a fast host (longer under nested virtualization) and about 15 minutes to collect and analyse. A generation that fails while the guest boots or is provisioned is retried with the same frozen recipe, up to `--attempts` times (default 3). The study needed repeat attempts too, because the native ShellBag step is occasionally slow.

## What is pinned, and what can drift

- **The collection tools.** `eztools-lock.json` pins the exact bytes of Eric Zimmerman's tools. Setup downloads those bytes; building from source cannot reproduce them, for two reasons:
  - Costura compresses each tool's embedded dependencies through CPU-specific code paths;
  - 46 registry plugins stamp their build time into their version.

  PECmd and SBECmd are Microsoft-Windows-only. Setup downloads the official builds, and collection checks them against the lock's validated hashes. They run in the paper's parser VM on macOS, natively on Windows, and in a disposable QEMU guest on Linux.
- **The Windows base on Linux and Windows.** It is built from Microsoft's Windows 11 Enterprise Evaluation ISO with the paper's base settings:
  - user `vagrant` with automatic logon, Pacific time, US English;
  - sleep, updates and automatic restarts off;
  - outbound traffic blocked and event logs uncompressed, by the paper's `offline-base.ps1`.

  The evaluation activates online once, during setup, and stays licensed for 90 days. Microsoft refreshes the ISO behind its link, so a later setup can produce a newer build. `guest.json` records the build, and the dependency lock pins it for every run on that host.
- **Images are never bit-identical.** Neither are the paper's: each frozen recipe draws a fresh random assignment, and Windows installs vary. What replicates is the protocol and the result: 9/9 exact under strict admission.

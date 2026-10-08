# Cross-host replication of the I-series experiment

8 October 2026. This report covers paper images I1, I2 and I3, each generated, collected and run through
the deterministic analysis to strict admission on three host operating systems. An image replicates when
admission passes, which requires the deterministic arm to be exact on all nine questions. No LLM
conditions were dispatched.

## Results

| Host | I1 | I2 | I3 |
|---|---|---|---|
| macOS 27, Apple silicon (the paper's setup) | 9/9, F1 1.0 | 9/9, F1 1.0 | 9/9, F1 1.0 |
| Linux, GitHub `ubuntu-24.04` runner | pending | pending | pending |
| Windows, GitHub `windows-2025` runner | pending | pending | pending |

macOS counts per image:

| Image | True positives | True negatives | False positives | False negatives | Generation attempts |
|---|---|---|---|---|---|
| I1 | 19 | 38 | 0 | 0 | 1 |
| I2 | 25 | 43 | 0 | 0 | 2 |
| I3 | 28 | 52 | 0 | 0 | 2 |

The repeat attempts on macOS came from two different probabilistic steps in the paper's own generator,
and each repeat used the same frozen recipe:

- **I2.** The native ShellBag helper's child watchdog expired with no folder visits. It had a 240 s dispatch budget.
- **I3.** The factual supplement's frozen entry-reuse burst ended before NTFS reused a freed entry.

The study's own `attempt-N` folders show the same pattern. Every image whose generation completed was
admitted.

## Method

Each host ran the same sequence:

1. Write a dependency lock for the host. It pins the hypervisor, the Windows base by file hashes, the
   Ansible guest code and the host libraries.
2. Freeze a recipe per paper image, with a fresh random assignment.
3. Generate the image from the frozen recipe.
4. Run `fmd pipeline run` with the `luna-high` condition frozen and not dispatched. This collects the
   evidence, runs the deterministic rules and applies strict admission.

The collection toolchain is the same on every host:

- Eric Zimmerman's tools, using the exact bytes `eztools-lock.json` pins;
- the locked `dfir_ntfs` environment;
- the validated official PECmd and SBECmd builds.

## How the hosts differ

| | macOS (paper) | Linux | Windows |
|---|---|---|---|
| Guest engine | VMware Fusion 13.6.4 via Vagrant | QEMU 8.2 with KVM | QEMU with WHPX; Ansible in WSL 1 |
| Windows guest | Windows 11 ARM64, build 22000, the paper's box | Windows 11 Enterprise Evaluation x64, 26H2 build 26300 | the same x64 base as Linux |
| Guest definition | the paper's Vagrantfile | the same VM: 4 GiB, 2 vCPUs, frozen MAC and BIOS UUID, two NICs on separate subnets, three 64 MiB USB disks on xHCI ports 5, 3 and 2, the RTC started at UTC minus the frozen bias | the same as Linux |
| PECmd / SBECmd | the paper's VMware parser appliance | a disposable QEMU guest booted from the base | native PowerShell on the runner |

The x64 base is installed unattended from Microsoft's ISO with the paper's base settings and scripts:

- user `vagrant` with automatic logon, Pacific time, US English;
- sleep, automatic updates and automatic restarts off;
- outbound traffic blocked and event logs uncompressed by the paper's `offline-base.ps1`.

As in the paper's Packer build, every provisioning script runs from the first-logon commands, WinRM
included. The build therefore acts only once Windows Setup and OOBE are over, and it checks Windows'
setup flags and the autologon values before shutting the guest down. The evaluation licence is activated
once, online, during the base build.

Before a base is cached, one boot of a disposable overlay must reach WinRM and vagrant's own desktop
under generation's conditions: no route out, new MACs, a second NIC and the biased clock. One base
serves both the Linux and the Windows host.

Deviations from the paper's generator, none of which changes the generated evidence:

- **ShellBag watchdog budgets** are doubled for slower hosts, from 20/20/180/240 s to 40/40/360/480 s.
- **The playbook time cap** is 2 h under QEMU.
- **Hyper-V clock enlightenments** are on for KVM guests, to keep the ±2 s clock checkpoints.
- **Explorer** opens folders in windows, not tabs. Build 22000 had no tabs, and the ShellBag helper matches windows by handle.
- **On Windows hosts, post-export writes go through `qemu-img`.** `qemu-io` reads payloads in text mode there.

## Reproducing it

See `REPLICATE.md`: `fmd replicate doctor`, `setup` and `run`. CI runs exactly these commands
(`.github/workflows/replicate.yml`).

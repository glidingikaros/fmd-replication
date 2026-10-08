# Cross-host replication of the I-series experiment

8–9 October 2026. This report covers paper images I1, I2 and I3, each generated, collected and run through
the deterministic analysis to strict admission on three host operating systems. An image replicates when
admission passes, which requires the deterministic arm to be exact on all nine questions. No LLM
conditions were dispatched.

## Results

| Host | I1 | I2 | I3 |
|---|---|---|---|
| macOS 27, Apple silicon (the paper's setup) | 9/9, F1 1.0 | 9/9, F1 1.0 | 9/9, F1 1.0 |
| Linux, GitHub `ubuntu-24.04` runner | 9/9, F1 1.0 | 9/9, F1 1.0 | 8/9, F1 0.966 |
| Windows, GitHub `windows-2025` runner | 9/9, F1 1.0 | 9/9, F1 1.0 | 8/9, F1 0.966 |

Every admitted image has exactly the macOS counts. I3 on Linux and on Windows is not admitted. It is not
exact on BQ-TIME-01 only, with the same two false positives on both hosts (see "I3 on build 26300"
below).

| Host and image | True positives | True negatives | False positives | False negatives | Generation attempts |
|---|---|---|---|---|---|
| Linux I1 | 19 | 38 | 0 | 0 | 1 |
| Linux I2 | 25 | 43 | 0 | 0 | 1 |
| Linux I3 | 28 | 50 | 2 | 0 | 1 (repeated once, same result) |
| Windows I1 | 19 | 38 | 0 | 0 | 1 |
| Windows I2 | 25 | 43 | 0 | 0 | 1 |
| Windows I3 | 28 | 50 | 2 | 0 | 1 |

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

## I3 on build 26300

Both false positives are the paper's own old-copy controls in I3's factual supplement:
`r_997e48334979\f_c8a876614ed2.txt` and `r_997e48334979\f_3e790df77f6e.txt`. Each is a copy of
`C:\Windows\System32\where.exe` made with `[IO.File]::Copy`. The copy keeps the source's write time, so
NTFS logs a committed `$STANDARD_INFORMATION` update that moves the modified time backwards to the
source's (2024-04-01 on this build). The BQ-TIME-01 rule reports any such retained backwards transition
as committed backdating. Its own caveat says it cannot tell a restoration from an anti-forensic act. The
generator's reference labels the controls benign.

Whether the transition is still in `$LogFile` at collection decides the outcome:

| | macOS run (paper guest, build 22000) | Linux run (x64 guest, build 26300) |
|---|---|---|
| `$LogFile` size | 64 MiB | 64 MiB |
| Records at collection | 418,887 | 406,454 |
| Time the records span | 2.5 minutes | 2 h 50 min, back into the base build |
| The controls' backwards update | overwritten (only later access-time updates remain) | retained (LSN 639630876) |

The paper's guest journalled about 70 times more per minute. Its USN journal shows 4,000 to 19,000
records a minute through the last 20 minutes of generation, with Windows app-package servicing
(`Microsoft.UI.Xaml` resources, `AppxManifest.xml`, `.pckg` state) prominent at the end. The 64 MiB log
therefore wrapped every couple of minutes, long after the controls were made early in the run. On build
26300 the whole generation fit in one pass of the log.

I3's 9/9 on the paper's guest therefore depends on incidental background activity: the old-copy controls
are benign for BQ-TIME-01 only once `$LogFile` has wrapped past them. On build 26300 the same
deterministic rule reads them as backdating on every host. This is a property of the guest's Windows
build and background activity, not of the host or of the replication path. No background activity was
added to imitate the paper's guest.

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
| Windows guest | Windows 11 Pro ARM64, build 22000, the paper's box | Windows 11 Pro x64, 26H2 build 26300 | the same x64 base as Linux |
| Licence | the generic volume key W269N, never activated | the same key, never activated | the same |
| Guest definition | the paper's Vagrantfile | the same VM: 4 GiB, 2 vCPUs, frozen MAC and BIOS UUID, two NICs on separate subnets, three 64 MiB removable USB disks on xHCI ports 5, 3 and 2 plugged in once Windows runs, the RTC started at UTC minus the frozen bias | the same as Linux |
| PECmd / SBECmd | the paper's VMware parser appliance | a disposable QEMU guest booted from the base | native PowerShell on the runner |

The x64 base is installed unattended from Microsoft's consumer Windows 11 ISO (build 26300.9457, checked
against Microsoft's published SHA-256), with the paper's base settings and scripts:

- user `vagrant` with automatic logon, Pacific time, US English;
- sleep, automatic updates and automatic restarts off;
- outbound traffic blocked and event logs uncompressed by the paper's `offline-base.ps1`.

As in the paper's Packer build, every provisioning script runs from the first-logon commands, WinRM
included. The build therefore acts only once Windows Setup and OOBE are over, and it checks Windows'
setup flags and the autologon values before shutting the guest down.

The licensing is the paper's: Windows 11 Pro on the generic volume key, never activated. Microsoft's
consumer media refuse a volume key during Setup, so Setup installs Pro with the generic retail key, and
`slmgr /ipk` puts in the paper's key after first logon, offline. An Enterprise Evaluation base was built
too and set aside: its online activation did not survive the hardware generation boots it on (BIOS UUID,
MACs, CPU), and an unactivated evaluation shuts down every hour.

Before a base is cached, one boot of a disposable overlay must reach WinRM and vagrant's own desktop
under generation's conditions: no route out, new MACs, a second NIC and the biased clock. One base
serves both the Linux and the Windows host.

Deviations from the paper's generator, none of which changes the generated evidence:

- **ShellBag watchdog budgets** are doubled for slower hosts, from 20/20/180/240 s to 40/40/360/480 s.
- **The playbook time cap** is 2 h under QEMU.
- **Hyper-V clock enlightenments** are on for KVM guests, to keep the ±2 s clock checkpoints.
- **Explorer** opens folders in windows, not tabs. Build 22000 had no tabs, and the ShellBag helper matches windows by handle.
- **On Windows hosts, post-export writes go through `qemu-img`.** `qemu-io` reads payloads in text mode there.

Adjustments to the QEMU guest so that it presents what the paper's VMware guest presented:

- **The virtual USB disks are removable and are plugged in once Windows runs.** VMware's virtual USB
  storage is removable, and VMware connects it after power-on. Windows then installs a portable-device
  node per disk, and SetupAPI logs it under the disk's USBSTOR identity:
  `SWD\WPDBUSENUM\_??_USBSTOR#Disk&...#{53f56307-...}`. The USB scenario's helper requires that
  record. QEMU's default fixed disks, present at boot, leave none.
- **Generation starts only after its clock is past the base build's last log entries.** Generation
  boots the guest at UTC minus the frozen 480-minute bias, an hour behind the Pacific time the base was
  built on in summer. A generation within that hour logs SetupAPI sections earlier than the base's last
  ones. The SetupAPI window is then not established, and BQ-USB-01 became indeterminate. The paper's
  own base build started its clock at the same bias. `fmd replicate run` now waits out the gap.

One collection fix, found on Linux:

- **Required-artifact patterns ignore case.** The extractor names directories after KAPE's
  declarations (`winevt\logs`), and the check looked for `winevt/Logs`. The case-insensitive file
  systems of macOS and Windows hid the difference.

## Reproducing it

See `REPLICATE.md`: `fmd replicate doctor`, `setup` and `run`. CI runs exactly these commands
(`.github/workflows/replicate.yml`).

import json
import os

import pytest

from fmd.cli.app import main
from fmd.replication import host, run


def test_child_processes_find_the_private_dotnet_first(tmp_path, monkeypatch):
    monkeypatch.setenv("FMD_CACHE", str(tmp_path))
    (tmp_path / "dotnet").mkdir()
    env = host.environment()
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "dotnet")
    assert env["DOTNET_ROOT"] == str(tmp_path / "dotnet")
    assert env["FMD_QEMU_BASE_HOME"] == str(tmp_path / "qemu-bases")


def test_only_a_published_generation_counts_as_done(tmp_path):
    failed = tmp_path / "attempt-1" / "full_scale" / "a"
    failed.mkdir(parents=True)
    (failed / "full_scale.vmdk").write_bytes(b"")
    assert run.completed(tmp_path) is None
    published = tmp_path / "attempt-2" / "full_scale" / "b"
    published.mkdir(parents=True)
    (published / "full_scale.vmdk").write_bytes(b"")
    (published / "manifest.json").write_text("{}")
    assert run.completed(tmp_path) == published


def test_the_summary_reads_strict_admission_and_per_question_exactness(tmp_path):
    gate = tmp_path / "G5.json"
    questions = {f"BQ-{n}": {"exact": n != "LOG-01"} for n in ("DELETE-01", "LOG-01")}
    gate.write_text(json.dumps({"admission": {"status": "failed"},
                                "comparison": {"rules": {"f1": 0.9, "finding_counts": {"fn": 1},
                                                         "per_question": questions}}}))
    assert run.summary("I2", gate) == {"image": "I2", "admission": "failed", "f1": 0.9, "exact": 1, "questions": 2,
                                       "counts": {"fn": 1}, "not_exact": ["BQ-LOG-01"]}
    assert run.summary("I3", tmp_path / "missing.json") == {"image": "I3", "admission": "not reached"}


def test_a_failure_while_provisioning_is_retried_and_others_are_not():
    phase = '{"elapsed_seconds": 1.0, "error_type": "X", "outcome": "error", "phase": "%s"}'
    assert run.RETRYABLE.search(phase % "ansible_provisioning")
    assert run.RETRYABLE.search(phase % "qemu_boot")
    assert not run.RETRYABLE.search(phase % "disk_discovery_export")


def test_the_base_needs_an_iso_the_user_downloads_and_says_where_from(monkeypatch):
    from fmd.replication import setup

    monkeypatch.setattr(host, "base_guest_facts", lambda: None)
    with pytest.raises(SystemExit) as stopped:
        setup.base()
    pin = host.PINS["windows_iso"]
    assert pin["download"] in str(stopped.value) and "--iso" in str(stopped.value)
    assert pin["edition"] == "pro" and len(pin["sha256"]) == 64


def test_the_cli_offers_doctor_setup_and_run(capsys):
    for action in ("doctor", "setup", "run"):
        try:
            main(["replicate", action, "--help"])
        except SystemExit as exit:
            assert exit.code == 0
    out = capsys.readouterr().out
    assert "I1" in out and "--iso" in out


@pytest.mark.skipif(os.name == "nt", reason="pip writes .exe launchers on Windows; fmd checks console scripts on POSIX only")
def test_a_console_script_in_a_long_environment_path_hashes_like_a_short_one(tmp_path):
    from fmd.index.scanners.logfile_runtime import portable_console_sha256

    body = b"\n# dfir_ntfs\nimport sys\n"
    short, long = tmp_path / "v", tmp_path / ("x" * 140) / "v"
    for root in (short, long):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "python").write_bytes(b"")
    (short / "bin" / "ntfs_parser").write_bytes(b"#!" + str(short / "bin" / "python").encode() + b"\n" + body)
    (long / "bin" / "ntfs_parser").write_bytes(
        b"#!/bin/sh\n'''exec' '" + str(long / "bin" / "python").encode() + b"' \"$0\" \"$@\"\n' '''\n" + body)
    hashes = {portable_console_sha256(root / "bin" / "ntfs_parser", root / "bin" / "python") for root in (short, long)}
    assert len(hashes) == 1 and None not in hashes

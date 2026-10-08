import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


box = module('bootstrap_box')
ez = module('bootstrap_eztools')
dfir = module('bootstrap_dfir_ntfs')


def test_size_only_never_verifies_bytes(tmp_path):
    p = tmp_path / 'extent.bin'
    p.write_bytes(b'1234')
    lock = {'base': {'box': 'test', 'provider': 'vmware_desktop', 'version': '1', 'vmx_path': str(p)},
            'artifacts': [{'path': str(p), 'role': 'base', 'size_bytes': 4, 'sha256': 'f' * 64}]}
    args = {'box': {'name': 'test', 'provider': 'vmware_desktop', 'version': '1'},
            'flat': {'vmx_path': str(p)}, 'recipe': SimpleNamespace(validate_dependency_lock=lambda *a, **k: None)}
    result = box.verify_against_lock(lock, **args, max_hash_bytes=1, hasher=lambda _: pytest.fail('hash skipped'))
    assert result['status'] == 'mismatch' and result['hash_skipped_count'] == 1
    assert box.verify_against_lock(lock, **args, hasher=lambda _: 'f' * 64)['status'] == 'verified'
    assert box.verify_against_lock(lock, **args, hasher=lambda _: '0' * 64)['status'] == 'mismatch'


def test_portable_lock_names_box_files_relative_to_the_installed_box(tmp_path):
    vmx = tmp_path / 'box.vmx'
    vmx.write_bytes(b'1234')
    lock = {'schema_version': box.PORTABLE_LOCK_SCHEMA,
            'base': {'location': {'kind': 'vagrant_box', 'box': 'test', 'version': '1', 'architecture': None,
                                  'provider': 'vmware_desktop'},
                     'entry': 'box.vmx', 'files': [{'path': 'box.vmx', 'size_bytes': 4, 'sha256': 'f' * 64}]}}
    args = {'box': {'name': 'test', 'provider': 'vmware_desktop', 'version': '1'},
            'flat': {'vmx_path': str(vmx)}, 'recipe': SimpleNamespace(validate_dependency_lock=lambda *a, **k: None)}
    assert box.verify_against_lock(lock, **args, hasher=lambda _: 'f' * 64)['status'] == 'verified'
    result = box.verify_against_lock(lock, **args, hasher=lambda _: '0' * 64)
    assert result['problems'] == [f'base_file_hash_mismatch:{vmx}']


def test_verify_only_cannot_add_box(tmp_path):
    assert box.main(['--verify-only', '--no-lock', '--box-file', str(tmp_path / 'not-read.box')],
                    run=lambda *a, **k: pytest.fail('no command')) != 0


def test_existing_nonrepository_preserved(tmp_path):
    directory = tmp_path / 'tool'
    directory.mkdir()
    sentinel = directory / 'local.txt'
    sentinel.write_text('keep')
    with pytest.raises(ez.BootstrapError, match='refusing'):
        ez.checkout_pinned({'checkout': 'tool', 'commit_sha': 'a' * 40}, tmp_path,
                          lambda *a, **k: pytest.fail('no command'), 'git')
    assert sentinel.read_text() == 'keep'


def test_dirty_checkout_not_cleaned(tmp_path):
    (tmp_path / 'tool/.git').mkdir(parents=True)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, ' M edited.cs\n', '')
    with pytest.raises(ez.BootstrapError, match='local changes'):
        ez.checkout_pinned({'checkout': 'tool', 'commit_sha': 'a' * 40}, tmp_path, run, 'git')
    assert len(calls) == 1 and 'status' in calls[0]


def test_dfir_plan_pins_commit_and_python(tmp_path):
    lock = {'tool': 'dfir_ntfs', 'source': 'https://github.com/msuhanov/dfir_ntfs',
            'commit_id': 'a' * 40, 'python_version': '3.13.5'}
    assert dfir.install_requirement(lock).endswith('@' + 'a' * 40)
    assert dfir.venv_command('uv', lock, tmp_path)[-3:] == ['--python', '3.13.5', str(tmp_path)]
    assert '--no-deps' in dfir.install_command('uv', lock, tmp_path)
    with pytest.raises(dfir.BootstrapError):
        dfir.install_requirement({**lock, 'commit_id': 'main'})


def test_custom_lock_rebuild_refuses_before_mutation(tmp_path):
    args = dfir.build_parser().parse_args(['--lock', str(tmp_path / 'custom.json')])
    with pytest.raises(dfir.BootstrapError, match='custom locks'):
        dfir.rebuild(args, run=lambda *a, **kw: pytest.fail('no command'), which=lambda _: 'uv')


@pytest.mark.skipif(os.name == "nt", reason="bootstrap_env.sh is the POSIX entry point")
def test_env_dry_run_resolves_relative_venv(tmp_path):
    uv = tmp_path / 'uv'
    uv.write_text('#!/bin/sh\necho fake-uv\n')
    uv.chmod(0o755)
    result = subprocess.run(['bash', str(ROOT / 'scripts/bootstrap_env.sh'), '--dry-run', '--venv', 'custom'],
                            cwd=tmp_path, env={**os.environ, 'FMD_UV': str(uv)}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert str(tmp_path / 'custom') in result.stdout and '--extra dev --extra generation' in result.stdout
    assert not (tmp_path / 'custom').exists()
